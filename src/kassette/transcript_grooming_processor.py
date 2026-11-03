"""Pipecat frame processor that applies a transcript groomer after STT."""

from __future__ import annotations

from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from kassette.transcript_grooming import (
    TranscriptGroomer,
    TranscriptGroomingRequest,
    groom_transcript,
)


class TranscriptGroomingProcessor(FrameProcessor):
    """Apply a groomer after STT and before normalized transcript events."""

    def __init__(
        self,
        groomer: TranscriptGroomer,
        *,
        timeout_secs: float = 0.5,
        name: str | None = None,
        enable_direct_mode: bool = False,
    ) -> None:
        if timeout_secs <= 0:
            raise ValueError("timeout_secs must be positive")
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            name=name,
            enable_direct_mode=enable_direct_mode,
        )
        self._groomer = groomer
        self._timeout_secs = timeout_secs

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction is FrameDirection.DOWNSTREAM and isinstance(
            frame,
            (InterimTranscriptionFrame, TranscriptionFrame),
        ):
            await self._groom_frame(frame)
        await self.push_frame(frame, direction)

    async def _groom_frame(
        self,
        frame: InterimTranscriptionFrame | TranscriptionFrame,
    ) -> None:
        frame.text = await groom_transcript(
            self._groomer,
            TranscriptGroomingRequest(
                text=frame.text,
                final=isinstance(frame, TranscriptionFrame),
                language=str(frame.language) if frame.language is not None else None,
            ),
            timeout_secs=self._timeout_secs,
        )
