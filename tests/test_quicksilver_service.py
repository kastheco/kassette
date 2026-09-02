from __future__ import annotations

from typing import Any, cast

import pytest
from pipecat.frames.frames import Frame, OutputTransportMessageUrgentFrame
from pipecat.processors.frame_processor import FrameDirection

from kassette.credentials import CodexCredentialProvider
from kassette.providers.quicksilver.service import GPTLiveService
from kassette.sessions import SessionRegistry


class RecordingGPTLiveService(GPTLiveService):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pushed_frames: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        self.pushed_frames.append((frame, direction))


@pytest.mark.parametrize(
    ("message_type", "paused"),
    [("input.pause", True), ("input.resume", False)],
)
async def test_client_input_controls_acknowledge_their_state(
    message_type: str,
    paused: bool,
) -> None:
    service = RecordingGPTLiveService(
        session_id="voice-1",
        registry=SessionRegistry(),
        credentials=cast(CodexCredentialProvider, object()),
        manage_session_lifecycle=False,
        client_delegation=True,
        publish_client_events=True,
    )

    accepted = await service.handle_client_message(
        {"label": "kassette", "type": message_type, "data": {}}
    )

    assert accepted is True
    messages = [
        frame.message
        for frame, _ in service.pushed_frames
        if isinstance(frame, OutputTransportMessageUrgentFrame)
    ]
    assert messages == [
        {
            "label": "kassette",
            "type": "input.state_changed",
            "data": {"session_id": "voice-1", "paused": paused, "sequence": 1},
        }
    ]
