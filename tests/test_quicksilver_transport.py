from __future__ import annotations

from collections.abc import Callable
from typing import Any, ClassVar

import pytest
from pytest import MonkeyPatch

from kassette.credentials import CodexCredentials
from kassette.providers.quicksilver.transport import (
    QuicksilverTransport,
    QuicksilverTransportError,
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
    def __init__(self) -> None:
        self.connectionState = "new"

    def addTrack(self, _track: object) -> None:
        return

    def getTransceivers(self) -> list[FakeTransceiver]:
        return [FakeTransceiver()]

    def createDataChannel(self, _name: str) -> FakeChannel:
        return FakeChannel()

    def on(self, _event: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def register(callback: Callable[..., Any]) -> Callable[..., Any]:
            return callback

        return register

    async def createOffer(self) -> Any:
        return type("Offer", (), {"sdp": "sdp-secret", "type": "offer"})()

    async def close(self) -> None:
        return


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
