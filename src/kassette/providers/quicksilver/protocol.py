"""Quicksilver v2 wire messages isolated from kassette's public contracts."""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass
from typing import Any, Literal, cast

from kassette.credentials import CodexCredentials

LIVE_MODEL = "gpt-live-1-codex"
LIVE_VOICES = (
    "arbor",
    "breeze",
    "cove",
    "ember",
    "juniper",
    "maple",
    "sol",
    "spruce",
    "vale",
)
LiveVoice = Literal[
    "arbor",
    "breeze",
    "cove",
    "ember",
    "juniper",
    "maple",
    "sol",
    "spruce",
    "vale",
]
DEFAULT_LIVE_VOICE: LiveVoice = "sol"
CODEX_BASE_URL = "https://chatgpt.com/backend-api"
CODEX_CLIENT_VERSION = "0.144.1"
SIGNALING_URL = f"{CODEX_BASE_URL}/codex/realtime/calls?intent=quicksilver&architecture=avas"
LIVE_ORIGINATOR = "Codex Desktop"
_CALL_ID = re.compile(r"^rtc_[\w-]+$")
_MAX_PROVIDER_PAYLOAD_BYTES = 262_144
_MAX_PROVIDER_TEXT_CHARS = 32_000
_MAX_PROVIDER_ID_CHARS = 256
_MAX_PROVIDER_TYPE_CHARS = 128
_MAX_PROVIDER_ERROR_CHARS = 2_048
_MAX_PROVIDER_AUDIO_BYTES = 196_608
_MAX_PROVIDER_COLLECTION_ITEMS = 64

ProviderEventType = Literal[
    "session.started",
    "session.updated",
    "output_audio.delta",
    "input_transcript.added",
    "output_transcript.added",
    "turn.done",
    "delegation.created",
    "error",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class ProviderEvent:
    type: ProviderEventType
    session_id: str | None = None
    role: Literal["user", "assistant"] | None = None
    text: str | None = None
    delegation_id: str | None = None
    wire_type: str | None = None
    message: str | None = None
    audio: bytes | None = None
    sample_rate: int | None = None
    num_channels: int | None = None


def build_session_payload(instructions: str, voice: LiveVoice) -> dict[str, Any]:
    return {
        "model": LIVE_MODEL,
        "instructions": instructions,
        "audio": {"output": {"voice": voice}},
        "delegation": {"type": "client"},
    }


def build_live_headers(
    credentials: CodexCredentials,
    session_id: str,
    realtime_session_id: str,
) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {credentials.access_token}",
        "OpenAI-Alpha": "quicksilver=v2",
        "User-Agent": f"Codex Desktop/{CODEX_CLIENT_VERSION}",
        "x-session-id": realtime_session_id,
        "originator": LIVE_ORIGINATOR,
        "version": CODEX_CLIENT_VERSION,
        "session-id": session_id,
        "thread-id": session_id,
        "chatgpt-account-id": credentials.account_id,
    }


def parse_call_id(location: str | None) -> str | None:
    if not location:
        return None
    for segment in location.split("?", 1)[0].split("/"):
        if _CALL_ID.fullmatch(segment):
            return segment
    return None


def sideband_url(call_id: str) -> str:
    if not _CALL_ID.fullmatch(call_id):
        raise ValueError("invalid Quicksilver call ID")
    return f"wss://api.openai.com/v1/live/{call_id}"


def build_session_close() -> dict[str, str]:
    return {"type": "session.close"}


def build_delegation_response(
    delegation_id: str,
    text: str,
    *,
    channel: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "type": "delegation.context.append",
        "delegation_item_id": delegation_id,
        "content": [{"type": "input_text", "text": text}],
    }
    if channel is not None:
        message["channel"] = channel
    return message


def build_delegation_unavailable(delegation_id: str) -> dict[str, Any]:
    return build_delegation_response(
        delegation_id,
        "Delegation is unavailable in this voice client. Answer directly.",
    )


def build_spoken_context(text: str) -> dict[str, Any]:
    """Append client-owned speech when the provider skipped delegation."""
    return {
        "type": "session.context.append",
        "channel": "speakable",
        "content": [{"type": "input_text", "text": text}],
    }


