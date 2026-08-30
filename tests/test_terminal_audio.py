from __future__ import annotations

import struct
from unittest.mock import AsyncMock

import pytest
from pipecat.frames.frames import InputAudioRawFrame, OutputTransportMessageUrgentFrame
from pipecat.processors.frame_processor import FrameDirection

from kassette.terminal_audio import (
    TerminalInputProcessor,
    TerminalOutputProcessor,
    pcm_level,
    select_audio_device_index,
)


def test_select_audio_device_index_prefers_stable_exact_capable_name() -> None:
    devices = [
        {"index": 15, "name": "pipewire", "maxInputChannels": 128, "maxOutputChannels": 128},
        {"index": 16, "name": "pulse", "maxInputChannels": 32, "maxOutputChannels": 32},
        {"index": 17, "name": "default", "maxInputChannels": 128, "maxOutputChannels": 128},
        {"index": 23, "name": "Pulse Monitor", "maxInputChannels": 2, "maxOutputChannels": 0},
    ]

    assert select_audio_device_index(devices, "pulse", "input") == 16
    assert select_audio_device_index(devices, "PULSE", "output") == 16
    assert select_audio_device_index(devices, "monitor", "input") == 23
    assert select_audio_device_index(devices, "missing", "output") is None


def test_pcm_level_reports_silence_and_normalized_signal() -> None:
    assert pcm_level(b"\x00\x00" * 40) == 0.0

    samples = struct.pack("<40h", *([16_384] * 40))
    assert pcm_level(samples) == 0.5


async def test_output_activity_blocks_microphone_frames_until_playback_stops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[dict[str, object]] = []

    async def send(event: dict[str, object]) -> None:
        events.append(event)

    processor = TerminalInputProcessor(send)
    push_frame = AsyncMock()
    monkeypatch.setattr(processor, "push_frame", push_frame)
    frame = InputAudioRawFrame(audio=b"\xff\x7f" * 40, sample_rate=16_000, num_channels=1)

    await processor.set_output_active(True)
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert processor.blocked
    push_frame.assert_not_awaited()
    assert events[-1]["data"] == {"direction": "input", "level": 0.0}

    await processor.set_output_active(False)
    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert not processor.blocked
    push_frame.assert_awaited_once_with(frame, FrameDirection.DOWNSTREAM)


async def test_paused_terminal_input_resets_level_and_suppresses_future_telemetry() -> None:
    events: list[dict[str, object]] = []

    async def send(event: dict[str, object]) -> None:
        events.append(event)

    processor = TerminalInputProcessor(send)
    await processor.set_paused(True)
    await processor.set_paused(True)

    assert processor.paused
    assert events == [
        {
            "label": "kassette",
            "type": "audio.level",
            "data": {"direction": "input", "level": 0.0},
        }
    ]

    events.clear()
    await processor.process_frame(
        InputAudioRawFrame(audio=b"\xff\x7f" * 40, sample_rate=16_000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    assert events == []


async def test_terminal_output_forwards_only_kassette_envelopes() -> None:
    events: list[dict[str, object]] = []

    async def send(event: dict[str, object]) -> None:
        events.append(event)

    processor = TerminalOutputProcessor(send)
    await processor.process_frame(
        OutputTransportMessageUrgentFrame(
            message={"label": "rtvi-ai", "type": "metrics", "data": {}}
        ),
        FrameDirection.DOWNSTREAM,
    )
    await processor.process_frame(
        OutputTransportMessageUrgentFrame(
            message={"label": "kassette", "type": "speech.started", "data": {}}
        ),
        FrameDirection.DOWNSTREAM,
    )

    assert events == [{"label": "kassette", "type": "speech.started", "data": {}}]


def test_pcm_level_clamps_full_scale() -> None:
    samples = struct.pack("<4h", -32_768, 32_767, -32_768, 32_767)

    assert 0.99 <= pcm_level(samples) <= 1.0
