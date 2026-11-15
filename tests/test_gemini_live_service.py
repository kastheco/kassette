# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import pytest
from google.genai.types import (
    FunctionResponse,
    LiveConnectConfig,
    LiveServerContent,
    LiveServerMessage,
    LiveServerToolCall,
    Transcription,
)
from loguru import logger
from pipecat.frames.frames import (
    Frame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSTextFrame,
)
from pipecat.processors.frame_processor import FrameCallback, FrameDirection, FrameProcessor
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallParams

from kassette.domain import SessionEvent, SessionEventType, SessionState, TranscriptRole
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
        self.directions: list[FrameDirection] = []
        super().__init__(**kwargs)

    async def push_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        self.frames.append(frame)
        self.directions.append(direction)


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


async def test_live_transcripts_stream_and_settle_without_logging() -> None:
    events: list[SessionEvent] = []
    logs: list[str] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    service = RecordingGeminiLiveService(
        session_id="voice-1",
        api_key="test-key",
        model="gemini-3.8-live-extended-thinking",
        event_sink=collect,
    )
    sink = logger.add(lambda message: logs.append(str(message)), level="TRACE")
    try:
        for text in ("private words", " continue."):
            await service._handle_msg_input_transcription(
                LiveServerMessage(
                    server_content=LiveServerContent(input_transcription=Transcription(text=text))
                )
            )
        for text in ("spoken ", "answer"):
            await service._handle_msg_output_transcription(
                LiveServerMessage(
                    server_content=LiveServerContent(output_transcription=Transcription(text=text))
                )
            )
        await service._handle_msg_turn_complete(
            LiveServerMessage(server_content=LiveServerContent(turn_complete=True))
        )
    finally:
        logger.remove(sink)

    assert [(e.type, e.role, e.text, e.metadata) for e in events] == [
        (
            SessionEventType.TRANSCRIPT_DELTA,
            TranscriptRole.USER,
            "private words",
            {"turn_id": "voice-1:1"},
        ),
        (
            SessionEventType.TRANSCRIPT_DELTA,
            TranscriptRole.USER,
            "private words continue.",
            {"turn_id": "voice-1:1"},
        ),
        (
            SessionEventType.TRANSCRIPT_FINAL,
            TranscriptRole.USER,
            "private words continue.",
            {"turn_id": "voice-1:1"},
        ),
        (SessionEventType.TRANSCRIPT_DELTA, TranscriptRole.ASSISTANT, "spoken ", {}),
        (SessionEventType.TRANSCRIPT_DELTA, TranscriptRole.ASSISTANT, "spoken answer", {}),
        (SessionEventType.TRANSCRIPT_FINAL, TranscriptRole.ASSISTANT, "spoken answer", {}),
    ]
    assert not any("private words" in line or "spoken answer" in line for line in logs)


async def test_finished_transcription_closes_each_role_once() -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    service = RecordingGeminiLiveService(
        session_id="voice-1", api_key="test-key", model="gemini-3.8-live", event_sink=collect
    )
    await service._handle_msg_input_transcription(
        LiveServerMessage(
            server_content=LiveServerContent(
                input_transcription=Transcription(text="hello", finished=True)
            )
        )
    )
    await service._handle_msg_output_transcription(
        LiveServerMessage(
            server_content=LiveServerContent(
                output_transcription=Transcription(text="hi", finished=True)
            )
        )
    )
    await service._handle_msg_turn_complete(
        LiveServerMessage(server_content=LiveServerContent(turn_complete=True))
    )
    assert [(e.type, e.role, e.text) for e in events] == [
        (SessionEventType.TRANSCRIPT_DELTA, TranscriptRole.USER, "hello"),
        (SessionEventType.TRANSCRIPT_FINAL, TranscriptRole.USER, "hello"),
        (SessionEventType.TRANSCRIPT_DELTA, TranscriptRole.ASSISTANT, "hi"),
        (SessionEventType.TRANSCRIPT_FINAL, TranscriptRole.ASSISTANT, "hi"),
    ]


async def _dispatch(service: RecordingGeminiLiveService, messages: list[LiveServerMessage]) -> None:
    class Connection:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return

        async def receive(self) -> AsyncIterator[LiveServerMessage]:
            for message in messages:
                yield message
            service._disconnecting = True
            raise RuntimeError("end of fake stream")

    def connect(**_kwargs: object) -> Connection:
        return Connection()

    service._client = SimpleNamespace(  # type: ignore[assignment]
        aio=SimpleNamespace(live=SimpleNamespace(connect=connect))
    )
    service._disconnecting = False
    await service._connection_task_handler(LiveConnectConfig())


