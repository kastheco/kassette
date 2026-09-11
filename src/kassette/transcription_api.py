"""Bounded, local OpenAI-compatible batch transcription through Gemini."""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import wave
from collections.abc import Awaitable, Callable
from io import BytesIO
from typing import cast

import aiohttp
from fastapi import APIRouter, FastAPI, Request, Response
from loguru import logger
from starlette.datastructures import UploadFile
from starlette.exceptions import HTTPException
from starlette.types import Message

from kassette.settings import KassetteSettings, load_settings

MODEL = "gemini-3.5-transcribe"
_MAX_BODY_BYTES = 8 * 1024 * 1024
_MAX_RESPONSE_BYTES = 256 * 1024
_TIMEOUT_SECONDS = 25
_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

Transcriber = Callable[[bytes, str, list[str], KassetteSettings], Awaitable[str]]


def _transcript(payload: object) -> str:
    if not isinstance(payload, dict):
        raise ValueError("invalid provider response")
    record = cast(dict[str, object], payload)
    if record.get("status") != "completed":
        raise ValueError("incomplete provider response")
    # Gemini omits steps for completed audio with no recognized speech.
    steps = record.get("steps", [])
    if not isinstance(steps, list):
        raise ValueError("invalid provider steps")
    if not steps:
        return ""
    texts: list[str] = []
    for step in cast(list[object], steps):
        if not isinstance(step, dict):
            continue
        item = cast(dict[str, object], step)
        if item.get("type") != "model_output" or not isinstance(item.get("content"), list):
            continue
        for content in cast(list[object], item["content"]):
            if isinstance(content, dict):
                block = cast(dict[str, object], content)
                text = block.get("text")
                if block.get("type") == "text" and isinstance(text, str):
                    texts.append(text)
    if not texts:
        raise ValueError("missing provider transcript")
    return "\n".join(texts)


