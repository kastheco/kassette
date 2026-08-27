import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from pipecat.frames.frames import Frame, InputAudioRawFrame, OutputAudioRawFrame
from pipecat.processors.frame_processor import FrameDirection

from kassette.credentials import CodexCredentials
from kassette.domain import AudioChunk, SessionEvent, SessionEventType, SessionState
from kassette.providers.quicksilver.protocol import ProviderEvent
from kassette.providers.quicksilver.service import GPTLiveService
from kassette.sessions import SessionRegistry


class FakeCredentials:
    async def load(self) -> CodexCredentials:
        return CodexCredentials(access_token="unused", account_id="unused")


class FakeTransport:
    def __init__(
        self,
        *,
        event_sink: Callable[[ProviderEvent], Awaitable[None]],
        audio_sink: Callable[[AudioChunk], Awaitable[None]],
        **_kwargs: Any,
    ) -> None:
        self.event_sink = event_sink
        self.audio_sink = audio_sink
        self.session_id = _kwargs["session_id"]
        self.voice = _kwargs["voice"]
        self.sent_audio: list[AudioChunk] = []
        self.sent_messages: list[dict[str, Any]] = []
        self.interrupted = False
        self.closed = False
        self.open_error: Exception | None = None
        self.interrupt_error: Exception | None = None

    async def open(self) -> None:
        if self.open_error is not None:
            raise self.open_error
        await self.event_sink(ProviderEvent(type="session.started", session_id="provider-1"))

    async def send_audio(self, chunk: AudioChunk) -> None:
        self.sent_audio.append(chunk)

    async def send(self, message: dict[str, Any]) -> None:
        self.sent_messages.append(message)

    async def interrupt(self) -> None:
        if self.interrupt_error is not None:
            raise self.interrupt_error
        self.interrupted = True

    async def close(self) -> None:
        self.closed = True


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


async def test_fake_native_provider_completes_session_lifecycle() -> None:
    registry = SessionRegistry()
    await registry.create("voice-1")
    events: list[SessionEvent] = []
    transports: list[FakeTransport] = []

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    service = GPTLiveService(
        session_id="voice-1",
        registry=registry,
        credentials=FakeCredentials(),
        event_sink=collect,
        transport_factory=create_transport,
    )

    await service._start_session()  # pyright: ignore[reportPrivateUsage]
    assert (await registry.get("voice-1")).state is SessionState.LISTENING
    assert await registry.audio_owner() == "voice-1"

    await service._interrupt()  # pyright: ignore[reportPrivateUsage]
    assert transports[0].interrupted
    assert (await registry.get("voice-1")).state is SessionState.LISTENING

    await service._close()  # pyright: ignore[reportPrivateUsage]
    assert transports[0].closed
    assert (await registry.get("voice-1")).state is SessionState.CLOSED
    assert await registry.audio_owner() is None
    assert SessionEventType.INTERRUPTED in {event.type for event in events}


async def test_service_cleanup_continues_after_caller_cancellation() -> None:
    registry = SessionRegistry()
    await registry.create("voice-1")
    close_started = asyncio.Event()
    allow_close = asyncio.Event()
    transports: list[FakeTransport] = []

    class BlockingCloseTransport(FakeTransport):
        async def close(self) -> None:
            close_started.set()
            await allow_close.wait()
            self.closed = True

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = BlockingCloseTransport(**kwargs)
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        registry=registry,
        credentials=FakeCredentials(),
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]

    cancelled_caller = asyncio.create_task(
        service._close()  # pyright: ignore[reportPrivateUsage]
    )
    await close_started.wait()
    cancelled_caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled_caller

    replacement_cleanup = asyncio.create_task(
        service._close()  # pyright: ignore[reportPrivateUsage]
    )
    await asyncio.sleep(0)
    assert not replacement_cleanup.done()

    allow_close.set()
    await replacement_cleanup
    assert transports[0].closed
    assert (await registry.get("voice-1")).state is SessionState.CLOSED
    assert await registry.audio_owner() is None


async def test_pipecat_and_quicksilver_exchange_audio_in_process() -> None:
    registry = SessionRegistry()
    await registry.create("voice-1")
    events: list[SessionEvent] = []
    transports: list[FakeTransport] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    service = RecordingGPTLiveService(
        session_id="voice-1",
        registry=registry,
        credentials=FakeCredentials(),
        event_sink=collect,
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]

    client_audio = InputAudioRawFrame(audio=b"\x01\x00\x02\x00", sample_rate=16_000, num_channels=1)
    await service.process_frame(client_audio, FrameDirection.DOWNSTREAM)
    assert transports[0].sent_audio == [
        AudioChunk(audio=client_audio.audio, sample_rate=16_000, num_channels=1)
    ]

    await service.process_frame(client_audio, FrameDirection.DOWNSTREAM)
    assert transports[0].sent_audio == [
        AudioChunk(audio=client_audio.audio, sample_rate=16_000, num_channels=1),
        AudioChunk(audio=client_audio.audio, sample_rate=16_000, num_channels=1),
    ]
    assert [event.type for event in events].count(SessionEventType.INPUT_AUDIO_STARTED) == 1

    provider_audio = AudioChunk(audio=b"\x03\x00\x04\x00", sample_rate=24_000, num_channels=1)
    await transports[0].audio_sink(provider_audio)
    output = service.pushed_frames[-1]
    assert isinstance(output[0], OutputAudioRawFrame)
    assert output[0].audio == provider_audio.audio
    assert output[0].sample_rate == 24_000
    assert output[0].num_channels == 1
    assert output[1] is FrameDirection.DOWNSTREAM


