"""Pipecat processor for one native GPT-Live voice session."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol, cast

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InputAudioRawFrame,
    InterruptionFrame,
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
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
    build_delegation_response,
    build_delegation_unavailable,
)
from kassette.providers.quicksilver.transport import QuicksilverTransport
from kassette.sessions import SessionNotFoundError, SessionRegistry, SessionRegistryError

DIRECT_VOICE_INSTRUCTIONS = """You are kassette, a direct realtime voice assistant.

Respond directly, briefly, conversationally, and in speech-friendly language.
Do not use markdown, code blocks, or long lists unless the user explicitly asks
for technical detail aloud. The client cannot delegate work to another agent in
this first delivery, so answer with your own knowledge and never request client
delegation.
"""

DELEGATED_VOICE_INSTRUCTIONS = """You are kassette, the live voice interface for the client's agent.

Delegate every user request to the client. Remain silent and don't acknowledge
the request before client context arrives. Once the client returns the agent's
answer, present it naturally and faithfully in concise spoken form.
Do not mention delegation, hidden context, or implementation details.
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
        generation: int = 1,
        name: str | None = None,
        enable_direct_mode: bool = False,
        manage_session_lifecycle: bool = True,
        client_delegation: bool = False,
        publish_client_events: bool = False,
    ) -> None:
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            name=name,
            enable_direct_mode=enable_direct_mode,
        )
        self._session_id = session_id
        self._generation = generation
        self._registry = registry
        self._event_sink = event_sink or _discard_event
        self._manage_session_lifecycle = manage_session_lifecycle
        self._client_delegation = client_delegation
        self._publish_client_events = publish_client_events
        self._client_event_sequence = 0
        self._pending_delegations: set[str] = set()
        self._transport = transport_factory(
            session_id=session_id,
            credentials=credentials,
            instructions=(
                DELEGATED_VOICE_INSTRUCTIONS if client_delegation else DIRECT_VOICE_INSTRUCTIONS
            ),
            voice=voice,
            event_sink=self._handle_provider_event,
            audio_sink=self._handle_provider_audio,
        )
        self._close_lock = asyncio.Lock()
        self._cleanup_task: asyncio.Task[None] | None = None
        self._interrupt_lock = asyncio.Lock()
        self._closed = False
        self._received_input = False
        self._speaking = False

    async def _emit_event(self, event: SessionEvent) -> None:
        await self._event_sink(event)
        if not self._publish_client_events:
            return
        data: dict[str, object] = dict(event.metadata)
        data["session_id"] = event.session_id
        if event.state is not None:
            data["state"] = event.state.value
        if event.role is not None:
            data["role"] = event.role.value
        if event.text is not None:
            data["text"] = event.text
        if event.provider_type is not None:
            data["provider_type"] = event.provider_type
        if event.error_code is not None:
            data["error_code"] = event.error_code
        self._client_event_sequence += 1
        data["sequence"] = self._client_event_sequence
        await self.push_frame(
            OutputTransportMessageUrgentFrame(
                message={"label": "kassette", "type": event.type.value, "data": data}
            )
        )

    async def handle_client_message(self, message: Any) -> bool:
        if not self._client_delegation or not isinstance(message, dict):
            return False
        record = cast(dict[str, object], message)
        if record.get("label") != "kassette":
            return False
        if record.get("type") in {"input.pause", "input.resume"}:
            return True
        if record.get("type") != "delegation.complete":
            return False
        data = record.get("data")
        if not isinstance(data, dict):
            return False
        payload = cast(dict[str, object], data)
        delegation_id = payload.get("delegation_id")
        text = payload.get("text")
        if (
            not isinstance(delegation_id, str)
            or delegation_id not in self._pending_delegations
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > 32_000
        ):
            return False
        await self._transport.send(build_delegation_response(delegation_id, text.strip()))
        self._pending_delegations.remove(delegation_id)
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame):
            await self._start_session()
        elif isinstance(frame, InputAudioRawFrame):
            if self._closed or not await self._is_current():
                return
            if not self._received_input:
                self._received_input = True
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.INPUT_AUDIO_STARTED,
                    )
                )
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
        if self._manage_session_lifecycle:
            await self._registry.acquire_audio(
                self._session_id,
                expected_generation=self._generation,
            )
        try:
            if self._manage_session_lifecycle:
                await self._transition(SessionState.CONNECTING)
            await self._transport.open()
        except BaseException:
            await self._fail(
                "provider_connection_failed",
                message="provider connection failed",
            )
            raise

    async def _interrupt(self) -> None:
        async with self._interrupt_lock:
            if self._closed or not await self._is_current():
                return
            snapshot = await self._registry.get(self._session_id)
            if snapshot.state not in {SessionState.LISTENING, SessionState.SPEAKING}:
                return
            await self._transition(SessionState.INTERRUPTING)
            self._speaking = False
            self._pending_delegations.clear()
            try:
                await self._transport.interrupt()
            except BaseException:
                snapshot = await self._registry.get(self._session_id)
                if snapshot.state is SessionState.INTERRUPTING:
                    await self._transition(SessionState.LISTENING)
                raise
            try:
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.INTERRUPTED,
                        state=SessionState.INTERRUPTING,
                    )
                )
            finally:
                snapshot = await self._registry.get(self._session_id)
                if snapshot.state is SessionState.INTERRUPTING:
                    await self._transition(SessionState.LISTENING)

    async def _close(self) -> None:
        async with self._close_lock:
            if self._cleanup_task is None:
                self._closed = True
                self._pending_delegations.clear()
                self._cleanup_task = asyncio.create_task(self._close_session())
            task = self._cleanup_task
        await asyncio.shield(task)

    async def _close_session(self) -> None:
        if not self._manage_session_lifecycle:
            await self._transport.close()
            return
        current = await self._is_current()
        try:
            if current:
                snapshot = await self._registry.get(self._session_id)
                if snapshot.state not in {SessionState.CLOSED, SessionState.FAILED}:
                    await self._transition(SessionState.CLOSING)
        finally:
            try:
                await self._transport.close()
            finally:
                try:
                    if current and await self._is_current():
                        snapshot = await self._registry.get(self._session_id)
                        if snapshot.state is SessionState.CLOSING:
                            await self._transition(SessionState.CLOSED)
                finally:
                    await self._registry.release_audio(
                        self._session_id,
                        expected_generation=self._generation,
                    )

    async def _handle_provider_audio(self, chunk: AudioChunk) -> None:
        if self._closed or not await self._is_current():
            return
        if not self._speaking:
            self._speaking = True
            await self._transition(SessionState.SPEAKING)
            await self._emit_event(
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
        if self._closed or not await self._is_current():
            return
        if event.type == "session.started":
            await self._transition(SessionState.LISTENING, provider_session_id=event.session_id)
            return
        if event.type in {"input_transcript.added", "output_transcript.added"}:
            await self._emit_event(
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
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.SPEECH_STOPPED,
                        state=SessionState.LISTENING,
                    )
                )
            await self._emit_event(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.TRANSCRIPT_FINAL,
                    role=_role(event),
                    text=event.text,
                )
            )
            return
        if event.type == "delegation.created" and event.delegation_id:
            if self._client_delegation:
                self._pending_delegations.add(event.delegation_id)
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.DELEGATION_REQUESTED,
                        text=event.text,
                        metadata={"delegation_id": event.delegation_id},
                    )
                )
            else:
                await self._transport.send(build_delegation_unavailable(event.delegation_id))
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.DELEGATION_UNAVAILABLE,
                    )
                )
            return
        if event.type == "unknown":
            await self._emit_event(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.PROVIDER_UNKNOWN,
                    provider_type=event.wire_type,
                )
            )
            return
        if event.type == "error":
            await self._fail("provider_error", message="provider error")

    async def _transition(
        self,
        state: SessionState,
        *,
        provider_session_id: str | None = None,
    ) -> None:
        resolved_state = state
        if self._manage_session_lifecycle:
            snapshot = await self._registry.transition(
                self._session_id,
                state,
                provider_session_id=provider_session_id,
                expected_generation=self._generation,
            )
            resolved_state = snapshot.state
        await self._emit_event(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=resolved_state,
                metadata=(
                    {"provider_session_id": provider_session_id}
                    if provider_session_id is not None
                    else {}
                ),
            )
        )

    async def _fail(self, error_code: str, *, message: str) -> None:
        async with self._close_lock:
            if self._cleanup_task is None:
                self._closed = True
                self._cleanup_task = asyncio.create_task(
                    self._fail_session(error_code, message=message)
                )
            task = self._cleanup_task
        await asyncio.shield(task)

    async def _fail_session(self, error_code: str, *, message: str) -> None:
        if not self._manage_session_lifecycle:
            try:
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.ERROR,
                        state=SessionState.FAILED,
                        error_code=error_code,
                        metadata={"message": message},
                    )
                )
            finally:
                await self._transport.close()
            return
        previous = None
        snapshot = None
        try:
            previous = await self._registry.get(self._session_id)
            snapshot = await self._registry.transition(
                self._session_id,
                SessionState.FAILED,
                error_code=error_code,
                expected_generation=self._generation,
            )
        except SessionRegistryError:
            pass
        finally:
            await self._registry.release_audio(
                self._session_id,
                expected_generation=self._generation,
            )
        try:
            if (
                previous is not None
                and snapshot is not None
                and previous.state is not snapshot.state
            ):
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.SESSION_STATE_CHANGED,
                        state=snapshot.state,
                    )
                )
            await self._emit_event(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.ERROR,
                    state=SessionState.FAILED,
                    error_code=error_code,
                    metadata={"message": message},
                )
            )
        finally:
            await self._transport.close()

    async def _is_current(self) -> bool:
        try:
            snapshot = await self._registry.get(self._session_id)
        except SessionNotFoundError:
            return False
        if snapshot.generation != self._generation:
            return False
        return True


def _role(event: ProviderEvent) -> TranscriptRole | None:
    if event.role == "user":
        return TranscriptRole.USER
    if event.role == "assistant":
        return TranscriptRole.ASSISTANT
    return None
