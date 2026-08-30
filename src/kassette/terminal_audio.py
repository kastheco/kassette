"""Local-audio processors for terminal voice sessions."""

from __future__ import annotations

import math
from array import array
from collections.abc import Awaitable, Callable
from time import monotonic
from typing import Any, Literal, cast

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.local.audio import LocalAudioTransport, LocalAudioTransportParams

from kassette.terminal_protocol import envelope

EventSink = Callable[[dict[str, Any]], Awaitable[None]]
DeviceDirection = Literal["input", "output"]


def select_audio_device_index(
    devices: list[dict[str, Any]],
    requested_name: str,
    direction: DeviceDirection,
) -> int | None:
    """Select a capable audio device by stable name, preferring exact matches."""
    needle = requested_name.strip().casefold()
    channel_key = "maxInputChannels" if direction == "input" else "maxOutputChannels"
    capable = [device for device in devices if int(device.get(channel_key, 0)) > 0]
    exact = [device for device in capable if str(device.get("name", "")).casefold() == needle]
    matches = exact or [
        device for device in capable if needle in str(device.get("name", "")).casefold()
    ]
    if not matches:
        return None
    return int(matches[0]["index"])


class StableLocalAudioTransport(LocalAudioTransport):
    """Resolve named devices against the same PortAudio instance that opens them."""

    def __init__(
        self,
        params: LocalAudioTransportParams,
        *,
        input_name: str | None = None,
        output_name: str | None = None,
    ) -> None:
        super().__init__(params)
        if input_name is None and output_name is None:
            return
        devices = [
            cast(dict[str, Any], self._pyaudio.get_device_info_by_index(index))
            for index in range(self._pyaudio.get_device_count())
        ]
        if input_name is not None:
            input_index = select_audio_device_index(devices, input_name, "input")
            if input_index is None:
                raise ValueError(f"input audio device not found: {input_name}")
            self._params.input_device_index = input_index
        if output_name is not None:
            output_index = select_audio_device_index(devices, output_name, "output")
            if output_index is None:
                raise ValueError(f"output audio device not found: {output_name}")
            self._params.output_device_index = output_index


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
        self.output_active = False

    @property
    def blocked(self) -> bool:
        return self.paused or self.output_active

    async def set_paused(self, paused: bool) -> None:
        was_blocked = self.blocked
        self.paused = paused
        await self._publish_blocked_transition(was_blocked)

    async def set_output_active(self, active: bool) -> None:
        was_blocked = self.blocked
        self.output_active = active
        await self._publish_blocked_transition(was_blocked)

    async def _publish_blocked_transition(self, was_blocked: bool) -> None:
        if self.blocked and not was_blocked:
            self._last_level_at = 0.0
            await self._sink(envelope("audio.level", {"direction": "input", "level": 0.0}))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            if self.blocked:
                return
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
