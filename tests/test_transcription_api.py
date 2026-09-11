from __future__ import annotations

import asyncio
import base64
import json
import wave
from collections.abc import AsyncIterator
from io import BytesIO
from typing import cast

import httpx
import pytest
from aiohttp import web
from fastapi import FastAPI
from pydantic import SecretStr

from kassette import transcription_api as api
from kassette.settings import KassetteSettings

TOKEN = "local-test-token-not-a-secret-12345"
HEADERS = {"Authorization": "Bearer " + TOKEN}
PATH = "/v1/audio/transcriptions"


def settings() -> KassetteSettings:
    return KassetteSettings.model_construct(
        google_api_key=SecretStr("google-test-secret"),
        transcription_api_token=SecretStr(TOKEN),
        transcription_provider="openai",
    )


def wav_audio() -> bytes:
    buffer = BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16_000)
        wav.writeframes(b"\x00\x00" * 160)
    return buffer.getvalue()


async def success(
    audio: bytes, language: str, vocabulary: list[str], config: KassetteSettings
) -> str:
    assert audio == wav_audio()
    assert config.google_transcription_credential() == "google-test-secret"
    return "recognized speech"


def client_for(transcriber: api.Transcriber = success) -> httpx.AsyncClient:
    app = FastAPI()
    app.include_router(api.create_transcription_router(transcriber))
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost")


@pytest.fixture(autouse=True)
def configure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(api, "load_settings", settings)


async def send(client: httpx.AsyncClient) -> httpx.Response:
    return await client.post(
        PATH,
        headers=HEADERS,
        data={"model": api.MODEL, "response_format": "json"},
        files={"file": ("audio.wav", wav_audio(), "audio/wav")},
    )


async def test_screenpipe_contract_and_hint_mapping() -> None:
    async def transcribe(
        audio: bytes, language: str, vocabulary: list[str], config: KassetteSettings
    ) -> str:
        assert language == "en"
        assert vocabulary == ["kassette", "Gemini"]
        return await success(audio, language, vocabulary, config)

    async with client_for(transcribe) as client:
        response = await client.post(
            PATH,
            headers=HEADERS,
            data={
                "model": api.MODEL,
                "response_format": "json",
                "language": "en",
                "prompt": "kassette, Gemini",
                "context": "kassette, Gemini",
            },
            files={"file": ("audio.wav", wav_audio(), "audio/wav")},
        )
    assert response.status_code == 200
    assert response.json() == {"text": "recognized speech"}


@pytest.mark.parametrize(
    "headers,status",
    [
        ({}, 401),
        ({"Authorization": "Bearer wrong"}, 401),
        ({**HEADERS, "Origin": "http://localhost"}, 403),
    ],
)
async def test_access_control(headers: dict[str, str], status: int) -> None:
    async with client_for() as client:
        response = await client.post(PATH, headers=headers, content=b"not even multipart")
    assert response.status_code == status


