from __future__ import annotations

import struct

from pipecat.frames.frames import InputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from kassette.terminal_audio import TerminalInputProcessor, pcm_level


def test_pcm_level_reports_silence_and_normalized_signal() -> None:
    assert pcm_level(b"\x00\x00" * 40) == 0.0

    samples = struct.pack("<40h", *([16_384] * 40))
    assert pcm_level(samples) == 0.5


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


def test_pcm_level_clamps_full_scale() -> None:
    samples = struct.pack("<4h", -32_768, 32_767, -32_768, 32_767)

    assert 0.99 <= pcm_level(samples) <= 1.0