def _input(text: str, *, finished: bool = False) -> LiveServerMessage:
    return LiveServerMessage(
        server_content=LiveServerContent(
            input_transcription=Transcription(text=text, finished=finished)
        )
    )


def _output(text: str, *, complete: bool = False) -> LiveServerMessage:
    return LiveServerMessage(
        server_content=LiveServerContent(
            output_transcription=Transcription(text=text), turn_complete=complete
        )
    )


def _assert_turns_settled(events: list[SessionEvent]) -> None:
    deltas = {
        event.metadata["turn_id"]
        for event in events
        if event.role == TranscriptRole.USER and event.type == SessionEventType.TRANSCRIPT_DELTA
    }
    finals = [
        event.metadata["turn_id"]
        for event in events
        if event.role == TranscriptRole.USER and event.type == SessionEventType.TRANSCRIPT_FINAL
    ]
    assert len(finals) == len(set(finals))
    assert set(finals) == deltas


async def test_dispatch_web_captions_and_late_input_without_logging() -> None:
    events: list[SessionEvent] = []
    logs: list[str] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    service = RecordingGeminiLiveService(
        session_id="voice",
        api_key="test-key",
        model="gemini-3.8-live",
        event_sink=collect,
        publish_client_events=True,
    )
    service._user_settle_secs = 0.01
    sink = logger.add(lambda message: logs.append(str(message)), level="TRACE")
    try:
        await _dispatch(
            service,
            [_input("private words"), _output("spoken reply", complete=True), _input(" tail")],
        )
        await asyncio.sleep(0.05)
    finally:
        logger.remove(sink)
    user_frames = [
        (frame.text, direction)
        for frame, direction in zip(service.frames, service.directions, strict=True)
        if isinstance(frame, TranscriptionFrame)
    ]
    assert user_frames == [
        ("private words", FrameDirection.UPSTREAM),
        ("tail", FrameDirection.UPSTREAM),
    ]
    assert any(
        isinstance(frame, TTSTextFrame) and frame.text == "spoken reply" for frame in service.frames
    )
    user = [event for event in events if event.role == TranscriptRole.USER]
    assert [(event.type, event.text, event.metadata["turn_id"]) for event in user] == [
        (SessionEventType.TRANSCRIPT_DELTA, "private words", "voice:1"),
        (SessionEventType.TRANSCRIPT_FINAL, "private words", "voice:1"),
        (SessionEventType.TRANSCRIPT_DELTA, "tail", "voice:2"),
        (SessionEventType.TRANSCRIPT_FINAL, "tail", "voice:2"),
    ]
    _assert_turns_settled(events)
    assert not any("private words" in line or "spoken reply" in line for line in logs)


async def test_linked_web_frames_keep_captions_out_of_trace_logs() -> None:
    logs: list[str] = []
    frames: list[Frame] = []

    class Receiver(FrameProcessor):
        async def queue_frame(
            self,
            frame: Frame,
            direction: FrameDirection = FrameDirection.DOWNSTREAM,
            callback: FrameCallback | None = None,
        ) -> None:
            frames.append(frame)

    async def collect(_event: SessionEvent) -> None:
        return

    service = DelegatedGeminiLiveService(
        session_id="voice",
        api_key="test-key",
        model="gemini-3.8-live",
        event_sink=collect,
        publish_client_events=True,
    )
    upstream, downstream = Receiver(), Receiver()
    upstream.link(service)
    service.link(downstream)
    sink = logger.add(lambda message: logs.append(str(message)), level="TRACE")
    try:
        await service._handle_msg_input_transcription(_input("private input", finished=True))
        await service._handle_msg_output_transcription(_output("private output", complete=True))
    finally:
        logger.remove(sink)
    assert any(
        isinstance(frame, TranscriptionFrame) and frame.text == "private input" for frame in frames
    )
    assert any(
        isinstance(frame, TTSTextFrame) and frame.text == "private output" for frame in frames
    )
    assert not any("private input" in line or "private output" in line for line in logs)