async def test_provider_events_are_normalized_without_leaking_provider_behavior() -> None:
    registry = SessionRegistry()
    await registry.create("voice-1")
    events: list[SessionEvent] = []
    transports: list[FakeTransport] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        registry=registry,
        credentials=FakeCredentials(),
        event_sink=collect,
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]
    transport = transports[0]

    snapshot = await registry.get("voice-1")
    assert snapshot.provider_session_id == "provider-1"
    assert transport.session_id == "voice-1"
    assert transport.voice == "sol"

    await transport.event_sink(
        ProviderEvent(type="input_transcript.added", role="user", text="hello")
    )
    await transport.audio_sink(AudioChunk(audio=b"\x01\x00", sample_rate=24_000, num_channels=1))
    await transport.event_sink(ProviderEvent(type="turn.done", role="assistant", text="hi"))
    await transport.event_sink(
        ProviderEvent(type="delegation.created", delegation_id="delegation-1")
    )

    before_unknown = await registry.get("voice-1")
    message_count = len(transport.sent_messages)
    await transport.event_sink(ProviderEvent(type="unknown", wire_type="provider.future"))
    assert await registry.get("voice-1") == before_unknown
    assert len(transport.sent_messages) == message_count

    assert transport.sent_messages == [
        {
            "type": "delegation.context.append",
            "delegation_item_id": "delegation-1",
            "content": [
                {
                    "type": "input_text",
                    "text": "Delegation is unavailable in this voice client. Answer directly.",
                }
            ],
        }
    ]
    assert [event.type for event in events] == [
        SessionEventType.SESSION_STATE_CHANGED,
        SessionEventType.SESSION_STATE_CHANGED,
        SessionEventType.TRANSCRIPT_DELTA,
        SessionEventType.SESSION_STATE_CHANGED,
        SessionEventType.SPEECH_STARTED,
        SessionEventType.SESSION_STATE_CHANGED,
        SessionEventType.SPEECH_STOPPED,
        SessionEventType.TRANSCRIPT_FINAL,
        SessionEventType.DELEGATION_UNAVAILABLE,
        SessionEventType.PROVIDER_UNKNOWN,
    ]
    assert events[2].text == "hello"
    assert events[7].text == "hi"
    assert events[-1].provider_type == "provider.future"


async def test_provider_open_failure_emits_normalized_failure_event() -> None:
    registry = SessionRegistry()
    await registry.create("voice-1")
    events: list[SessionEvent] = []
    transports: list[FakeTransport] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transport.open_error = RuntimeError("provider unavailable")
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        registry=registry,
        credentials=FakeCredentials(),
        event_sink=collect,
        transport_factory=create_transport,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await service._start_session()  # pyright: ignore[reportPrivateUsage]

    snapshot = await registry.get("voice-1")
    assert snapshot.state is SessionState.FAILED
    assert snapshot.error_code == "provider_connection_failed"
    assert await registry.audio_owner() is None
    assert events[-2:] == [
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.SESSION_STATE_CHANGED,
            state=SessionState.FAILED,
        ),
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.ERROR,
            state=SessionState.FAILED,
            error_code="provider_connection_failed",
            metadata={"message": "provider connection failed"},
        ),
    ]


@pytest.mark.parametrize(
    "provider_message",
    ["provider unavailable", None],
)
async def test_provider_error_emits_normalized_failure_event(
    provider_message: str | None,
) -> None:
    registry = SessionRegistry()
    await registry.create("voice-1")
    events: list[SessionEvent] = []
    transports: list[FakeTransport] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        registry=registry,
        credentials=FakeCredentials(),
        event_sink=collect,
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]
    events.clear()

    await transports[0].event_sink(ProviderEvent(type="error", message=provider_message))

    snapshot = await registry.get("voice-1")
    assert snapshot.state is SessionState.FAILED
    assert snapshot.error_code == "provider_error"
    assert await registry.audio_owner() is None
    assert events == [
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.SESSION_STATE_CHANGED,
            state=SessionState.FAILED,
        ),
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.ERROR,
            state=SessionState.FAILED,
            error_code="provider_error",
            metadata={"message": "provider error"},
        ),
    ]
    assert transports[0].closed


