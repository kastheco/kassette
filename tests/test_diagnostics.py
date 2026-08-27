import json

from kassette.diagnostics import LifecycleDiagnostics
from kassette.domain import SessionEvent, SessionEventType
from kassette.providers.quicksilver.protocol import (
    ProviderEvent,
    parse_provider_event,
    safe_fixture,
)


async def test_lifecycle_diagnostics_are_bounded_and_content_free() -> None:
    secret = "Bearer credential-secret"
    hostile = "\n\r" + "x" * 10_000
    recorder = LifecycleDiagnostics()
    event = SessionEvent(
        session_id="session-1",
        type=SessionEventType.ERROR,
        text=f"transcript {secret}",
        provider_type=f"provider {secret} {hostile}",
        error_code="provider_error",
        metadata={f"{secret}-{index}-{hostile}": secret for index in range(100)},
    )

    record = await recorder.record(event)
    encoded = json.dumps(record)

    assert secret not in encoded
    assert "transcript" not in encoded
    assert record["has_text"] is True
    assert record["metadata_key_count"] == 64
    assert all(not isinstance(value, str) or len(value) <= 96 for value in record.values())


def test_provider_fixture_drops_provider_controlled_content() -> None:
    secret = "credential auth sdp raw-audio transcript"
    fixture = safe_fixture(
        ProviderEvent(
            type="unknown",
            wire_type=secret * 1_000,
            text=secret,
            message=secret,
        )
    )

    encoded = json.dumps(fixture)
    assert secret not in encoded
    assert len(encoded) < 160


def test_wire_fixture_drops_auth_sdp_audio_transcript_and_hostile_metadata() -> None:
    secret = "credential auth sdp raw-audio transcript hostile-metadata"
    event = parse_provider_event(
        json.dumps(
            {
                "type": "future.event",
                "Authorization": secret,
                "sdp": secret * 100,
                "audio": secret * 100,
                "transcript": secret * 100,
                "metadata": {secret * 100: secret * 100},
            }
        )
    )

    assert event is not None
    encoded = json.dumps(safe_fixture(event))
    assert secret not in encoded
    assert len(encoded) < 160


def test_oversized_provider_payload_is_rejected() -> None:
    payload = json.dumps({"type": "future.event", "value": "x" * 300_000})

    assert parse_provider_event(payload) is None
