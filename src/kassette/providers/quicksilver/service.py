"""Pipecat processor for one native GPT-Live voice session."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from kassette.credentials import CodexCredentialProvider
from kassette.domain import (
    AudioChunk,
    EventSink,
    SessionEvent,
    SessionEventType,
    SessionState,
    TranscriptRole,
)
from kassette.providers.quicksilver.protocol import (
    DEFAULT_LIVE_VOICE,
    LiveVoice,
    ProviderEvent,
    build_delegation_unavailable,
)
from kassette.providers.quicksilver.transport import QuicksilverTransport
from kassette.sessions import SessionRegistry, SessionRegistryError

DIRECT_VOICE_INSTRUCTIONS = """You are kassette, a direct realtime voice assistant.

Respond directly, briefly, conversationally, and in speech-friendly language.
Do not use markdown, code blocks, or long lists unless the user explicitly asks
for technical detail aloud. The client cannot delegate work to another agent in
this first delivery, so answer with your own knowledge and never request client
delegation.
"""


class QuicksilverSessionTransport(Protocol):
    async def open(self) -> None: ...

    async def send_audio(self, chunk: AudioChunk) -> None: ...

    async def send(self, message: dict[str, Any]) -> None: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...


TransportFactory = Callable[..., QuicksilverSessionTransport]


async def _discard_event(_: SessionEvent) -> None:
    return


class GPTLiveService(FrameProcessor):
    """Keep Quicksilver details behind a normal Pipecat audio processor."""

    def __init__(
        self,
        *,
        session_id: str,
        registry: SessionRegistry,
        credentials: CodexCredentialProvider,
        event_sink: EventSink | None = None,
        voice: LiveVoice = DEFAULT_LIVE_VOICE,
        transport_factory: TransportFactory = QuicksilverTransport,
        name: str | None = None,
        enable_direct_mode: bool = False,
    ) -> None:
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            name=name,
            enable_direct_mode=enable_direct_mode,
        )
        self._session_id = session_id
        self._registry = registry
        self._event_sink = event_sink or _discard_event
        self._transport = transport_factory(
            session_id=session_id,
            credentials=credentials,
            instructions=DIRECT_VOICE_INSTRUCTIONS,
            voice=voice,
            event_sink=self._handle_provider_event,
            audio_sink=self._handle_provider_audio,
        )
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._speaking = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._start_session()
        elif isinstance(frame, InputAudioRawFrame):
            await self._transport.send_audio(
                AudioChunk(
                    audio=frame.audio,
                    sample_rate=frame.sample_rate,
                    num_channels=frame.num_channels,
                )
            )
        elif isinstance(frame, InterruptionFrame):
            await self._interrupt()
        elif isinstance(frame, (EndFrame, CancelFrame)):
            await self._close()

        await self.push_frame(frame, direction)

    async def _start_session(self) -> None:
        await self._registry.acquire_audio(self._session_id)
        await self._transition(SessionState.CONNECTING)
        try:
            await self._transport.open()
        except BaseException:
            await self._fail(
                "provider_connection_failed",
                message="provider connection failed",
            )
            raise

    async def _interrupt(self) -> None:
        snapshot = await self._registry.get(self._session_id)
        if snapshot.state in {SessionState.LISTENING, SessionState.SPEAKING}:
            await self._transition(SessionState.INTERRUPTING)
        await self._transport.interrupt()
        self._speaking = False
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.INTERRUPTED,
                state=SessionState.INTERRUPTING,
            )
        )
        snapshot = await self._registry.get(self._session_id)
        if snapshot.state is SessionState.INTERRUPTING:
            await self._transition(SessionState.LISTENING)

    async def _close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            snapshot = await self._registry.get(self._session_id)
            if snapshot.state not in {SessionState.CLOSED, SessionState.FAILED}:
                await self._transition(SessionState.CLOSING)
            try:
                await self._transport.close()
            finally:
                snapshot = await self._registry.get(self._session_id)
                if snapshot.state is SessionState.CLOSING:
                    await self._transition(SessionState.CLOSED)
                await self._registry.release_audio(self._session_id)

    async def _handle_provider_audio(self, chunk: AudioChunk) -> None:
        if self._closed:
            return
        if not self._speaking:
            self._speaking = True
            await self._transition(SessionState.SPEAKING)
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.SPEECH_STARTED,
                    state=SessionState.SPEAKING,
                )
            )
        await self.push_frame(
            OutputAudioRawFrame(
                audio=chunk.audio,
                sample_rate=chunk.sample_rate,
                num_channels=chunk.num_channels,
            )
        )

    async def _handle_provider_event(self, event: ProviderEvent) -> None:
        if self._closed:
            return
        if event.type == "session.started":
            await self._transition(SessionState.LISTENING, provider_session_id=event.session_id)
            return
        if event.type in {"input_transcript.added", "output_transcript.added"}:
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.TRANSCRIPT_DELTA,
                    role=_role(event),
                    text=event.text,
                )
            )
            return
        if event.type == "turn.done":
            if event.role == "assistant" and self._speaking:
                self._speaking = False
                await self._transition(SessionState.LISTENING)
                await self._event_sink(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.SPEECH_STOPPED,
                        state=SessionState.LISTENING,
                    )
                )
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.TRANSCRIPT_FINAL,
                    role=_role(event),
                    text=event.text,
                )
            )
            return
        if event.type == "delegation.created" and event.delegation_id:
            await self._transport.send(build_delegation_unavailable(event.delegation_id))
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.DELEGATION_UNAVAILABLE,
                )
            )
            return
        if event.type == "unknown":
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.PROVIDER_UNKNOWN,
                    provider_type=event.wire_type,
                )
            )
            return
        if event.type == "error":
            await self._fail("provider_error", message=event.message or "provider error")

    async def _transition(
        self,
        state: SessionState,
        *,
        provider_session_id: str | None = None,
    ) -> None:
        snapshot = await self._registry.transition(
            self._session_id,
            state,
            provider_session_id=provider_session_id,
        )
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=snapshot.state,
            )
        )

    async def _fail(self, error_code: str, *, message: str) -> None:
        previous = None
        snapshot = None
        try:
            previous = await self._registry.get(self._session_id)
            snapshot = await self._registry.transition(
                self._session_id,
                SessionState.FAILED,
                error_code=error_code,
            )
        except SessionRegistryError:
            pass
        finally:
            await self._registry.release_audio(self._session_id)
        if previous is not None and snapshot is not None and previous.state is not snapshot.state:
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.SESSION_STATE_CHANGED,
                    state=snapshot.state,
                )
            )
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.ERROR,
                state=SessionState.FAILED,
                error_code=error_code,
                metadata={"message": message},
            )
        )


def _role(event: ProviderEvent) -> TranscriptRole | None:
    if event.role == "user":
        return TranscriptRole.USER
    if event.role == "assistant":
        return TranscriptRole.ASSISTANT
    return None