async def test_provider_error_emits_event_when_failed_transition_is_rejected() -> None:
    registry = SessionRegistry()
    await registry.create("voice-1")
    events: list[SessionEvent] = []
    transports: list[FakeTransport] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        registry=registry,
        credentials=FakeCredentials(),
        event_sink=collect,
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]
    await registry.transition("voice-1", SessionState.CLOSING)
    await registry.transition("voice-1", SessionState.CLOSED)
    event_count = len(events)

    await transports[0].event_sink(ProviderEvent(type="error", message="provider failed"))

    assert events[event_count:] == [
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.ERROR,
            state=SessionState.FAILED,
            error_code="provider_error",
            metadata={"message": "provider error"},
        )
    ]


async def test_repeated_provider_errors_emit_one_failed_state_change() -> None:
    registry = SessionRegistry()
    await registry.create("voice-1")
    events: list[SessionEvent] = []
    transports: list[FakeTransport] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        registry=registry,
        credentials=FakeCredentials(),
        event_sink=collect,
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]
    events.clear()

    await transports[0].event_sink(ProviderEvent(type="error", message="boom one"))
    await transports[0].event_sink(ProviderEvent(type="error", message="boom two"))

    assert events == [
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.SESSION_STATE_CHANGED,
            state=SessionState.FAILED,
        ),
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.ERROR,
            state=SessionState.FAILED,
            error_code="provider_error",
            metadata={"message": "provider error"},
        ),
    ]


async def test_terminal_cleanup_is_idempotent() -> None:
    registry = SessionRegistry()
    snapshot = await registry.create("voice-1")
    transports: list[FakeTransport] = []

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        generation=snapshot.generation,
        registry=registry,
        credentials=FakeCredentials(),
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]

    await service._close()  # pyright: ignore[reportPrivateUsage]
    await service._close()  # pyright: ignore[reportPrivateUsage]

    assert transports[0].closed
    assert (await registry.get("voice-1")).state is SessionState.CLOSED
    assert await registry.audio_owner() is None


async def test_terminal_cleanup_survives_lifecycle_sink_failure() -> None:
    registry = SessionRegistry()
    snapshot = await registry.create("voice-1")
    transports: list[FakeTransport] = []

    async def fail_closing(event: SessionEvent) -> None:
        if event.state is SessionState.CLOSING:
            raise RuntimeError("lifecycle sink failed")

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        generation=snapshot.generation,
        registry=registry,
        credentials=FakeCredentials(),
        event_sink=fail_closing,
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(RuntimeError, match="lifecycle sink failed"):
        await service._close()  # pyright: ignore[reportPrivateUsage]

    assert transports[0].closed
    assert (await registry.get("voice-1")).state is SessionState.CLOSED
    assert await registry.audio_owner() is None


async def test_failed_interruption_returns_session_to_listening() -> None:
    registry = SessionRegistry()
    snapshot = await registry.create("voice-1")
    transports: list[FakeTransport] = []

    def create_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        transport.interrupt_error = RuntimeError("interrupt failed")
        transports.append(transport)
        return transport

    service = GPTLiveService(
        session_id="voice-1",
        generation=snapshot.generation,
        registry=registry,
        credentials=FakeCredentials(),
        transport_factory=create_transport,
    )
    await service._start_session()  # pyright: ignore[reportPrivateUsage]

    with pytest.raises(RuntimeError, match="interrupt failed"):
        await service._interrupt()  # pyright: ignore[reportPrivateUsage]

    assert (await registry.get("voice-1")).state is SessionState.LISTENING


async def test_stale_provider_callbacks_cannot_mutate_recreated_session() -> None:
    registry = SessionRegistry()
    first = await registry.create("voice")
    old_transports: list[FakeTransport] = []

    def create_old_transport(**kwargs: Any) -> FakeTransport:
        transport = FakeTransport(**kwargs)
        old_transports.append(transport)
        return transport

    old_service = RecordingGPTLiveService(
        session_id="voice",
        generation=first.generation,
        registry=registry,
        credentials=FakeCredentials(),
        transport_factory=create_old_transport,
    )
    await old_service._start_session()  # pyright: ignore[reportPrivateUsage]
    await registry.transition("voice", SessionState.FAILED)
    await registry.reap("voice", expected_generation=first.generation)
    second = await registry.create("voice")
    await registry.acquire_audio("voice", expected_generation=second.generation)

    before = await registry.get("voice")
    await old_transports[0].event_sink(
        ProviderEvent(type="session.started", session_id="stale-provider")
    )
    await old_transports[0].audio_sink(
        AudioChunk(audio=b"\x01\x00", sample_rate=24_000, num_channels=1)
    )
    await old_service.process_frame(
        InputAudioRawFrame(audio=b"\x01\x00", sample_rate=16_000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )
    await registry.release_audio("voice", expected_generation=first.generation)

    assert await registry.get("voice") == before
    assert await registry.audio_owner() == "voice"
    assert old_transports[0].sent_audio == []
    assert old_service.pushed_frames == []
