"""Local-audio processors for terminal voice sessions."""

from __future__ import annotations

import math
from array import array
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, cast

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from kassette.terminal_protocol import envelope

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


def pcm_level(audio: bytes) -> float:
    """Return normalized RMS for mono signed 16-bit PCM."""
    if len(audio) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(audio[: len(audio) - len(audio) % 2])
    if not samples:
        return 0.0
    mean_square = sum(float(sample) * float(sample) for sample in samples) / len(samples)
    return min(1.0, math.sqrt(mean_square) / 32_768.0)


class _LevelProcessor(FrameProcessor):
    def __init__(self, direction: str, sink: EventSink, *, name: str) -> None:
        super().__init__(name=name)  # pyright: ignore[reportUnknownMemberType]
        self._level_direction = direction
        self._sink = sink
        self._last_level_at = 0.0

    async def _report(self, audio: bytes) -> None:
        now = monotonic()
        if now - self._last_level_at < 0.05:
            return
        self._last_level_at = now
        await self._sink(
            envelope(
                "audio.level",
                {"direction": self._level_direction, "level": round(pcm_level(audio), 4)},
            )
        )


class TerminalInputProcessor(_LevelProcessor):
    """Publish real microphone levels while preserving input frames."""

    def __init__(self, sink: EventSink) -> None:
        super().__init__("input", sink, name="TerminalInputProcessor")
        self.paused = False

    async def set_paused(self, paused: bool) -> None:
        if self.paused == paused:
            return
        self.paused = paused
        if paused:
            self._last_level_at = 0.0
            await self._sink(envelope("audio.level", {"direction": "input", "level": 0.0}))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame) and not self.paused:
            await self._report(frame.audio)
        await self.push_frame(frame, direction)


class TerminalOutputProcessor(_LevelProcessor):
    """Publish playback levels, bridge app messages, and enforce output mute."""

    def __init__(self, sink: EventSink) -> None:
        super().__init__("output", sink, name="TerminalOutputProcessor")
        self.muted = False

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, OutputTransportMessageUrgentFrame):
            message = frame.message
            if isinstance(message, dict):
                candidate = cast(dict[str, object], message)
                if (
                    candidate.get("label") == "kassette"
                    and isinstance(candidate.get("type"), str)
                    and isinstance(candidate.get("data"), dict)
                ):
                    await self._sink(cast(dict[str, Any], candidate))
            return
        if isinstance(frame, OutputAudioRawFrame):
            await self._report(frame.audio)
            if self.muted:
                return
        await self.push_frame(frame, direction)
