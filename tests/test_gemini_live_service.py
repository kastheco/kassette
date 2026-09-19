# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from typing import Any

from google.genai.types import FunctionResponse
from pipecat.frames.frames import Frame, OutputTransportMessageUrgentFrame
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.llm_service import FunctionCallParams

from kassette.domain import SessionEvent, SessionEventType, SessionState
from kassette.providers.builtin import build_builtin_provider_registry
from kassette.providers.gemini_live.service import (
    CONSULT_TOOL_NAME,
    DelegatedGeminiLiveService,
)
from kassette.sessions import SessionRegistry
from kassette.settings import KassetteSettings


class FakeGeminiSession:
    def __init__(self) -> None:
        self.responses: list[FunctionResponse] = []

    async def send_tool_response(self, *, function_responses: FunctionResponse) -> None:
        self.responses.append(function_responses)


class RecordingGeminiLiveService(DelegatedGeminiLiveService):
    def __init__(self, **kwargs: Any) -> None:
        self.frames: list[Frame] = []
        super().__init__(**kwargs)

    async def push_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        self.frames.append(frame)


def _params(
    service: DelegatedGeminiLiveService,
    callback: Any,
) -> FunctionCallParams:
    return FunctionCallParams(
        function_name=CONSULT_TOOL_NAME,
        tool_call_id="call-1",
        arguments={"request": "inspect this repository"},
        llm=service,
        pipeline_worker=None,  # type: ignore[arg-type]
        context=None,  # type: ignore[arg-type]
        result_callback=callback,
    )


def test_gemini_live_models_are_selectable_through_settings() -> None:
    standard = KassetteSettings.model_validate(
        {
            "KASSETTE_VOICE_BACKEND": "gemini-live",
            "GOOGLE_API_KEY": "test-key",
            "KASSETTE_GEMINI_LIVE_MODEL": "gemini-3.8-live",
        }
    )
    extended = KassetteSettings.model_validate(
        {
            "KASSETTE_VOICE_BACKEND": "gemini-live",
            "GOOGLE_API_KEY": "test-key",
            "KASSETTE_GEMINI_LIVE_MODEL": "gemini-3.8-live-extended-thinking",
            "KASSETTE_GEMINI_LIVE_THINKING_LEVEL": "high",
        }
    )

    assert standard.gemini_live_model == "gemini-3.8-live"
    assert extended.gemini_live_model == "gemini-3.8-live-extended-thinking"
    assert extended.gemini_live_thinking_level == "high"
    registry = build_builtin_provider_registry(standard, session_registry=SessionRegistry())
    provider = registry.resolve("gemini-live")
    assert provider is not None
    assert provider.capabilities.credential_readiness.value == "ready"


async def test_gemini_live_accepts_audio_without_an_llm_context() -> None:
    async def discard(_: SessionEvent) -> None:
        return

    service = RecordingGeminiLiveService(
        session_id="voice-1",
        api_key="test-key",
        model="gemini-3.8-live",
        event_sink=discard,
    )

    await service._handle_session_ready(object())  # pyright: ignore[reportPrivateUsage]

    assert service._ready_for_realtime_input


async def test_gemini_live_delegates_to_the_active_client() -> None:
    events: list[SessionEvent] = []
    results: list[object] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    async def result(value: object) -> None:
        results.append(value)

    service = RecordingGeminiLiveService(
        session_id="voice-1",
        api_key="test-key",
        model="gemini-3.8-live",
        event_sink=collect,
        publish_client_events=True,
    )
    session = FakeGeminiSession()
    service._session = session  # type: ignore[assignment]
    assert service.has_function(CONSULT_TOOL_NAME)

    delegation = asyncio.create_task(service._delegate(_params(service, result)))
    await asyncio.sleep(0)

    assert [(event.type, event.state) for event in events] == [
        (SessionEventType.SESSION_STATE_CHANGED, SessionState.THINKING),
        (SessionEventType.DELEGATION_REQUESTED, None),
    ]
    assert events[-1].text == "inspect this repository"
    assert events[-1].metadata == {"delegation_id": "call-1"}
    message = next(
        frame for frame in service.frames if isinstance(frame, OutputTransportMessageUrgentFrame)
    )
    assert message.message == {
        "label": "kassette",
        "type": "delegation.requested",
        "data": {
            "session_id": "voice-1",
            "delegation_id": "call-1",
            "text": "inspect this repository",
            "sequence": 1,
        },
    }

    assert await service.handle_client_message(
        {
            "label": "kassette",
            "type": "delegation.complete",
            "data": {"delegation_id": "call-1", "text": "The client answer."},
        }
    )
    await delegation

    assert results == []
    assert len(session.responses) == 1
    response = session.responses[0]
    assert response.id == "call-1"
    assert response.name == CONSULT_TOOL_NAME
    assert response.response == {"response": "The client answer."}


async def test_gemini_live_rejects_unknown_or_invalid_delegation_results() -> None:
    async def discard(_: SessionEvent) -> None:
        return

    service = RecordingGeminiLiveService(
        session_id="voice-1",
        api_key="test-key",
        model="gemini-3.8-live-extended-thinking",
        thinking_level="high",
        event_sink=discard,
    )

    assert not await service.handle_client_message(
        {
            "label": "kassette",
            "type": "delegation.complete",
            "data": {"delegation_id": "missing", "text": "answer"},
        }
    )
    assert service._settings.model == "gemini-3.8-live-extended-thinking"
    assert service._supports_non_blocking_tools
    assert service._function_is_async(CONSULT_TOOL_NAME)
