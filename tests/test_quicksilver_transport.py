from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, ClassVar, cast

import pytest
from aiortc import MediaStreamTrack
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from pytest import MonkeyPatch

from kassette.credentials import CodexCredentials
from kassette.domain import AudioChunk
from kassette.providers.quicksilver.protocol import ProviderEvent
from kassette.providers.quicksilver.transport import (
    QuicksilverTransport,
    QuicksilverTransportError,
    _InputAudioTrack,  # pyright: ignore[reportPrivateUsage]
)


class FakeCredentials:
    async def load(self) -> CodexCredentials:
        return CodexCredentials(access_token="credential-secret", account_id="account-secret")


class FakeTransceiver:
    def setCodecPreferences(self, _codecs: object) -> None:
        return


class FakeChannel:
    def on(self, _event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(callback: Callable[..., Any]) -> Callable[..., Any]:
            return callback

        return register


class FakePeer:
    latest: FakePeer | None = None

    def __init__(self) -> None:
        type(self).latest = self
        self.connectionState = "new"
        self.handlers: dict[str, Callable[..., Any]] = {}

    def addTrack(self, _track: object) -> None:
        return

    def getTransceivers(self) -> list[FakeTransceiver]:
        return [FakeTransceiver()]

    def createDataChannel(self, _name: str) -> FakeChannel:
        return FakeChannel()

    def on(self, _event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(callback: Callable[..., Any]) -> Callable[..., Any]:
            self.handlers[_event] = callback
            return callback

        return register

    async def createOffer(self) -> Any:
        return type("Offer", (), {"sdp": "sdp-secret", "type": "offer"})()

    async def close(self) -> None:
        return

    def emit(self, event: str, *args: object) -> None:
        self.handlers[event](*args)


class EndedAudioTrack:
    kind = "audio"

    async def recv(self) -> None:
        raise MediaStreamError


class OneFrameAudioTrack:
    kind = "audio"

    def __init__(self) -> None:
        frame = AudioFrame(format="s16", layout="mono", samples=480)
        frame.planes[0].update(b"\x64\x00" * 480)
        frame.sample_rate = 24_000
        self.frame: AudioFrame | None = frame

    async def recv(self) -> AudioFrame:
        if self.frame is None:
            raise MediaStreamError
        frame = self.frame
        self.frame = None
        return frame


class HangingSideband:
    closed = False

    def __aiter__(self) -> HangingSideband:
        return self

    async def __anext__(self) -> Any:
        await asyncio.Future[None]()
        raise StopAsyncIteration

    async def send_json(self, _message: object) -> None:
        return

    async def close(self, **_kwargs: object) -> None:
        self.closed = True


class EndedSideband(HangingSideband):
    async def __anext__(self) -> Any:
        raise StopAsyncIteration


class CancelledSendSideband(HangingSideband):
    async def send_json(self, _message: object) -> None:
        raise asyncio.CancelledError


class BlockingCloseSideband(HangingSideband):
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.allow_close = asyncio.Event()

    async def close(self, **_kwargs: object) -> None:
        self.close_started.set()
        await self.allow_close.wait()
        self.closed = True


class FakeResponse:
    ok = False
    status = 403
    headers: ClassVar[dict[str, str]] = {}

    async def text(self) -> str:
        return "credential-secret account-secret sdp-secret provider-controlled" * 100

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return


class OversizedContent:
    async def read(self, _limit: int) -> bytes:
        return b"x" * 262_145


class OversizedResponse(FakeResponse):
    ok = True
    status = 200
    headers: ClassVar[dict[str, str]] = {"location": "/rtc_call"}
    content = OversizedContent()


class OversizedHTTP:
    def __init__(self, **_kwargs: object) -> None:
        return

    def post(self, _url: str, **_kwargs: object) -> OversizedResponse:
        return OversizedResponse()

    async def close(self) -> None:
        return


class FakeHTTP:
    def __init__(self, **_kwargs: object) -> None:
        self.request: dict[str, object] = {}

    def post(self, _url: str, **kwargs: object) -> FakeResponse:
        self.request = kwargs
        return FakeResponse()

    async def close(self) -> None:
        return


async def _discard(_value: object) -> None:
    return


def _capabilities(_kind: str) -> Any:
    return type("Capabilities", (), {"codecs": []})()


async def test_signaling_failure_does_not_echo_auth_sdp_or_provider_body(
    monkeypatch: MonkeyPatch,
) -> None:
    from kassette.providers.quicksilver import transport as module

    monkeypatch.setattr(module, "RTCPeerConnection", FakePeer)
    monkeypatch.setattr(module.aiohttp, "ClientSession", FakeHTTP)
    monkeypatch.setattr(
        module.RTCRtpSender,
        "getCapabilities",
        _capabilities,
    )
    transport = QuicksilverTransport(
        session_id="voice-1",
        credentials=FakeCredentials(),
        instructions="instructions-secret",
        voice="sol",
        event_sink=_discard,
        audio_sink=_discard,
    )

    with pytest.raises(QuicksilverTransportError) as raised:
        await transport.open()

    message = str(raised.value)
    assert message == "Quicksilver signaling failed with HTTP 403"
    assert "credential-secret" not in message
    assert "account-secret" not in message
    assert "sdp-secret" not in message
    assert "provider-controlled" not in message


async def test_signaling_answer_is_size_bounded(monkeypatch: MonkeyPatch) -> None:
    from kassette.providers.quicksilver import transport as module

    monkeypatch.setattr(module, "RTCPeerConnection", FakePeer)
    monkeypatch.setattr(module.aiohttp, "ClientSession", OversizedHTTP)
    monkeypatch.setattr(module.RTCRtpSender, "getCapabilities", _capabilities)
    transport = QuicksilverTransport(
        session_id="voice-1",
        credentials=FakeCredentials(),
        instructions="instructions-secret",
        voice="sol",
        event_sink=_discard,
        audio_sink=_discard,
    )

    with pytest.raises(QuicksilverTransportError, match="response is too large"):
        await transport.open()


async def test_ordered_provider_audio_is_dispatched_after_its_control_event() -> None:
    received: list[tuple[str, object]] = []

    async def collect_event(event: ProviderEvent) -> None:
        received.append(("event", event.type))

    async def collect_audio(chunk: AudioChunk) -> None:
        received.append(("audio", chunk.audio))

    transport = QuicksilverTransport(
        session_id="voice-1",
        credentials=FakeCredentials(),
        instructions="instructions",
        voice="sol",
        event_sink=collect_event,
        audio_sink=collect_audio,
    )
    await transport._dispatch_provider_event(  # pyright: ignore[reportPrivateUsage]
        ProviderEvent(
            type="output_audio.delta",
            audio=b"\x01\x02",
            sample_rate=24_000,
            num_channels=1,
        )
    )

    assert received == [
        ("event", "output_audio.delta"),
        ("audio", b"\x01\x02"),
    ]


async def test_input_audio_track_enforces_chunk_size_bound() -> None:
    track = _InputAudioTrack()
    accepted = AudioChunk(audio=b"\x00" * 65_536, sample_rate=16_000, num_channels=1)

    await track.write(accepted)
    with pytest.raises(QuicksilverTransportError, match="audio chunk is too large"):
        await track.write(AudioChunk(audio=b"\x00" * 65_538, sample_rate=16_000, num_channels=1))

    frame = await track.recv()
    assert frame.samples == 32_768
    await track.close()


async def test_input_audio_backpressures_during_delayed_rtp_consumption() -> None:
    track = _InputAudioTrack()
    chunk = AudioChunk(audio=b"\x00" * 640, sample_rate=16_000, num_channels=1)

    async def produce() -> None:
        for _ in range(129):
            await track.write(chunk)

    async def consume() -> None:
        await asyncio.sleep(0.01)
        for _ in range(129):
            await track.recv()

    await asyncio.wait_for(asyncio.gather(produce(), consume()), timeout=1)
    await track.close()


async def _open_connected_transport(
    monkeypatch: MonkeyPatch,
    events: list[ProviderEvent],
    sideband_type: type[HangingSideband] = HangingSideband,
) -> tuple[QuicksilverTransport, FakePeer]:
    from kassette.providers.quicksilver import transport as module

    async def collect(event: ProviderEvent) -> None:
        events.append(event)

    async def connect_sideband(
        _self: QuicksilverTransport,
        _call_id: str,
        _headers: dict[str, str],
    ) -> Any:
        return sideband_type()

    async def wait_for_peer(peer: FakePeer) -> None:
        peer.connectionState = "connected"

    monkeypatch.setattr(module, "RTCPeerConnection", FakePeer)
    monkeypatch.setattr(module.aiohttp, "ClientSession", OversizedHTTP)
    monkeypatch.setattr(module.RTCRtpSender, "getCapabilities", _capabilities)
    monkeypatch.setattr(QuicksilverTransport, "_connect_sideband", connect_sideband)
    monkeypatch.setattr(QuicksilverTransport, "_wait_for_peer", staticmethod(wait_for_peer))

    class AnswerContent:
        async def read(self, _limit: int) -> bytes:
            return b"answer-sdp"

    class AnswerResponse(OversizedResponse):
        content = AnswerContent()

    class AnswerHTTP(OversizedHTTP):
        def post(self, _url: str, **_kwargs: object) -> AnswerResponse:
            return AnswerResponse()

    async def set_description(_self: FakePeer, _description: object) -> None:
        return

    monkeypatch.setattr(module.aiohttp, "ClientSession", AnswerHTTP)
    monkeypatch.setattr(FakePeer, "setLocalDescription", set_description, raising=False)
    monkeypatch.setattr(FakePeer, "setRemoteDescription", set_description, raising=False)

    transport = QuicksilverTransport(
        session_id="voice-1",
        credentials=FakeCredentials(),
        instructions="instructions-secret",
        voice="sol",
        event_sink=collect,
        audio_sink=_discard,
    )
    await transport.open()
    assert FakePeer.latest is not None
    return transport, FakePeer.latest


async def test_rtp_audio_remains_active_when_sideband_carries_only_control() -> None:
    events: list[ProviderEvent] = []
    audio: list[AudioChunk] = []

    async def collect_event(event: ProviderEvent) -> None:
        events.append(event)

    async def collect_audio(chunk: AudioChunk) -> None:
        audio.append(chunk)

    transport = QuicksilverTransport(
        session_id="voice-1",
        credentials=FakeCredentials(),
        instructions="test",
        voice="sol",
        event_sink=collect_event,
        audio_sink=collect_audio,
    )
    transport._sideband = HangingSideband()  # pyright: ignore[reportPrivateUsage, reportAttributeAccessIssue]

    await transport._read_remote_audio(  # pyright: ignore[reportPrivateUsage]
        cast(MediaStreamTrack, OneFrameAudioTrack())
    )

    assert events[0].type == "output_audio.delta"
    assert len(audio) == 1
    assert audio[0].audio


async def test_sideband_audio_is_not_duplicated_when_rtp_is_active() -> None:
    events: list[ProviderEvent] = []
    audio: list[AudioChunk] = []

    async def collect_event(event: ProviderEvent) -> None:
        events.append(event)

    async def collect_audio(chunk: AudioChunk) -> None:
        audio.append(chunk)

    transport = QuicksilverTransport(
        session_id="voice-1",
        credentials=FakeCredentials(),
        instructions="test",
        voice="sol",
        event_sink=collect_event,
        audio_sink=collect_audio,
    )
    transport._rtp_audio_active = True  # pyright: ignore[reportPrivateUsage]
    event = ProviderEvent(
        type="output_audio.delta",
        audio=b"\x64\x00",
        sample_rate=24_000,
        num_channels=1,
    )

    await transport._dispatch_provider_event(event)  # pyright: ignore[reportPrivateUsage]
    await transport._dispatch_provider_event(  # pyright: ignore[reportPrivateUsage]
        event,
        audio_source="rtp",
    )

    assert [item.type for item in events] == ["output_audio.delta", "output_audio.delta"]
    assert len(audio) == 1


async def test_transport_cleanup_survives_independent_cancelled_error(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[ProviderEvent] = []
    transport, _peer = await _open_connected_transport(monkeypatch, events, CancelledSendSideband)
    sideband = transport._sideband  # pyright: ignore[reportPrivateUsage]
    assert isinstance(sideband, CancelledSendSideband)

    await transport.close()

    assert sideband.closed


async def test_transport_cleanup_continues_after_caller_cancellation(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[ProviderEvent] = []
    transport, _peer = await _open_connected_transport(monkeypatch, events, BlockingCloseSideband)
    sideband = transport._sideband  # pyright: ignore[reportPrivateUsage]
    assert isinstance(sideband, BlockingCloseSideband)

    cancelled_caller = asyncio.create_task(transport.close())
    await sideband.close_started.wait()
    cancelled_caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cancelled_caller

    replacement_cleanup = asyncio.create_task(transport.close())
    await asyncio.sleep(0)
    assert not replacement_cleanup.done()

    sideband.allow_close.set()
    await replacement_cleanup
    assert sideband.closed


async def test_established_peer_disconnect_emits_one_terminal_error(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[ProviderEvent] = []
    transport, peer = await _open_connected_transport(monkeypatch, events)

    peer.connectionState = "failed"
    peer.emit("connectionstatechange")
    peer.emit("connectionstatechange")
    await asyncio.sleep(0)

    assert len(events) == 1
    assert events[0].type == "error"
    await transport.close()


async def test_established_media_disconnect_emits_terminal_error(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[ProviderEvent] = []
    transport, peer = await _open_connected_transport(monkeypatch, events)

    peer.emit("track", EndedAudioTrack())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert len(events) == 1
    assert events[0].type == "error"
    await transport.close()


async def test_established_sideband_disconnect_emits_terminal_error(
    monkeypatch: MonkeyPatch,
) -> None:
    events: list[ProviderEvent] = []
    transport, _peer = await _open_connected_transport(monkeypatch, events, EndedSideband)
    await asyncio.sleep(0)

    assert len(events) == 1
    assert events[0].type == "error"
    await transport.close()
