"""Local on-demand text-to-speech endpoint for message playback."""

from __future__ import annotations

import asyncio
import wave
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from hashlib import sha256
from io import BytesIO
from typing import cast

import ormsgpack
from fastapi import APIRouter, FastAPI, HTTPException, Response
from loguru import logger
from pydantic import BaseModel, Field
from websockets.asyncio.client import connect

from kassette.settings import KassetteSettings, load_settings

_MAX_AUDIO_BYTES = 12 * 1024 * 1024
_SYNTHESIS_TIMEOUT_SECONDS = 45
_CACHE_ENTRIES = 32


class TTSRequest(BaseModel):
    """Validated message speech request."""

    text: str = Field(min_length=1, max_length=12_000)


Synthesizer = Callable[[str, KassetteSettings], Awaitable[bytes]]


async def synthesize_fish_wav(text: str, settings: KassetteSettings) -> bytes:
    """Generate one complete browser-playable WAV using Fish Audio's streaming protocol."""
    api_key = settings.fish_credential()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "model": settings.fish_model,
    }
    start_message = {
        "event": "start",
        "request": {
            "text": "",
            "sample_rate": 24_000,
            "latency": "balanced",
            "format": "pcm",
            "normalize": True,
            "prosody": {"speed": 1.0, "volume": 0},
            "reference_id": settings.fish_voice_id,
        },
    }
    chunks: list[bytes] = []
    total_bytes = 0

    async with asyncio.timeout(_SYNTHESIS_TIMEOUT_SECONDS):
        async with connect(
            "wss://api.fish.audio/v1/tts/live",
            additional_headers=headers,
            max_size=_MAX_AUDIO_BYTES,
        ) as websocket:
            await websocket.send(ormsgpack.packb(start_message))
            await websocket.send(ormsgpack.packb({"event": "text", "text": text}))
            await websocket.send(ormsgpack.packb({"event": "flush"}))
            await websocket.send(ormsgpack.packb({"event": "stop"}))

            while True:
                try:
                    message = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=1.5 if chunks else 15.0,
                    )
                except TimeoutError as error:
                    if chunks:
                        break
                    raise RuntimeError("Fish Audio returned no speech audio") from error
                if not isinstance(message, bytes):
                    continue
                decoded = cast(object, ormsgpack.unpackb(message))
                if not isinstance(decoded, dict):
                    continue
                record = cast(dict[str, object], decoded)
                event = record.get("event")
                if event == "audio":
                    audio = record.get("audio")
                    if not isinstance(audio, bytes) or not audio:
                        continue
                    total_bytes += len(audio)
                    if total_bytes > _MAX_AUDIO_BYTES:
                        raise RuntimeError("generated speech exceeded the audio size limit")
                    chunks.append(audio)
                    continue
                if event == "finish":
                    if record.get("reason") == "error":
                        raise RuntimeError("Fish Audio failed to synthesize speech")
                    break

    if not chunks:
        raise RuntimeError("Fish Audio returned no speech audio")
    output = BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24_000)
        wav.writeframes(b"".join(chunks))
    return output.getvalue()


class OnDemandTTSService:
    """Deduplicate concurrent requests and retain a small process-local audio cache."""

    def __init__(
        self,
        *,
        synthesizer: Synthesizer = synthesize_fish_wav,
        max_entries: int = _CACHE_ENTRIES,
    ) -> None:
        self._synthesizer = synthesizer
        self._max_entries = max_entries
        self._cache: OrderedDict[str, bytes] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    async def generate(self, text: str, settings: KassetteSettings) -> tuple[str, bytes]:
        """Return a stable content key and MP3 bytes for normalized message text."""
        normalized = " ".join(text.split())
        if not normalized:
            raise ValueError("speech text must not be blank")
        cache_key = sha256(
            "\0".join(
                (
                    settings.fish_model,
                    settings.fish_voice_id or "",
                    normalized,
                )
            ).encode()
        ).hexdigest()
        cached = self._cache.get(cache_key)
        if cached is not None:
            self._cache.move_to_end(cache_key)
            return cache_key, cached

        lock = self._locks.setdefault(cache_key, asyncio.Lock())
        try:
            async with lock:
                cached = self._cache.get(cache_key)
                if cached is not None:
                    self._cache.move_to_end(cache_key)
                    return cache_key, cached
                audio = await self._synthesizer(normalized, settings)
                self._cache[cache_key] = audio
                self._cache.move_to_end(cache_key)
                while len(self._cache) > self._max_entries:
                    self._cache.popitem(last=False)
                return cache_key, audio
        finally:
            self._locks.pop(cache_key, None)


def create_tts_router(service: OnDemandTTSService | None = None) -> APIRouter:
    """Create the local API router with an injectable synthesis service."""
    router = APIRouter()
    tts = service or OnDemandTTSService()

    async def _generate_speech(request: TTSRequest) -> Response:
        try:
            cache_key, audio = await tts.generate(request.text, load_settings())
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except RuntimeError as error:
            logger.warning("on-demand TTS unavailable: {}", error)
            raise HTTPException(status_code=503, detail="Text-to-speech is unavailable") from error
        except TimeoutError as error:
            logger.warning("on-demand TTS timed out")
            raise HTTPException(status_code=504, detail="Text-to-speech timed out") from error
        except Exception as error:
            logger.exception("on-demand TTS failed")
            raise HTTPException(status_code=502, detail="Text-to-speech failed") from error
        return Response(
            content=audio,
            media_type="audio/wav",
            headers={
                "Cache-Control": "no-store",
                "X-Kassette-TTS-Key": cache_key,
            },
        )

    router.add_api_route("/api/tts", _generate_speech, methods=["POST"])
    return router


def install_tts_route(app: FastAPI) -> None:
    """Mount the message TTS endpoint on Pipecat's local runner app once."""
    if any(getattr(route, "path", None) == "/api/tts" for route in app.routes):
        return
    app.include_router(create_tts_router())
