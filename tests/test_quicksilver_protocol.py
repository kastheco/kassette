from kassette.credentials import CodexCredentials
from kassette.providers.quicksilver.protocol import (
    LIVE_MODEL,
    build_delegation_response,
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


def test_delegation_response_returns_pi_answer_to_the_matching_live_turn() -> None:
    assert build_delegation_response("delegation-1", "Pi answer") == {
        "type": "delegation.context.append",
        "delegation_item_id": "delegation-1",
        "content": [{"type": "input_text", "text": "Pi answer"}],
    }


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
        "wire_type": "present",
        "has_text": "false",
        "has_error": "false",
    }


def test_provider_text_and_identifiers_are_bounded() -> None:
    transcript = parse_provider_event(
        {
            "type": "turn.done",
            "turn": {"role": "assistant", "transcript": "x" * 100_000},
        }
    )
    unknown = parse_provider_event({"type": "x" * 10_000})

    assert transcript is not None
    assert transcript.text is not None
    assert len(transcript.text) == 32_000
    assert unknown is not None
    assert unknown.wire_type is not None
    assert len(unknown.wire_type) == 128


def test_direct_provider_collections_are_bounded_before_traversal() -> None:
    oversized_event: dict[str, object] = {
        "type": "turn.done",
        "turn": {"role": "assistant", "transcript": "hello"},
        **{f"extra-{index}": index for index in range(64)},
    }
    oversized_content = parse_provider_event(
        {
            "type": "delegation.created",
            "item": {
                "id": "delegation-1",
                "content": [{"text": "x"} for _ in range(65)],
            },
        }
    )
    bounded_content = parse_provider_event(
        {
            "type": "delegation.created",
            "item": {
                "id": "delegation-1",
                "content": [{"text": "x" * 10_000} for _ in range(64)],
            },
        }
    )

    assert parse_provider_event(oversized_event) is None
    assert oversized_content is None
    assert bounded_content is not None
    assert bounded_content.text is not None
    assert len(bounded_content.text) == 32_000
