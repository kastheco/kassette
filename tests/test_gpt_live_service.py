from collections.abc import Awaitable, Callable
from typing import Any

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
        self.interrupted = False
        self.closed = False

    async def open(self) -> None:
        await self.event_sink(ProviderEvent(type="session.started", session_id="provider-1"))

    async def send_audio(self, chunk: AudioChunk) -> None:
        del chunk

    async def send(self, message: dict[str, Any]) -> None:
        del message

    async def interrupt(self) -> None:
        self.interrupted = True

    async def close(self) -> None:
        self.closed = True


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
