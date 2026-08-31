from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterimTranscriptionFrame,
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    VADUserStartedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection

from kassette.domain import SessionEvent, SessionEventType, SessionState
from kassette.providers.cascade import (
    CascadedBargeInProcessor,
    CascadedVoiceEvents,
    handle_client_message,
)
from kassette.settings import load_settings


class RecordingCascadedVoiceEvents(CascadedVoiceEvents):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pushed_frames: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        self.pushed_frames.append((frame, direction))

    async def _start_interruption(self) -> None:
        return


class RecordingBargeInProcessor(CascadedBargeInProcessor):
    def __init__(self) -> None:
        super().__init__()
        self.interruptions = 0
        self.trace: list[str] = []

    async def broadcast_interruption(self) -> None:
        self.interruptions += 1
        self.trace.append("interrupt")

    async def push_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        self.trace.append(type(frame).__name__)


def _messages(processor: RecordingCascadedVoiceEvents) -> list[dict[str, Any]]:
    return [
        frame.message
        for frame, _direction in processor.pushed_frames
        if isinstance(frame, OutputTransportMessageUrgentFrame)
    ]


async def test_vad_user_start_is_published_before_transcript_text() -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    processor = RecordingCascadedVoiceEvents(session_id="voice-1", event_sink=collect)
    await processor.process_frame(
        VADUserStartedSpeakingFrame(),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        InterimTranscriptionFrame("continuing", "owner", "now"),
        FrameDirection.DOWNSTREAM,
    )

    assert [event.type for event in events] == [
        SessionEventType.INPUT_AUDIO_STARTED,
        SessionEventType.TRANSCRIPT_DELTA,
    ]


async def test_transcript_updates_share_a_turn_until_final() -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    processor = RecordingCascadedVoiceEvents(session_id="voice-1", event_sink=collect)
    await processor.process_frame(
        InterimTranscriptionFrame("hello", "owner", "now"),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        InterimTranscriptionFrame("hello there", "owner", "now"),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        TranscriptionFrame("hello there", "owner", "now", finalized=True),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        InterimTranscriptionFrame("next", "owner", "later"),
        FrameDirection.DOWNSTREAM,
    )

    messages = _messages(processor)
    assert [message["type"] for message in messages] == [
        "transcript.delta",
        "transcript.delta",
        "transcript.final",
        "transcript.delta",
    ]
    assert [message["data"]["sequence"] for message in messages] == [1, 2, 3, 4]
    assert {message["data"]["turn_id"] for message in messages[:3]} == {"voice-1:1"}
    assert messages[3]["data"]["turn_id"] == "voice-1:2"
    assert messages[2]["data"]["final"] is True
    assert [event.type for event in events] == [
        SessionEventType.TRANSCRIPT_DELTA,
        SessionEventType.TRANSCRIPT_DELTA,
        SessionEventType.TRANSCRIPT_FINAL,
        SessionEventType.TRANSCRIPT_DELTA,
    ]


async def test_pausing_input_closes_the_active_transcript_segment() -> None:
    processor = RecordingCascadedVoiceEvents(session_id="voice-1")

    await processor.process_frame(
        InterimTranscriptionFrame("first part", "user-1", "now"),
        FrameDirection.DOWNSTREAM,
    )
    await processor.publish_input_state(paused=True)
    await processor.process_frame(
        InterimTranscriptionFrame("second part", "user-1", "later"),
        FrameDirection.DOWNSTREAM,
    )

    turn_ids = [
        message["data"]["turn_id"]
        for message in _messages(processor)
        if message["type"] == SessionEventType.TRANSCRIPT_DELTA.value
    ]
    assert turn_ids == ["voice-1:1", "voice-1:2"]


async def test_only_actual_playback_publishes_speaking_and_listening_states() -> None:
    processor = RecordingCascadedVoiceEvents(session_id="voice-1")

    await processor.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(TTSStoppedFrame(), FrameDirection.DOWNSTREAM)
    assert _messages(processor) == []

    await processor.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await processor.process_frame(BotStoppedSpeakingFrame(), FrameDirection.UPSTREAM)

    messages = _messages(processor)
    assert [message["data"]["state"] for message in messages] == [
        SessionState.SPEAKING.value,
        SessionState.LISTENING.value,
    ]


