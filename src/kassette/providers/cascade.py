"""Provider-neutral transcript and speech events for cascaded voice sessions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from kassette.domain import EventSink, SessionEvent, SessionEventType, SessionState, TranscriptRole

_MAX_TRANSCRIPT_CHARS = 32_000


async def _discard_event(_: SessionEvent) -> None:
    return


class CascadedBargeInProcessor(FrameProcessor):
    """Flush queued or active speech as soon as VAD detects user speech."""

    def __init__(self, *, name: str | None = None) -> None:
        super().__init__(name=name)  # pyright: ignore[reportUnknownMemberType]
        self._queued_speech = 0
        self._active_speech = 0
        self._playback_active = False

    async def queue_speech(
        self,
        text: str,
        enqueue: Callable[[Frame], Awaitable[None]],
    ) -> None:
        """Queue speech while keeping barge-in state consistent on failure."""
        self._queued_speech += 1
        try:
            await enqueue(TTSSpeakFrame(text=text, append_to_context=False))
        except BaseException:
            self._queued_speech = max(0, self._queued_speech - 1)
            raise

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSStartedFrame):
            self._queued_speech = max(0, self._queued_speech - 1)
            self._active_speech += 1
        elif isinstance(frame, TTSStoppedFrame):
            self._active_speech = max(0, self._active_speech - 1)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self._playback_active = True
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self._playback_active = False
        elif isinstance(frame, InterruptionFrame):
            self._reset_speech()
        elif isinstance(frame, VADUserStartedSpeakingFrame) and self._speech_pending():
            self._reset_speech()
            await self.broadcast_interruption()

        await self.push_frame(frame, direction)

    def _speech_pending(self) -> bool:
        return self._queued_speech > 0 or self._active_speech > 0 or self._playback_active

    def _reset_speech(self) -> None:
        self._queued_speech = 0
        self._active_speech = 0
        self._playback_active = False


class CascadedVoiceEvents(FrameProcessor):
    """Relay STT and TTS lifecycle frames to the browser data channel."""

    def __init__(
        self,
        *,
        session_id: str,
        event_sink: EventSink | None = None,
        name: str | None = None,
        enable_direct_mode: bool = False,
        publish_transcripts: bool = True,
        publish_speech: bool = True,
        publish_start_state: bool = True,
    ) -> None:
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            name=name,
            enable_direct_mode=enable_direct_mode,
        )
        self._session_id = session_id
        self._event_sink = event_sink or _discard_event
        self._publish_transcripts = publish_transcripts
        self._publish_speech = publish_speech
        self._publish_start_state = publish_start_state
        self._turn_number = 0
        self._active_turn_id: str | None = None
        self._sequence = 0

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame) and self._publish_start_state:
            await self.publish_state(SessionState.LISTENING)
        elif isinstance(frame, InterimTranscriptionFrame) and self._publish_transcripts:
            await self._publish_transcript(frame.text, final=False)
        elif isinstance(frame, TranscriptionFrame) and self._publish_transcripts:
            await self._publish_transcript(frame.text, final=True)
        elif isinstance(frame, TTSStartedFrame) and self._publish_speech:
            await self.publish_state(SessionState.SPEAKING)
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.SPEECH_STARTED,
                    state=SessionState.SPEAKING,
                )
            )
        elif isinstance(frame, TTSStoppedFrame) and self._publish_speech:
            await self.publish_state(SessionState.LISTENING)
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.SPEECH_STOPPED,
                    state=SessionState.LISTENING,
                )
            )
        elif isinstance(frame, InterruptionFrame) and self._publish_speech:
            await self._event_sink(
                SessionEvent(
                    session_id=self._session_id,
                    type=SessionEventType.INTERRUPTED,
                    state=SessionState.INTERRUPTING,
                )
            )
            await self.publish_state(SessionState.LISTENING)

        await self.push_frame(frame, direction)

    async def publish_state(self, state: SessionState) -> None:
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=state,
            )
        )
        await self._send(
            "session.state_changed",
            {
                "session_id": self._session_id,
                "state": state.value,
            },
        )

    async def publish_input_state(self, *, paused: bool) -> None:
        if paused:
            self._active_turn_id = None
        await self._send(
            "input.state_changed",
            {
                "session_id": self._session_id,
                "paused": paused,
            },
        )

    async def publish_error(self, code: str, message: str) -> None:
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.ERROR,
                state=SessionState.FAILED,
                error_code=code,
                metadata={"message": message},
            )
        )
        await self._send(
            "session.error",
            {
                "session_id": self._session_id,
                "code": code,
                "message": message,
            },
        )

    async def _publish_transcript(self, text: str, *, final: bool) -> None:
        normalized = text.strip()[:_MAX_TRANSCRIPT_CHARS]
        if not normalized:
            if final:
                self._active_turn_id = None
            return
        turn_id = self._turn_id()
        event_type = (
            SessionEventType.TRANSCRIPT_FINAL if final else SessionEventType.TRANSCRIPT_DELTA
        )
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=event_type,
                role=TranscriptRole.USER,
                text=normalized,
                metadata={"turn_id": turn_id},
            )
        )
        await self._send(
            event_type.value,
            {
                "session_id": self._session_id,
                "turn_id": turn_id,
                "role": TranscriptRole.USER.value,
                "text": normalized,
                "final": final,
            },
        )
        if final:
            self._active_turn_id = None

    def _turn_id(self) -> str:
        if self._active_turn_id is None:
            self._turn_number += 1
            self._active_turn_id = f"{self._session_id}:{self._turn_number}"
        return self._active_turn_id

    async def _send(self, event_type: str, data: dict[str, Any]) -> None:
        self._sequence += 1
        await self.push_frame(
            OutputTransportMessageUrgentFrame(
                message={
                    "label": "kassette",
                    "type": event_type,
                    "data": {**data, "sequence": self._sequence},
                }
            )
        )


TTSRequestSink = Callable[[str], Awaitable[None]]
InputPauseSink = Callable[[bool], Awaitable[None]]


async def handle_client_message(
    message: Any,
    speak: TTSRequestSink,
    *,
    set_input_paused: InputPauseSink | None = None,
) -> bool:
    """Validate one browser application message and route bounded voice controls."""
    if not isinstance(message, dict):
        return False
    record = cast(dict[str, object], message)
    if record.get("label") != "kassette":
        return False
    message_type = record.get("type")
    data = record.get("data")
    if not isinstance(data, dict):
        return False
    if message_type in {"input.pause", "input.resume"}:
        if set_input_paused is None:
            return False
        await set_input_paused(message_type == "input.pause")
        return True
    if message_type != "tts.speak":
        return False
    data_record = cast(dict[str, object], data)
    text = data_record.get("text")
    if not isinstance(text, str):
        return False
    normalized = text.strip()
    if not normalized or len(normalized) > _MAX_TRANSCRIPT_CHARS:
        return False
    await speak(normalized)
    return True