async def transcribe_gemini(
    audio: bytes, language: str, vocabulary: list[str], settings: KassetteSettings
) -> str:
    """Send inline WAV; never create a remote file or stored interaction."""
    key = settings.google_transcription_credential()
    config: dict[str, object] = {"mode": {"type": "verbatim"}}
    if language:
        config["language_codes"] = [language]
    if vocabulary:
        config["custom_vocabulary"] = vocabulary
    payload = {
        "model": MODEL,
        "store": False,
        "input": [
            {
                "type": "audio",
                "mime_type": "audio/wav",
                "data": base64.b64encode(audio).decode("ascii"),
            }
        ],
        "generation_config": {"transcription_config": config},
    }
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
    ) as client:
        async with client.post(
            _INTERACTIONS_URL,
            headers={"x-goog-api-key": key},
            json=payload,
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                logger.warning("batch transcription provider HTTP {}", response.status)
                raise RuntimeError("provider request failed")
            body = bytearray()
            async for chunk in response.content.iter_chunked(16_384):
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    raise ValueError("provider response too large")
            return _transcript(json.loads(body))


async def _bounded_form_request(request: Request) -> Request:
    """Bound the complete multipart body before Starlette parses any parts."""
    if not request.headers.get("content-type", "").lower().startswith("multipart/form-data;"):
        raise HTTPException(415, "Expected multipart audio upload")
    length = request.headers.get("content-length")
    if length is not None:
        try:
            size = int(length)
        except ValueError as error:
            raise HTTPException(400, "Invalid content length") from error
        if size < 0:
            raise HTTPException(400, "Invalid content length")
        if size > _MAX_BODY_BYTES:
            raise HTTPException(413, "Audio upload is too large")
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > _MAX_BODY_BYTES:
            raise HTTPException(413, "Audio upload is too large")
        body.extend(chunk)

    async def receive() -> Message:
        return {"type": "http.request", "body": bytes(body), "more_body": False}

    return Request(request.scope, receive=receive)


def _validate_wav(audio: bytes) -> None:
    try:
        with wave.open(BytesIO(audio)) as wav:
            frames = wav.getnframes()
            rate = wav.getframerate()
            channels = wav.getnchannels()
            if (
                wav.getsampwidth() != 2
                or channels not in (1, 2)
                or not 8_000 <= rate <= 48_000
                or not 0 < frames <= rate * 60
            ):
                raise ValueError("unsupported WAV")
            if len(wav.readframes(frames)) != frames * channels * 2:
                raise ValueError("truncated WAV")
    except (wave.Error, EOFError, ValueError) as error:
        raise HTTPException(422, "Expected complete PCM16 WAV audio, at most 60 seconds") from error


def create_transcription_router(transcriber: Transcriber = transcribe_gemini) -> APIRouter:
    """Create a token-protected route with two active requests and no waiting queue."""
    router = APIRouter()
    active = 0

    async def transcribe(request: Request, response: Response) -> dict[str, str]:
        nonlocal active
        settings = load_settings()
        token = settings.transcription_api_token
        if token is None:
            raise HTTPException(503, "Batch transcription is not configured")
        expected = "Bearer " + token.get_secret_value()
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), expected.encode()):
            raise HTTPException(401, "Invalid transcription token")
        if "origin" in request.headers:
            raise HTTPException(403, "Browser transcription requests are not supported")
        if active >= 2:
            raise HTTPException(429, "Transcription capacity is full")
        active += 1
        try:
            async with asyncio.timeout(_TIMEOUT_SECONDS):
                bounded = await _bounded_form_request(request)
                async with bounded.form(max_files=1, max_fields=5, max_part_size=4096) as form:
                    if len(form.multi_items()) != len(form):
                        raise HTTPException(422, "Duplicate transcription fields")
                    if set(form) - {
                        "file",
                        "model",
                        "response_format",
                        "language",
                        "prompt",
                        "context",
                    }:
                        raise HTTPException(422, "Unsupported transcription fields")
                    if form.get("model") != MODEL or form.get("response_format", "json") != "json":
                        raise HTTPException(422, "Expected gemini-3.5-transcribe and JSON output")
                    upload = form.get("file")
                    if not isinstance(upload, UploadFile):
                        raise HTTPException(422, "A WAV file is required")
                    audio = await upload.read()
                    _validate_wav(audio)
                    fields: dict[str, str] = {}
                    for name in ("language", "prompt", "context"):
                        value = form.get(name, "")
                        if not isinstance(value, str) or len(value) > 4096:
                            raise HTTPException(422, "Invalid transcription hint")
                        fields[name] = value
                    language = fields["language"].strip()
                    if len(language) > 35:
                        raise HTTPException(422, "Invalid language hint")
                    vocabulary = list(
                        dict.fromkeys(
                            word.strip()
                            for word in (fields["prompt"] + "," + fields["context"]).split(",")
                            if word.strip()
                        )
                    )
                    if len(vocabulary) > 1000:
                        raise HTTPException(422, "Too many vocabulary terms")
                    try:
                        settings.google_transcription_credential()
                    except RuntimeError as error:
                        raise HTTPException(
                            503, "Gemini transcription is not configured"
                        ) from error
                    text = await transcriber(audio, language, vocabulary, settings)
                    response.headers["Cache-Control"] = "no-store"
                    return {"text": text}
        except HTTPException:
            raise
        except TimeoutError as error:
            raise HTTPException(504, "Transcription timed out") from error
        except Exception as error:
            logger.warning("batch transcription failed: {}", type(error).__name__)
            raise HTTPException(502, "Transcription failed") from error
        finally:
            active -= 1

    router.add_api_route("/v1/audio/transcriptions", transcribe, methods=["POST"])
    return router


def install_transcription_route(app: FastAPI) -> None:
    """Mount batch transcription once without changing voice-session routing."""
    if not getattr(app.state, "batch_transcription_installed", False):
        app.include_router(create_transcription_router())
        app.state.batch_transcription_installed = True