async def test_new_fragment_invalidates_deadline_before_slow_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[SessionEvent] = []
    deadlines: asyncio.Queue[asyncio.Future[None]] = asyncio.Queue()
    delivering = asyncio.Event()
    delivered = asyncio.Event()
    yield_control = asyncio.sleep

    async def wait_for_deadline(_seconds: float) -> None:
        deadline: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        await deadlines.put(deadline)
        await deadline

    async def collect(event: SessionEvent) -> None:
        events.append(event)
        if event.type == SessionEventType.TRANSCRIPT_DELTA and event.text == "first second":
            delivering.set()
            await delivered.wait()

    monkeypatch.setattr(asyncio, "sleep", wait_for_deadline)
    service = RecordingGeminiLiveService(
        session_id="voice", api_key="test-key", model="gemini-3.8-live", event_sink=collect
    )
    await service._handle_msg_input_transcription(_input("first"))
    old_deadline = await deadlines.get()
    delivery = asyncio.create_task(service._handle_msg_input_transcription(_input(" second")))
    try:
        await delivering.wait()
        if not old_deadline.done():
            old_deadline.set_result(None)
        await yield_control(0)
        assert [event.type for event in events] == [
            SessionEventType.TRANSCRIPT_DELTA,
            SessionEventType.TRANSCRIPT_DELTA,
        ]
        delivered.set()
        await delivery
        new_deadline = await deadlines.get()
        assert all(event.type != SessionEventType.TRANSCRIPT_FINAL for event in events)
        new_deadline.set_result(None)
        await yield_control(0)
        assert (events[-1].type, events[-1].text, events[-1].metadata) == (
            SessionEventType.TRANSCRIPT_FINAL,
            "first second",
            {"turn_id": "voice:1"},
        )
        _assert_turns_settled(events)
    finally:
        delivered.set()
        await delivery
        await service.cleanup()


async def test_unanswered_new_turn_settles_after_turn_complete() -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    service = RecordingGeminiLiveService(
        session_id="voice", api_key="test-key", model="gemini-3.8-live", event_sink=collect
    )
    service._user_settle_secs = 0.01
    await _dispatch(service, [_input("first"), _output("answer", complete=True)])
    await service._handle_msg_input_transcription(_input("next"))
    await asyncio.sleep(0.05)
    user = [event for event in events if event.role == TranscriptRole.USER]
    assert [(event.type, event.text, event.metadata["turn_id"]) for event in user] == [
        (SessionEventType.TRANSCRIPT_DELTA, "first", "voice:1"),
        (SessionEventType.TRANSCRIPT_FINAL, "first", "voice:1"),
        (SessionEventType.TRANSCRIPT_DELTA, "next", "voice:2"),
        (SessionEventType.TRANSCRIPT_FINAL, "next", "voice:2"),
    ]
    _assert_turns_settled(events)


async def test_dispatch_interruption_and_reconnect_clear_turns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    async def broadcast(_service: GeminiLiveLLMService) -> None:
        return

    monkeypatch.setattr(GeminiLiveLLMService, "broadcast_interruption", broadcast)
    service = RecordingGeminiLiveService(
        session_id="voice", api_key="test-key", model="gemini-3.8-live", event_sink=collect
    )
    service._user_settle_secs = 0.01
    await _dispatch(
        service,
        [
            _input("first"),
            _output("old answer"),
            LiveServerMessage(
                server_content=LiveServerContent(interrupted=True, turn_complete=True)
            ),
            _input("second"),
            _output("partial answer"),
        ],
    )
    await service._handle_session_ready(object())
    await _dispatch(service, [_input("third", finished=True), _output("new answer", complete=True)])
    finals = [
        (event.role, event.text)
        for event in events
        if event.type == SessionEventType.TRANSCRIPT_FINAL
    ]
    assert finals == [
        (TranscriptRole.USER, "first"),
        (TranscriptRole.ASSISTANT, "old answer"),
        (TranscriptRole.USER, "second"),
        (TranscriptRole.ASSISTANT, "partial answer"),
        (TranscriptRole.USER, "third"),
        (TranscriptRole.ASSISTANT, "new answer"),
    ]
    _assert_turns_settled(events)


async def test_interleaved_input_and_output_start_distinct_turns() -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    service = RecordingGeminiLiveService(
        session_id="voice", api_key="test-key", model="gemini-3.8-live", event_sink=collect
    )
    await _dispatch(
        service,
        [
            _input("hi"),
            _output("one "),
            _input("wait"),
            _output("two", complete=True),
        ],
    )
    user_finals = [
        (event.text, event.metadata["turn_id"])
        for event in events
        if event.role == TranscriptRole.USER and event.type == SessionEventType.TRANSCRIPT_FINAL
    ]
    assert user_finals == [("hi", "voice:1"), ("wait", "voice:2")]
    assert any(
        event.role == TranscriptRole.ASSISTANT
        and event.text == "one two"
        and event.type == SessionEventType.TRANSCRIPT_FINAL
        for event in events
    )
    _assert_turns_settled(events)