def parse_provider_event(payload: str | bytes | dict[str, Any]) -> ProviderEvent | None:
    parsed: object = payload
    if isinstance(parsed, bytes):
        if len(parsed) > _MAX_PROVIDER_PAYLOAD_BYTES:
            return None
        try:
            parsed = parsed.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(parsed, str):
        if len(parsed) > _MAX_PROVIDER_PAYLOAD_BYTES:
            return None
        if len(parsed.encode("utf-8")) > _MAX_PROVIDER_PAYLOAD_BYTES:
            return None
        try:
            parsed = cast(object, json.loads(parsed))
        except json.JSONDecodeError:
            return None
    event = _record(parsed)
    if event is None:
        return None
    event_type = event.get("type")
    if not isinstance(event_type, str):
        return None

    if event_type == "session.started" or event_type == "session.updated":
        session = _record(event.get("session"))
        if session is None:
            return None
        provider_session_id = session.get("id")
        if not isinstance(provider_session_id, str):
            return None
        typed_event: Literal["session.started", "session.updated"] = event_type
        return ProviderEvent(
            type=typed_event,
            session_id=provider_session_id[:_MAX_PROVIDER_ID_CHARS],
        )
    if event_type == "input_transcript.added" or event_type == "output_transcript.added":
        item = _record(event.get("item"))
        if item is None:
            return None
        text = item.get("text")
        if not isinstance(text, str):
            return None
        role: Literal["user", "assistant"] = (
            "user" if event_type == "input_transcript.added" else "assistant"
        )
        transcript_event: Literal["input_transcript.added", "output_transcript.added"] = event_type
        return ProviderEvent(
            type=transcript_event,
            role=role,
            text=text[:_MAX_PROVIDER_TEXT_CHARS],
        )
    if event_type == "turn.done":
        turn = _record(event.get("turn"))
        if turn is None:
            return None
        raw_role = turn.get("role")
        text = turn.get("transcript")
        if raw_role not in {"user", "assistant"} or not isinstance(text, str):
            return None
        role = cast(Literal["user", "assistant"], raw_role)
        return ProviderEvent(
            type="turn.done",
            role=role,
            text=text[:_MAX_PROVIDER_TEXT_CHARS],
        )
    if event_type == "delegation.created":
        item = _record(event.get("item"))
        if item is None:
            return None
        delegation_id = item.get("id")
        if not isinstance(delegation_id, str):
            return None
        content = item.get("content")
        text_parts: list[str] = []
        text_chars = 0
        if isinstance(content, list):
            content_items = cast(list[object], content)
            if len(content_items) > _MAX_PROVIDER_COLLECTION_ITEMS:
                return None
            for candidate in content_items:
                entry = _record(candidate)
                text = entry.get("text") if entry else None
                if not isinstance(text, str):
                    continue
                separator_chars = 1 if text_parts else 0
                remaining = _MAX_PROVIDER_TEXT_CHARS - text_chars - separator_chars
                if remaining <= 0:
                    break
                text_parts.append(text[:remaining])
                text_chars += min(len(text), remaining) + separator_chars
        return ProviderEvent(
            type="delegation.created",
            delegation_id=delegation_id[:_MAX_PROVIDER_ID_CHARS],
            text="\n".join(text_parts)[:_MAX_PROVIDER_TEXT_CHARS],
        )
    if event_type == "error":
        message = event.get("message")
        if not isinstance(message, str):
            error = _record(event.get("error"))
            message = error.get("message") if error is not None else None
        return ProviderEvent(
            type="error",
            message=(
                message[:_MAX_PROVIDER_ERROR_CHARS]
                if isinstance(message, str)
                else "Quicksilver provider error"
            ),
        )
    if event_type == "output_audio.delta":
        encoded_audio = event.get("audio")
        if not isinstance(encoded_audio, str):
            return None
        try:
            audio = base64.b64decode(encoded_audio, validate=True)
        except (binascii.Error, ValueError):
            return None
        if not audio or len(audio) > _MAX_PROVIDER_AUDIO_BYTES:
            return None
        return ProviderEvent(
            type="output_audio.delta",
            audio=audio,
            sample_rate=24_000,
            num_channels=1,
        )
    return ProviderEvent(type="unknown", wire_type=event_type[:_MAX_PROVIDER_TYPE_CHARS])


def _record(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    record = cast(dict[object, object], value)
    if len(record) > _MAX_PROVIDER_COLLECTION_ITEMS:
        return None
    return cast(dict[str, object], record)


def safe_fixture(event: ProviderEvent) -> dict[str, str | None]:
    """Return bounded event metadata without audio, auth, SDP, or raw payloads."""
    return {
        "type": event.type,
        "role": event.role,
        "wire_type": "present" if event.wire_type is not None else None,
        "has_text": str(bool(event.text)).lower(),
        "has_error": str(bool(event.message)).lower(),
    }
