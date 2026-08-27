from kassette.credentials import CodexCredentials
from kassette.providers.quicksilver.protocol import (
    LIVE_MODEL,
    build_live_headers,
    build_session_payload,
    parse_call_id,
    parse_provider_event,
    safe_fixture,
)


def test_session_payload_selects_gpt_live() -> None:
    payload = build_session_payload("answer directly", "sol")

    assert payload["model"] == LIVE_MODEL
    assert payload["audio"] == {"output": {"voice": "sol"}}


def test_live_headers_bind_session_without_leaking_from_repr() -> None:
    credentials = CodexCredentials(access_token="secret", account_id="account")

    headers = build_live_headers(credentials, "session-1", "realtime-1")

    assert headers["OpenAI-Alpha"] == "quicksilver=v2"
    assert headers["session-id"] == "session-1"
    assert headers["Authorization"] == "Bearer secret"
    assert "secret" not in repr(credentials)


def test_parse_call_id_ignores_invalid_location() -> None:
    assert parse_call_id("https://example.test/calls/rtc_123") == "rtc_123"
    assert parse_call_id("https://example.test/calls/not-a-call") is None


def test_parse_transcript_and_unknown_events() -> None:
    transcript = parse_provider_event(
        '{"type":"turn.done","turn":{"role":"assistant","transcript":"hello"}}'
    )
    unknown = parse_provider_event('{"type":"future.event","secret":"not retained"}')

    assert transcript is not None
    assert transcript.role == "assistant"
    assert transcript.text == "hello"
    assert unknown is not None
    assert safe_fixture(unknown) == {
        "type": "unknown",
        "role": None,
        "wire_type": "future.event",
        "has_text": "false",
        "has_error": "false",
    }