async def test_tool_call_closes_turn_before_late_input(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    async def handle_tool(_service: GeminiLiveLLMService, _message: LiveServerMessage) -> None:
        return

    monkeypatch.setattr(GeminiLiveLLMService, "_handle_msg_tool_call", handle_tool)
    service = RecordingGeminiLiveService(
        session_id="voice", api_key="test-key", model="gemini-3.8-live", event_sink=collect
    )
    service._user_settle_secs = 0.01
    await _dispatch(
        service,
        [
            _input("request"),
            LiveServerMessage(tool_call=LiveServerToolCall()),
            _input("late"),
        ],
    )
    await asyncio.sleep(0.05)
    assert [
        (event.text, event.metadata["turn_id"])
        for event in events
        if event.role == TranscriptRole.USER and event.type == SessionEventType.TRANSCRIPT_FINAL
    ] == [("request", "voice:1"), ("late", "voice:2")]
    _assert_turns_settled(events)


@pytest.mark.parametrize("publish_client_events", [False, True])
async def test_cleanup_finalizes_pending_transcripts_once(publish_client_events: bool) -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    service = RecordingGeminiLiveService(
        session_id="voice",
        api_key="test-key",
        model="gemini-3.8-live",
        event_sink=collect,
        publish_client_events=publish_client_events,
    )
    await service._handle_msg_input_transcription(_input("first"))
    await service._handle_msg_output_transcription(_output("partial answer"))
    await service._handle_msg_input_transcription(_input("last words"))

    await service.cleanup()
    await service.cleanup()

    assert [
        (event.role, event.text, event.metadata)
        for event in events
        if event.type == SessionEventType.TRANSCRIPT_FINAL
    ] == [
        (TranscriptRole.USER, "first", {"turn_id": "voice:1"}),
        (TranscriptRole.USER, "last words", {"turn_id": "voice:2"}),
        (TranscriptRole.ASSISTANT, "partial answer", {}),
    ]
    _assert_turns_settled(events)
    assert service._user_settle_task is None


async def test_cleanup_during_input_does_not_start_timer(monkeypatch: pytest.MonkeyPatch) -> None:
    entered, release, cleanup_entered, cleanup_release = (asyncio.Event() for _ in range(4))
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)
        if event.type == SessionEventType.TRANSCRIPT_DELTA:
            entered.set()
            await release.wait()

    async def cleanup(_service: GeminiLiveLLMService) -> None:
        cleanup_entered.set()
        await cleanup_release.wait()

    monkeypatch.setattr(GeminiLiveLLMService, "cleanup", cleanup)
    service = RecordingGeminiLiveService(
        session_id="voice", api_key="test-key", model="gemini-3.8-live", event_sink=collect
    )
    service._user_settle_secs = 0.01
    incoming = asyncio.create_task(service._handle_msg_input_transcription(_input("private")))
    await entered.wait()
    closing = asyncio.create_task(service.cleanup())
    await cleanup_entered.wait()
    release.set()
    await incoming
    await service._handle_msg_input_transcription(_input("ignored"))
    cleanup_release.set()
    await closing
    await asyncio.sleep(0.05)
    assert service._user_settle_task is None
    assert [(event.type, event.text) for event in events] == [
        (SessionEventType.TRANSCRIPT_DELTA, "private"),
        (SessionEventType.TRANSCRIPT_FINAL, "private"),
    ]


async def test_real_reconnect_with_fake_live_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    service = RecordingGeminiLiveService(
        session_id="voice", api_key="test-key", model="gemini-3.8-live", event_sink=collect
    )

    class Connection:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return

        async def close(self) -> None:
            return

        async def receive(self) -> AsyncIterator[LiveServerMessage]:
            yield _input("new", finished=True)
            yield _output("new reply", complete=True)
            service._disconnecting = True
            raise RuntimeError("end of fake stream")

    def connect(**_kwargs: object) -> Connection:
        return Connection()

    def create_task(coroutine: Any, **_kwargs: Any) -> asyncio.Task[Any]:
        return asyncio.create_task(coroutine)

    monkeypatch.setattr(service, "create_task", create_task)
    service._client = SimpleNamespace(  # type: ignore[assignment]
        aio=SimpleNamespace(live=SimpleNamespace(connect=connect))
    )
    service._session = Connection()  # type: ignore[assignment]
    await service._handle_msg_input_transcription(_input("old"))
    await service._handle_msg_output_transcription(_output("old reply"))
    await service._handle_msg_input_transcription(_input("old tail"))
    await service._reconnect()
    task: asyncio.Task[Any] | None = service._connection_task  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
    assert task is not None
    await task
    assert [
        (event.role, event.text)
        for event in events
        if event.type == SessionEventType.TRANSCRIPT_FINAL
    ] == [
        (TranscriptRole.USER, "old"),
        (TranscriptRole.USER, "old tail"),
        (TranscriptRole.ASSISTANT, "old reply"),
        (TranscriptRole.USER, "new"),
        (TranscriptRole.ASSISTANT, "new reply"),
    ]
    _assert_turns_settled(events)


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
