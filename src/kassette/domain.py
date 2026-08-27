"""Public domain contracts for kassette voice sessions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class SessionState(StrEnum):
    CREATED = "created"
    CONNECTING = "connecting"
    LISTENING = "listening"
    SPEAKING = "speaking"
    INTERRUPTING = "interrupting"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


TERMINAL_SESSION_STATES = frozenset({SessionState.CLOSED, SessionState.FAILED})


class SessionEventType(StrEnum):
    SESSION_CREATED = "session.created"
    SESSION_STATE_CHANGED = "session.state_changed"
    INPUT_AUDIO_STARTED = "input.audio_started"
    TRANSCRIPT_DELTA = "transcript.delta"
    TRANSCRIPT_FINAL = "transcript.final"
    SPEECH_STARTED = "speech.started"
    SPEECH_STOPPED = "speech.stopped"
    INTERRUPTED = "session.interrupted"
    DELEGATION_UNAVAILABLE = "delegation.unavailable"
    PROVIDER_UNKNOWN = "provider.unknown"
    ERROR = "session.error"


class TranscriptRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class AudioChunk:
    """One signed 16-bit PCM audio chunk."""

    audio: bytes
    sample_rate: int
    num_channels: int

    def __post_init__(self) -> None:
        if self.sample_rate <= 0:
            raise ValueError("sample_rate must be positive")
        if self.num_channels <= 0:
            raise ValueError("num_channels must be positive")
        if len(self.audio) % (self.num_channels * 2) != 0:
            raise ValueError("audio must contain complete signed 16-bit PCM frames")


def _empty_metadata() -> dict[str, str | int | float | bool | None]:
    return {}


@dataclass(frozen=True, slots=True)
class SessionEvent:
    session_id: str
    type: SessionEventType
    state: SessionState | None = None
    role: TranscriptRole | None = None
    text: str | None = None
    provider_type: str | None = None
    error_code: str | None = None
    metadata: dict[str, str | int | float | bool | None] = field(default_factory=_empty_metadata)


@dataclass(frozen=True, slots=True)
class VoiceSessionSnapshot:
    id: str
    state: SessionState
    generation: int = 1
    provider_session_id: str | None = None
    error_code: str | None = None


EventSink = Callable[[SessionEvent], Awaitable[None]]


class NativeVoiceProvider(Protocol):
    """Provider boundary for a native voice session."""

    async def open(self, session_id: str, sink: EventSink) -> None: ...

    async def send_audio(self, chunk: AudioChunk) -> None: ...

    async def interrupt(self) -> None: ...

    async def close(self) -> None: ...
