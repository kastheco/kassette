from __future__ import annotations

import pytest

from kassette.domain import SessionEvent, SessionEventType, SessionState, TranscriptRole
from kassette.sessions import SessionNotFoundError, SessionRegistry
from kassette.terminal_runtime import (
    TerminalAudioLease,
    TerminalInputControl,
    close_terminal_session,
    session_event_envelope,
    terminal_output_active_for_event,
)


class FakeRuntime:
    def __init__(self, results: list[bool]) -> None:
        self.results = results
        self.messages: list[object] = []

    async def handle_client_message(self, message: object) -> bool:
        self.messages.append(message)
        return self.results.pop(0)


async def test_desired_pause_is_retried_after_adapter_ready_and_only_then_acks_telemetry() -> None:
    runtime = FakeRuntime([False, True])
    telemetry_states: list[bool] = []

    async def set_telemetry_paused(paused: bool) -> None:
        telemetry_states.append(paused)

    control = TerminalInputControl()

    assert not await control.request(True, runtime, set_telemetry_paused)
    assert telemetry_states == []
    assert await control.adapter_ready(runtime, set_telemetry_paused)
    assert await control.adapter_ready(runtime, set_telemetry_paused)
    assert telemetry_states == [True]
    assert [
        message["type"]  # type: ignore[index]
        for message in runtime.messages
    ] == ["input.pause", "input.pause"]


def test_quicksilver_delegation_and_transcript_reach_terminal_envelopes() -> None:
    delegation = session_event_envelope(
        SessionEvent(
            session_id="voice",
            type=SessionEventType.DELEGATION_REQUESTED,
            text="inspect the repository",
            metadata={"delegation_id": "delegation-1"},
        )
    )
    transcript = session_event_envelope(
        SessionEvent(
            session_id="voice",
            type=SessionEventType.TRANSCRIPT_FINAL,
            role=TranscriptRole.ASSISTANT,
            text="done",
        )
    )

    assert delegation == {
        "label": "kassette",
        "type": "delegation.requested",
        "data": {
            "session_id": "voice",
            "delegation_id": "delegation-1",
            "text": "inspect the repository",
        },
    }
    assert transcript == {
        "label": "kassette",
        "type": "transcript.final",
        "data": {"session_id": "voice", "role": "assistant", "text": "done"},
    }


@pytest.mark.parametrize(
    ("event_type", "expected_type"),
    [
        (SessionEventType.INPUT_AUDIO_STARTED, "input.audio_started"),
        (SessionEventType.SPEECH_STARTED, "speech.started"),
        (SessionEventType.INTERRUPTED, "session.interrupted"),
    ],
)
def test_runtime_speech_and_barge_in_events_reach_terminal_envelopes(
    event_type: SessionEventType,
    expected_type: str,
) -> None:
    message = session_event_envelope(
        SessionEvent(
            session_id="voice",
            type=event_type,
            state=SessionState.SPEAKING,
            metadata={"provider_generation": 2},
        )
    )

    assert message == {
        "label": "kassette",
        "type": expected_type,
        "data": {
            "session_id": "voice",
            "state": "speaking",
            "provider_generation": 2,
        },
    }


def test_delegation_request_reopens_terminal_input_after_stale_speaking_event() -> None:
    assert terminal_output_active_for_event(SessionEventType.SPEECH_STARTED) is True
    assert terminal_output_active_for_event(SessionEventType.DELEGATION_REQUESTED) is False
    assert terminal_output_active_for_event(SessionEventType.SPEECH_STOPPED) is False


async def test_terminal_audio_lease_acquires_closes_releases_and_reaps() -> None:
    registry = SessionRegistry()
    snapshot = await registry.create("terminal", initial_provider_id="cascade")
    lease = TerminalAudioLease(registry, snapshot)

    await lease.open()
    assert await registry.audio_owner() == "terminal"
    assert (await registry.get("terminal")).state is SessionState.CONNECTING

    await lease.close()
    await lease.close()
    assert await registry.audio_owner() is None
    with pytest.raises(SessionNotFoundError):
        await registry.get("terminal")


async def test_reconnect_close_releases_audio_before_replacement_acquires_it() -> None:
    class Worker:
        async def cancel(self) -> None:
            return None

    registry = SessionRegistry()
    first_snapshot = await registry.create("first", initial_provider_id="cascade")
    first_lease = TerminalAudioLease(registry, first_snapshot)
    await first_lease.open()

    await close_terminal_session(Worker(), first_lease)

    replacement_snapshot = await registry.create("replacement", initial_provider_id="cascade")
    replacement_lease = TerminalAudioLease(registry, replacement_snapshot)
    await replacement_lease.open()
    assert await registry.audio_owner() == "replacement"
    await replacement_lease.close()
