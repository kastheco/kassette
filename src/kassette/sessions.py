"""Transient voice-session registry and local audio leasing."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from uuid import uuid4

from kassette.domain import TERMINAL_SESSION_STATES, SessionState, VoiceSessionSnapshot


class SessionRegistryError(RuntimeError):
    code = "session_registry_error"


class SessionNotFoundError(SessionRegistryError):
    code = "session_not_found"


class InvalidSessionTransitionError(SessionRegistryError):
    code = "invalid_session_transition"


class AudioDeviceBusyError(SessionRegistryError):
    code = "audio_device_busy"


class SessionGenerationMismatchError(SessionRegistryError):
    code = "session_generation_mismatch"


@dataclass(frozen=True, slots=True)
class SessionHandle:
    id: str
    generation: int


CloseSession = Callable[[], Awaitable[None]]
RunSession = Callable[[], Awaitable[None]]
_MAX_SESSION_ID_CHARS = 96


@dataclass(frozen=True, slots=True)
class _ActiveSession:
    handle: SessionHandle
    close: CloseSession


class LiveSessionCoordinator:
    """Serialize replacement of the one localhost voice loop."""

    def __init__(self) -> None:
        self._replacement_lock = asyncio.Lock()
        self._lock = asyncio.Lock()
        self._active: _ActiveSession | None = None

    async def replace(self, handle: SessionHandle, close: CloseSession) -> bool:
        previous_closed = True
        async with self._replacement_lock:
            async with self._lock:
                previous = self._active
            if previous is not None and previous.handle != handle:
                try:
                    await previous.close()
                except asyncio.CancelledError:
                    task = asyncio.current_task()
                    if task is not None and task.cancelling():
                        raise
                    previous_closed = False
                except Exception:
                    previous_closed = False
            async with self._lock:
                self._active = _ActiveSession(handle, close)
        return previous_closed

    async def run_active(self, handle: SessionHandle, run: RunSession) -> bool:
        async with self._replacement_lock:
            async with self._lock:
                active = self._active
                if active is None or active.handle != handle:
                    return False

                async def invoke_run() -> None:
                    await run()

                run_task = asyncio.create_task(invoke_run())

                async def close_started_session() -> None:
                    if not run_task.done():
                        run_task.cancel()
                    await active.close()

                self._active = _ActiveSession(handle, close_started_session)
        await run_task
        return True

    async def clear(self, handle: SessionHandle) -> None:
        async with self._lock:
            if self._active is not None and self._active.handle == handle:
                self._active = None

    async def active(self) -> SessionHandle | None:
        async with self._lock:
            return self._active.handle if self._active is not None else None


_ALLOWED_TRANSITIONS: dict[SessionState, frozenset[SessionState]] = {
    SessionState.CREATED: frozenset(
        {SessionState.CONNECTING, SessionState.CLOSING, SessionState.FAILED}
    ),
    SessionState.CONNECTING: frozenset(
        {SessionState.LISTENING, SessionState.CLOSING, SessionState.FAILED}
    ),
    SessionState.LISTENING: frozenset(
        {
            SessionState.SPEAKING,
            SessionState.INTERRUPTING,
            SessionState.CLOSING,
            SessionState.FAILED,
        }
    ),
    SessionState.SPEAKING: frozenset(
        {
            SessionState.LISTENING,
            SessionState.INTERRUPTING,
            SessionState.CLOSING,
            SessionState.FAILED,
        }
    ),
    SessionState.INTERRUPTING: frozenset(
        {SessionState.LISTENING, SessionState.CLOSING, SessionState.FAILED}
    ),
    SessionState.CLOSING: frozenset({SessionState.CLOSED, SessionState.FAILED}),
    SessionState.CLOSED: frozenset(),
    SessionState.FAILED: frozenset(),
}


class SessionRegistry:
    """Own transient sessions and one exclusive local audio lease."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, VoiceSessionSnapshot] = {}
        self._next_generation = 0
        self._audio_owner: SessionHandle | None = None

    async def create(self, session_id: str | None = None) -> VoiceSessionSnapshot:
        resolved_id = session_id or str(uuid4())
        if len(resolved_id) > _MAX_SESSION_ID_CHARS or not resolved_id.isprintable():
            raise SessionRegistryError("invalid voice session identifier")
        async with self._lock:
            if resolved_id in self._sessions:
                raise SessionRegistryError("voice session already exists")
            self._next_generation += 1
            generation = self._next_generation
            session = VoiceSessionSnapshot(
                id=resolved_id,
                state=SessionState.CREATED,
                generation=generation,
            )
            self._sessions[resolved_id] = session
            return session

    async def get(self, session_id: str) -> VoiceSessionSnapshot:
        async with self._lock:
            return self._require(session_id)

    async def list(self) -> tuple[VoiceSessionSnapshot, ...]:
        async with self._lock:
            return tuple(self._sessions.values())

    async def transition(
        self,
        session_id: str,
        state: SessionState,
        *,
        provider_session_id: str | None = None,
        error_code: str | None = None,
        expected_generation: int | None = None,
    ) -> VoiceSessionSnapshot:
        async with self._lock:
            current = self._require(session_id)
            self._require_generation(current, expected_generation)
            if state == current.state:
                return current
            if state not in _ALLOWED_TRANSITIONS[current.state]:
                raise InvalidSessionTransitionError(
                    f"cannot transition voice session from {current.state} to {state}"
                )
            next_session = replace(
                current,
                state=state,
                provider_session_id=provider_session_id or current.provider_session_id,
                error_code=error_code,
            )
            self._sessions[session_id] = next_session
            if state in TERMINAL_SESSION_STATES and self._audio_owner == SessionHandle(
                session_id, current.generation
            ):
                self._audio_owner = None
            return next_session

    async def acquire_audio(
        self, session_id: str, *, expected_generation: int | None = None
    ) -> None:
        async with self._lock:
            session = self._require(session_id)
            self._require_generation(session, expected_generation)
            if session.state in TERMINAL_SESSION_STATES:
                raise SessionRegistryError("a terminal voice session cannot acquire audio")
            handle = SessionHandle(session_id, session.generation)
            owner = self._audio_owner
            if owner is not None and owner != handle:
                raise AudioDeviceBusyError("local audio is already leased by another voice session")
            self._audio_owner = handle

    async def release_audio(
        self, session_id: str, *, expected_generation: int | None = None
    ) -> None:
        async with self._lock:
            if self._audio_owner is None or self._audio_owner.id != session_id:
                return
            if (
                expected_generation is not None
                and self._audio_owner.generation != expected_generation
            ):
                return
            if self._audio_owner.id == session_id:
                self._audio_owner = None

    async def audio_owner(self) -> str | None:
        async with self._lock:
            return self._audio_owner.id if self._audio_owner is not None else None

    async def reap(self, session_id: str, *, expected_generation: int | None = None) -> None:
        async with self._lock:
            session = self._require(session_id)
            self._require_generation(session, expected_generation)
            if session.state not in TERMINAL_SESSION_STATES:
                raise SessionRegistryError("only terminal voice sessions can be reaped")
            self._sessions.pop(session_id)
            if self._audio_owner == SessionHandle(session_id, session.generation):
                self._audio_owner = None

    def _require(self, session_id: str) -> VoiceSessionSnapshot:
        try:
            return self._sessions[session_id]
        except KeyError as error:
            raise SessionNotFoundError("voice session not found") from error

    @staticmethod
    def _require_generation(session: VoiceSessionSnapshot, expected_generation: int | None) -> None:
        if expected_generation is not None and session.generation != expected_generation:
            raise SessionGenerationMismatchError("stale voice session generation")
