from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from kassette.settings import KassetteSettings
from kassette.tts_api import OnDemandTTSService, create_tts_router


def tts_settings() -> KassetteSettings:
    return KassetteSettings.model_construct(
        fish_api_key=SecretStr("fish-secret"),
        fish_model="s2.1-pro",
        fish_voice_id="voice-1",
    )


@pytest.mark.asyncio
async def test_on_demand_tts_deduplicates_and_caches_matching_requests() -> None:
    calls: list[str] = []

    async def synthesize(text: str, _settings: KassetteSettings) -> bytes:
        calls.append(text)
        await asyncio.sleep(0)
        return b"ID3speech"

    service = OnDemandTTSService(synthesizer=synthesize)
    first, second = await asyncio.gather(
        service.generate(" hello   world ", tts_settings()),
        service.generate("hello world", tts_settings()),
    )
    third = await service.generate("hello world", tts_settings())

    assert first == second == third
    assert calls == ["hello world"]


@pytest.mark.asyncio
async def test_tts_route_returns_browser_playable_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    async def synthesize(text: str, _settings: KassetteSettings) -> bytes:
        assert text == "read this aloud"
        return b"ID3speech"

    monkeypatch.setattr("kassette.tts_api.load_settings", tts_settings)
    app = FastAPI()
    app.include_router(create_tts_router(OnDemandTTSService(synthesizer=synthesize)))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/tts", json={"text": "read this aloud"})

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "no-store"
    assert len(response.headers["x-kassette-tts-key"]) == 64
    assert response.content == b"ID3speech"


@pytest.mark.asyncio
async def test_tts_route_rejects_blank_text(monkeypatch: pytest.MonkeyPatch) -> None:
    async def synthesize(_text: str, _settings: KassetteSettings) -> bytes:
        raise AssertionError("blank speech must not reach the provider")

    monkeypatch.setattr("kassette.tts_api.load_settings", tts_settings)
    app = FastAPI()
    app.include_router(create_tts_router(OnDemandTTSService(synthesizer=synthesize)))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/tts", json={"text": "   "})

    assert response.status_code == 422
