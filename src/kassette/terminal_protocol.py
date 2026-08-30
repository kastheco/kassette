"""Bounded messages shared by terminal voice clients and the kassette service."""

from __future__ import annotations

import json
from typing import Any, cast

PROTOCOL_VERSION = 1
REQUIRED_CAPABILITIES = frozenset(
    {
        "audio.input",
        "audio.levels",
        "audio.output",
        "input.pause",
        "output.cancel",
        "output.mute",
        "transcript.stream",
        "tts.queue",
    }
)
_MAX_MESSAGE_TYPE = 64
_MAX_TEXT = 32_000
_MAX_MESSAGE_BYTES = 64_000


class ProtocolError(ValueError):
    """A terminal client sent a malformed or unbounded message."""


def envelope(message_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """Build one shared kassette envelope."""
    return {"label": "kassette", "type": message_type, "data": data}


def hello_message(session_id: str) -> dict[str, Any]:
    """Describe the protocol before the session acquires local audio."""
    return envelope(
        "terminal.hello",
        {
            "session_id": session_id,
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": sorted(REQUIRED_CAPABILITIES),
        },
    )


def client_message(value: object) -> dict[str, Any]:
    """Validate a client envelope before it reaches a provider runtime."""
    if not isinstance(value, dict):
        raise ProtocolError("message must be an object")
    message = cast(dict[str, object], value)
    if message.get("label") != "kassette":
        raise ProtocolError("message label must be kassette")
    message_type = message.get("type")
    data = message.get("data")
    if (
        not isinstance(message_type, str)
        or not message_type
        or len(message_type) > _MAX_MESSAGE_TYPE
    ):
        raise ProtocolError("message type is invalid")
    if not isinstance(data, dict):
        raise ProtocolError("message data must be an object")
    payload = cast(dict[str, object], data)
    try:
        encoded_size = len(json.dumps(payload, ensure_ascii=False).encode())
    except (TypeError, ValueError) as error:
        raise ProtocolError("message data must be JSON") from error
    if encoded_size > _MAX_MESSAGE_BYTES:
        raise ProtocolError("message data is too large")
    text = payload.get("text")
    if text is not None and (not isinstance(text, str) or len(text) > _MAX_TEXT):
        raise ProtocolError("message text is invalid")
    return {"label": "kassette", "type": message_type, "data": payload}