async def test_interruption_publishes_listening_state_and_lifecycle_event() -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    processor = RecordingCascadedVoiceEvents(session_id="voice-1", event_sink=collect)
    await processor.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)

    assert [message["data"]["state"] for message in _messages(processor)] == [
        SessionState.LISTENING.value
    ]
    assert [event.type for event in events] == [
        SessionEventType.INTERRUPTED,
        SessionEventType.SESSION_STATE_CHANGED,
    ]
    assert events[0].state is SessionState.INTERRUPTING


async def test_user_speech_interrupts_queued_and_active_tts_before_forwarding() -> None:
    processor = RecordingBargeInProcessor()
    queued_frames: list[Frame] = []

    async def enqueue(frame: Frame) -> None:
        queued_frames.append(frame)

    await processor.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert processor.interruptions == 0

    await processor.queue_speech("queued response", enqueue)
    assert isinstance(queued_frames.pop(), TTSSpeakFrame)
    await processor.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert processor.interruptions == 1
    assert processor.trace[-2:] == ["interrupt", "VADUserStartedSpeakingFrame"]

    await processor.queue_speech("active response", enqueue)
    await processor.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert processor.interruptions == 2
    assert processor.trace[-3:] == [
        "interrupt",
        "VADUserStartedSpeakingFrame",
        "VADUserStartedSpeakingFrame",
    ]

    async def reject(_frame: Frame) -> None:
        raise RuntimeError("queue rejected speech")

    with pytest.raises(RuntimeError, match="queue rejected speech"):
        await processor.queue_speech("rejected response", reject)
    await processor.process_frame(TTSStoppedFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)
    assert processor.interruptions == 2


async def test_user_speech_interrupts_audio_still_playing_after_tts_generation() -> None:
    processor = RecordingBargeInProcessor()
    queued_frames: list[Frame] = []

    async def enqueue(frame: Frame) -> None:
        queued_frames.append(frame)

    await processor.queue_speech("long response", enqueue)
    await processor.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)
    await processor.process_frame(TTSStoppedFrame(), FrameDirection.DOWNSTREAM)
    await processor.process_frame(VADUserStartedSpeakingFrame(), FrameDirection.DOWNSTREAM)

    assert processor.interruptions == 1
    assert processor.trace[-2:] == ["interrupt", "VADUserStartedSpeakingFrame"]


async def test_transcript_and_speech_events_can_run_on_opposite_sides_of_tts() -> None:
    transcript_events = RecordingCascadedVoiceEvents(
        session_id="voice-1",
        publish_speech=False,
    )
    speech_events = RecordingCascadedVoiceEvents(
        session_id="voice-1",
        publish_transcripts=False,
        publish_start_state=False,
    )
    transcript = InterimTranscriptionFrame("heard during playback", "owner", "now")

    await transcript_events.process_frame(transcript, FrameDirection.DOWNSTREAM)
    await transcript_events.process_frame(TTSStartedFrame(), FrameDirection.DOWNSTREAM)
    await speech_events.process_frame(transcript, FrameDirection.DOWNSTREAM)
    await speech_events.process_frame(BotStartedSpeakingFrame(), FrameDirection.UPSTREAM)

    assert [message["type"] for message in _messages(transcript_events)] == ["transcript.delta"]
    assert [message["type"] for message in _messages(speech_events)] == ["session.state_changed"]
    assert _messages(speech_events)[0]["data"]["state"] == SessionState.SPEAKING.value


async def test_client_voice_messages_control_tts_and_microphone_input() -> None:
    spoken: list[str] = []
    input_states: list[bool] = []

    async def speak(text: str) -> None:
        spoken.append(text)

    async def set_input_paused(paused: bool) -> None:
        input_states.append(paused)

    assert await handle_client_message(
        {"label": "kassette", "type": "tts.speak", "data": {"text": "  hello  "}},
        speak,
        set_input_paused=set_input_paused,
    )
    assert await handle_client_message(
        {"label": "kassette", "type": "input.pause", "data": {}},
        speak,
        set_input_paused=set_input_paused,
    )
    assert await handle_client_message(
        {"label": "kassette", "type": "input.resume", "data": {}},
        speak,
        set_input_paused=set_input_paused,
    )
    assert spoken == ["hello"]
    assert input_states == [True, False]
    assert not await handle_client_message(
        {"label": "kassette", "type": "tts.speak", "data": {"text": ""}},
        speak,
        set_input_paused=set_input_paused,
    )
    assert not await handle_client_message(
        {"label": "other", "type": "tts.speak", "data": {"text": "ignored"}},
        speak,
        set_input_paused=set_input_paused,
    )


