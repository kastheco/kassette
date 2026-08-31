"""Pipecat processor for one native GPT-Live voice session."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
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
    build_spoken_context,
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

_LATE_DELEGATION_TTL_SECONDS = 30.0
_MAX_SYNTHETIC_TOMBSTONES = 64
_MAX_DEFERRED_DELEGATIONS = 64

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


def _has_audible_audio(audio: bytes, *, floor: int = 32) -> bool:
    """Reject empty provider audio that would otherwise leave the session speaking."""
    if len(audio) < 2:
        return False
    samples = memoryview(audio)[: len(audio) - len(audio) % 2].cast("h")
    return any(sample >= floor or sample <= -floor for sample in samples)


def _normalize_turn_text(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


@dataclass(slots=True)
class _SyntheticDelegation:
    client_id: str
    turn_id: str
    text: str
    answer: str | None = None
    real_id: str | None = None
    provider_resolved: bool = False
    response_sent: bool = False
    response_spoken: bool = False
    completed_at: float | None = None


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
        self._synthetic_delegations: dict[str, _SyntheticDelegation] = {}
        self._deferred_provider_delegations: list[ProviderEvent] = []
        self._active_delegation_id: str | None = None
        self._active_response_delegation_id: str | None = None
        self._awaiting_client_output_start = False
        self._unauthorized_output_active = False
        self._active_user_turn_id: str | None = None
        self._user_turn_number = 0
        self._user_transcript = ""
        self._client_output_authorized = not client_delegation
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
        answer = text.strip()
        synthetic = self._synthetic_delegations.get(delegation_id)
        self._pending_delegations.remove(delegation_id)
        if synthetic is not None:
            synthetic.answer = answer
            synthetic.provider_resolved = True
            if self._unauthorized_output_active and self._active_response_delegation_id is None:
                await self._transport.interrupt()
                self._unauthorized_output_active = False
            await self._flush_synthetic_response()
            return True
        self._active_response_delegation_id = delegation_id
        self._awaiting_client_output_start = True
        self._client_output_authorized = False
        try:
            await self._transport.send(build_delegation_response(delegation_id, answer))
        except BaseException:
            self._active_response_delegation_id = None
            self._awaiting_client_output_start = False
            raise
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
            self._clear_delegations()
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
                self._clear_delegations()
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
        if not _has_audible_audio(chunk.audio):
            return
        if self._client_delegation and not self._client_output_authorized:
            self._unauthorized_output_active = True
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
        if event.type == "output_audio.delta":
            self._authorize_client_output_start()
            return
        if event.type in {"input_transcript.added", "output_transcript.added"}:
            if event.role == "user":
                starting_turn = self._active_user_turn_id is None
                if starting_turn and self._active_delegation_id is None:
                    self._client_output_authorized = not self._client_delegation
                turn_id = self._ensure_user_turn_id()
                self._user_transcript += event.text or ""
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.TRANSCRIPT_DELTA,
                        role=TranscriptRole.USER,
                        text=self._user_transcript,
                        metadata={"turn_id": turn_id},
                    )
                )
            else:
                self._authorize_client_output_start()
            if event.role != "user" and (
                not self._client_delegation or self._client_output_authorized
            ):
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.TRANSCRIPT_DELTA,
                        role=_role(event),
                        text=event.text,
                    )
                )
            elif event.role != "user":
                self._unauthorized_output_active = True
            return
        if event.type == "turn.done":
            if event.role == "user":
                turn_id = self._ensure_user_turn_id()
                final_text = (event.text or self._user_transcript).strip()
                if final_text:
                    self._user_transcript = final_text
                    await self._emit_event(
                        SessionEvent(
                            session_id=self._session_id,
                            type=SessionEventType.TRANSCRIPT_FINAL,
                            role=TranscriptRole.USER,
                            text=final_text,
                            metadata={"turn_id": turn_id},
                        )
                    )
                if self._client_delegation:
                    await self._resolve_deferred_provider_delegations(final_text)
                    if final_text and self._active_delegation_id is None:
                        await self._request_synthetic_delegation(turn_id, final_text)
                self._finish_user_turn()
                return
            if (
                event.role == "assistant"
                and self._active_response_delegation_id is not None
                and self._awaiting_client_output_start
                and not self._client_output_authorized
            ):
                self._unauthorized_output_active = False
                return
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
            if not self._client_delegation or self._client_output_authorized:
                await self._emit_event(
                    SessionEvent(
                        session_id=self._session_id,
                        type=SessionEventType.TRANSCRIPT_FINAL,
                        role=_role(event),
                        text=event.text,
                    )
                )
                if self._active_response_delegation_id is not None:
                    synthetic = self._synthetic_delegations.get(self._active_response_delegation_id)
                    if synthetic is not None:
                        synthetic.response_spoken = True
                        synthetic.completed_at = time.monotonic()
            if event.role == "assistant":
                if self._client_delegation and self._active_response_delegation_id is None:
                    unresolved = self._next_provider_unresolved_synthetic()
                    if unresolved is not None:
                        unresolved.provider_resolved = True
                self._client_output_authorized = not self._client_delegation
                self._active_response_delegation_id = None
                self._awaiting_client_output_start = False
                self._unauthorized_output_active = False
                await self._flush_synthetic_response()
                self._prune_synthetic_delegations()
            return
        if event.type == "delegation.created" and event.delegation_id:
            if self._client_delegation:
                if self._active_user_turn_id is not None:
                    if len(self._deferred_provider_delegations) >= _MAX_DEFERRED_DELEGATIONS:
                        await self._fail(
                            "provider_protocol_error",
                            message="too many deferred provider delegations",
                        )
                        return
                    self._deferred_provider_delegations.append(event)
                    return
                await self._handle_client_provider_delegation(event)
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

    def _ensure_user_turn_id(self) -> str:
        if self._active_user_turn_id is None:
            self._user_turn_number += 1
            self._active_user_turn_id = f"{self._session_id}:native:{self._user_turn_number}"
        return self._active_user_turn_id

    async def _resolve_deferred_provider_delegations(self, final_text: str) -> None:
        deferred = self._deferred_provider_delegations
        self._deferred_provider_delegations = []
        if not deferred:
            return

        normalized_final = _normalize_turn_text(final_text)
        current_index = next(
            (
                index
                for index, event in enumerate(deferred)
                if _normalize_turn_text(event.text) == normalized_final
            ),
            None,
        )
        if current_index is None and len(deferred) == 1 and not deferred[0].text:
            current_index = 0

        if current_index is not None:
            await self._handle_client_provider_delegation(
                deferred[current_index],
                bind_active=True,
                match_synthetic=False,
                fallback_text=final_text,
            )
        for index, event in enumerate(deferred):
            if index == current_index:
                continue
            await self._handle_client_provider_delegation(event, bind_active=False)

    async def _handle_client_provider_delegation(
        self,
        event: ProviderEvent,
        *,
        bind_active: bool = True,
        match_synthetic: bool = True,
        fallback_text: str = "",
    ) -> None:
        if event.delegation_id is None:
            return
        synthetic = self._match_synthetic_delegation(event.text) if match_synthetic else None
        if synthetic is not None:
            synthetic.real_id = event.delegation_id
            synthetic.provider_resolved = True
            if synthetic.answer is not None and synthetic.response_sent:
                await self._transport.send(
                    build_delegation_response(
                        event.delegation_id,
                        synthetic.answer,
                        channel="commentary",
                    )
                )
                if synthetic.response_spoken:
                    self._synthetic_delegations.pop(synthetic.client_id, None)
            else:
                await self._flush_synthetic_response()
            return

        if bind_active:
            self._active_delegation_id = event.delegation_id
        self._pending_delegations.add(event.delegation_id)
        await self._emit_event(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.DELEGATION_REQUESTED,
                text=event.text or fallback_text or self._user_transcript,
                metadata={"delegation_id": event.delegation_id},
            )
        )

    async def _request_synthetic_delegation(self, turn_id: str, text: str) -> None:
        self._prune_synthetic_delegations()
        delegation_id = f"kassette:{turn_id}"[:256]
        self._active_delegation_id = delegation_id
        self._pending_delegations.add(delegation_id)
        self._synthetic_delegations[delegation_id] = _SyntheticDelegation(
            client_id=delegation_id,
            turn_id=turn_id,
            text=text,
        )
        await self._emit_event(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.DELEGATION_REQUESTED,
                text=text,
                metadata={"delegation_id": delegation_id},
            )
        )

    def _match_synthetic_delegation(
        self,
        provider_text: str | None,
    ) -> _SyntheticDelegation | None:
        self._prune_synthetic_delegations()
        unresolved = [
            delegation
            for delegation in self._synthetic_delegations.values()
            if delegation.real_id is None
        ]
        active = [delegation for delegation in unresolved if not delegation.response_sent]
        normalized = _normalize_turn_text(provider_text)
        if normalized:
            for delegation in active:
                if _normalize_turn_text(delegation.text) == normalized:
                    return delegation
            for delegation in unresolved:
                if _normalize_turn_text(delegation.text) == normalized:
                    return delegation
        return active[0] if active and not normalized else None

    def _next_provider_unresolved_synthetic(self) -> _SyntheticDelegation | None:
        return next(
            (
                delegation
                for delegation in self._synthetic_delegations.values()
                if not delegation.provider_resolved and not delegation.response_sent
            ),
            None,
        )

    async def _send_synthetic_response(
        self,
        delegation: _SyntheticDelegation,
    ) -> None:
        if (
            delegation.response_sent
            or delegation.answer is None
            or not delegation.provider_resolved
            or self._active_response_delegation_id is not None
            or self._unauthorized_output_active
        ):
            return
        message = (
            build_delegation_response(delegation.real_id, delegation.answer)
            if delegation.real_id is not None
            else build_spoken_context(delegation.answer)
        )
        self._active_response_delegation_id = delegation.client_id
        self._awaiting_client_output_start = True
        self._client_output_authorized = False
        try:
            await self._transport.send(message)
        except BaseException:
            self._active_response_delegation_id = None
            self._awaiting_client_output_start = False
            raise
        delegation.response_sent = True

    def _authorize_client_output_start(self) -> None:
        if (
            self._client_delegation
            and self._active_response_delegation_id is not None
            and self._awaiting_client_output_start
        ):
            self._awaiting_client_output_start = False
            self._unauthorized_output_active = False
            self._client_output_authorized = True

    async def _flush_synthetic_response(self) -> None:
        if self._active_response_delegation_id is not None or self._unauthorized_output_active:
            return
        for delegation in self._synthetic_delegations.values():
            if delegation.answer is None or delegation.response_sent:
                continue
            if not delegation.provider_resolved:
                return
            await self._send_synthetic_response(delegation)
            return

    def _prune_synthetic_delegations(self) -> None:
        now = time.monotonic()
        expired = [
            client_id
            for client_id, delegation in self._synthetic_delegations.items()
            if delegation.completed_at is not None
            and now - delegation.completed_at >= _LATE_DELEGATION_TTL_SECONDS
        ]
        for client_id in expired:
            self._synthetic_delegations.pop(client_id, None)

        tombstones = [
            delegation
            for delegation in self._synthetic_delegations.values()
            if delegation.completed_at is not None
        ]
        for delegation in tombstones[:-_MAX_SYNTHETIC_TOMBSTONES]:
            self._synthetic_delegations.pop(delegation.client_id, None)

    def _finish_user_turn(self) -> None:
        self._active_user_turn_id = None
        self._active_delegation_id = None
        self._user_transcript = ""

    def _clear_delegations(self) -> None:
        self._pending_delegations.clear()
        self._synthetic_delegations.clear()
        self._deferred_provider_delegations.clear()
        self._active_delegation_id = None
        self._active_response_delegation_id = None
        self._awaiting_client_output_start = False
        self._active_user_turn_id = None
        self._user_transcript = ""
        self._unauthorized_output_active = False
        self._client_output_authorized = not self._client_delegation

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
                self._clear_delegations()
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