@pytest.mark.parametrize("missing", ["google_api_key", "transcription_api_token"])
async def test_fail_closed_without_configuration(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    config = settings().model_copy(update={missing: None})
    monkeypatch.setattr(api, "load_settings", lambda: config)
    async with client_for() as client:
        response = await send(client)
    assert response.status_code == 503


async def test_streamed_upload_limit_without_content_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(api, "_MAX_BODY_BYTES", 10)

    async def chunks() -> AsyncIterator[bytes]:
        yield b"123456"
        yield b"123456"
        pytest.fail("body reader continued after limit")

    async with client_for() as client:
        response = await client.post(
            PATH,
            headers={**HEADERS, "Content-Type": "multipart/form-data; boundary=test"},
            content=chunks(),
        )
    assert response.status_code == 413


@pytest.mark.parametrize("audio", [b"", b"ID3not-a-wav", wav_audio()[:-2]])
async def test_reject_invalid_audio(audio: bytes) -> None:
    async with client_for() as client:
        response = await client.post(
            PATH,
            headers=HEADERS,
            data={"model": api.MODEL},
            files={"file": ("audio.wav", audio, "audio/wav")},
        )
    assert response.status_code == 422


@pytest.mark.parametrize(
    "data",
    [
        {"model": "other"},
        {"model": api.MODEL, "response_format": "text"},
        {"model": api.MODEL, "unexpected": "yes"},
    ],
)
async def test_reject_unsupported_fields(data: dict[str, str]) -> None:
    async with client_for() as client:
        response = await client.post(
            PATH,
            headers=HEADERS,
            data=data,
            files={"file": ("audio.wav", wav_audio(), "audio/wav")},
        )
    assert response.status_code == 422


async def test_capacity_and_cancel_release() -> None:
    entered = asyncio.Event()
    calls = 0

    async def blocked(
        audio: bytes, language: str, words: list[str], config: KassetteSettings
    ) -> str:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await asyncio.Event().wait()
        return "unreachable"

    async with client_for(blocked) as client:
        tasks = [asyncio.create_task(send(client)) for _ in range(2)]
        await asyncio.wait_for(entered.wait(), 1)
        assert (await send(client)).status_code == 429
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # A third upstream call demonstrates that cancellation released capacity.
        third = asyncio.create_task(send(client))
        await asyncio.sleep(0.02)
        assert calls == 3
        third.cancel()
        await asyncio.gather(third, return_exceptions=True)


async def test_timeout_and_error_sanitization(monkeypatch: pytest.MonkeyPatch) -> None:
    async def broken(
        audio: bytes, language: str, words: list[str], config: KassetteSettings
    ) -> str:
        raise RuntimeError("sensitive transcript and google-test-secret")

    async with client_for(broken) as client:
        response = await send(client)
        assert response.status_code == 502
        assert "sensitive" not in response.text and "secret" not in response.text

    monkeypatch.setattr(api, "_TIMEOUT_SECONDS", 0.01)

    async def slow(audio: bytes, language: str, words: list[str], config: KassetteSettings) -> str:
        await asyncio.sleep(1)
        return "late"

    async with client_for(slow) as client:
        assert (await send(client)).status_code == 504
        assert (await send(client)).status_code == 504


@pytest.mark.parametrize("payload", [{"status": "completed"}, {"status": "completed", "steps": []}])
def test_completed_silence_returns_empty_transcript(payload: dict[str, object]) -> None:
    assert api._transcript(payload) == ""  # pyright: ignore[reportPrivateUsage]


@pytest.mark.parametrize(
    "payload",
    [{}, {"status": "in_progress", "steps": []}, {"status": "completed", "steps": "invalid"}],
)
def test_incomplete_provider_output_is_not_silent_success(payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        api._transcript(payload)  # pyright: ignore[reportPrivateUsage]


async def test_real_http_adapter_payload_response_and_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_status = 200
    provider_body = json.dumps(
        {
            "status": "completed",
            "steps": [{"type": "model_output", "content": [{"type": "text", "text": "hello"}]}],
        }
    )

    async def handle(request: web.Request) -> web.Response:
        assert request.headers["x-goog-api-key"] == "google-test-secret"
        body = cast(dict[str, object], await request.json())
        assert body == {
            "model": api.MODEL,
            "store": False,
            "input": [
                {
                    "type": "audio",
                    "mime_type": "audio/wav",
                    "data": base64.b64encode(wav_audio()).decode(),
                }
            ],
            "generation_config": {
                "transcription_config": {
                    "mode": {"type": "verbatim"},
                    "language_codes": ["en"],
                    "custom_vocabulary": ["kassette"],
                }
            },
        }
        return web.Response(text=provider_body, status=provider_status)

    app = web.Application()
    app.router.add_post("/interactions", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = cast(tuple[str, int], runner.addresses[0])[1]
    monkeypatch.setattr(api, "_INTERACTIONS_URL", f"http://127.0.0.1:{port}/interactions")
    try:
        assert await api.transcribe_gemini(wav_audio(), "en", ["kassette"], settings()) == "hello"
        provider_status = 403
        with pytest.raises(RuntimeError, match="provider request failed"):
            await api.transcribe_gemini(wav_audio(), "en", ["kassette"], settings())
        provider_status = 200
        monkeypatch.setattr(api, "_MAX_RESPONSE_BYTES", 2)
        with pytest.raises(ValueError, match="too large"):
            await api.transcribe_gemini(wav_audio(), "en", ["kassette"], settings())
    finally:
        await runner.cleanup()


def test_install_route_idempotent() -> None:
    app = FastAPI()
    api.install_transcription_route(app)
    route_count = len(app.routes)
    api.install_transcription_route(app)
    assert len(app.routes) == route_count
    assert PATH in app.openapi()["paths"]