def test_settings_default_vad_thresholds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "KASSETTE_VAD_STOP_SECS",
        "KASSETTE_VAD_MIN_VOLUME",
        "KASSETTE_INPUT_DEVICE_NAME",
        "KASSETTE_OUTPUT_DEVICE_NAME",
        "KASSETTE_TRANSCRIPT_GROOMING_PROFILE",
        "KASSETTE_TRANSCRIPT_GROOMING_TIMEOUT_SECS",
    ):
        monkeypatch.delenv(name, raising=False)

    settings = load_settings(env_file=tmp_path / "missing.env")

    assert settings.vad_stop_secs == 1.8
    assert settings.vad_min_volume == 0.1
    assert settings.transcript_grooming_profile is None
    assert settings.transcript_grooming_timeout_secs == 0.5


def test_settings_select_openai_transcription_for_cascade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "KASSETTE_TRANSCRIPTION_PROVIDER",
        "KASSETTE_OPENAI_TRANSCRIPTION_MODEL",
        "OPENAI_API_KEY",
        "FISH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "KASSETTE_TRANSCRIPTION_PROVIDER=openai\n"
        "KASSETTE_OPENAI_TRANSCRIPTION_MODEL=gpt-live-transcribe\n"
        "OPENAI_API_KEY=openai-secret\n"
        "FISH_API_KEY=fish-secret\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    transcription_key, fish_key = settings.cascade_credentials()
    assert settings.transcription_provider == "openai"
    assert settings.openai_transcription_model == "gpt-live-transcribe"
    assert transcription_key == "openai-secret"
    assert fish_key == "fish-secret"
    assert "openai-secret" not in repr(settings)


def test_settings_load_gitignored_dotenv_without_exposing_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "FISH_API_KEY",
        "FISH_MODEL",
        "FISH_VOICE_ID",
        "KASSETTE_VAD_STOP_SECS",
        "KASSETTE_VAD_MIN_VOLUME",
        "KASSETTE_TRANSCRIPT_GROOMING_PROFILE",
        "KASSETTE_TRANSCRIPT_GROOMING_TIMEOUT_SECS",
        "ELEVENLABS_API_KEY",
        "ELEVENLABS_VOICE_ID",
    ):
        monkeypatch.delenv(name, raising=False)

    env_file = tmp_path / ".env"
    env_file.write_text(
        "GOOGLE_API_KEY=google-secret\n"
        "FISH_API_KEY=fish-secret\n"
        "FISH_MODEL=s2.1-pro\n"
        "FISH_VOICE_ID=voice-1\n"
        "KASSETTE_VAD_STOP_SECS=1.25\n"
        "KASSETTE_VAD_MIN_VOLUME=0.2\n"
        "KASSETTE_INPUT_DEVICE_NAME=pulse\n"
        "KASSETTE_OUTPUT_DEVICE_NAME=pulse\n"
        "KASSETTE_TRANSCRIPT_GROOMING_PROFILE=/tmp/grooming.json\n"
        "KASSETTE_TRANSCRIPT_GROOMING_TIMEOUT_SECS=0.75\n"
        "ELEVENLABS_API_KEY=eleven-secret\n"
        "ELEVENLABS_VOICE_ID=eleven-voice-1\n",
        encoding="utf-8",
    )

    settings = load_settings(env_file=env_file)

    google_api_key, fish_api_key = settings.cascade_credentials()
    assert google_api_key == "google-secret"
    assert fish_api_key == "fish-secret"
    assert settings.fish_voice_id == "voice-1"
    assert settings.vad_stop_secs == 1.25
    assert settings.vad_min_volume == 0.2
    assert settings.input_device_name == "pulse"
    assert settings.output_device_name == "pulse"
    assert settings.transcript_grooming_profile == Path("/tmp/grooming.json")
    assert settings.transcript_grooming_timeout_secs == 0.75
    eleven_key, eleven_voice, comparison_fish_key = settings.comparison_credentials()
    assert eleven_key == "eleven-secret"
    assert eleven_voice == "eleven-voice-1"
    assert comparison_fish_key == "fish-secret"
    assert "google-secret" not in repr(settings)
    assert "fish-secret" not in repr(settings)
    assert "eleven-secret" not in repr(settings)
