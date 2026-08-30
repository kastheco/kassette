from __future__ import annotations

import pytest

from kassette.terminal_protocol import (
    PROTOCOL_VERSION,
    REQUIRED_CAPABILITIES,
    ProtocolError,
    client_message,
    hello_message,
)


def test_hello_advertises_terminal_voice_capabilities_without_text() -> None:
    message = hello_message("session-1")

    assert message == {
        "label": "kassette",
        "type": "terminal.hello",
        "data": {
            "session_id": "session-1",
            "protocol_version": PROTOCOL_VERSION,
            "capabilities": sorted(REQUIRED_CAPABILITIES),
        },
    }


def test_client_message_accepts_bounded_shared_envelope() -> None:
    message = client_message(
        {"label": "kassette", "type": "input.pause", "data": {}},
    )

    assert message["type"] == "input.pause"


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"label": "other", "type": "input.pause", "data": {}},
        {"label": "kassette", "type": "input.pause", "data": []},
        {"label": "kassette", "type": "x" * 65, "data": {}},
        {"label": "kassette", "type": "tts.speak", "data": {"text": "x" * 32_001}},
    ],
)
def test_client_message_rejects_malformed_or_unbounded_input(value: object) -> None:
    with pytest.raises(ProtocolError):
        client_message(value)
