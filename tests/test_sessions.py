import pytest

from kassette.domain import SessionState
from kassette.sessions import (
    AudioDeviceBusyError,
    InvalidSessionTransitionError,
    LiveSessionCoordinator,
    SessionHandle,
    SessionRegistry,
)


async def test_registry_tracks_independent_sessions_and_one_audio_lease() -> None:
    registry = SessionRegistry()
    first = await registry.create("first")
    second = await registry.create("second")

    await registry.acquire_audio(first.id)

    with pytest.raises(AudioDeviceBusyError):
        await registry.acquire_audio(second.id)

    assert await registry.audio_owner() == first.id


async def test_terminal_transition_releases_audio_lease() -> None:
    registry = SessionRegistry()
    session = await registry.create("voice")
    await registry.acquire_audio(session.id)
    await registry.transition(session.id, SessionState.CLOSING)

    closed = await registry.transition(session.id, SessionState.CLOSED)

    assert closed.state is SessionState.CLOSED
    assert await registry.audio_owner() is None


async def test_registry_rejects_invalid_transition() -> None:
    registry = SessionRegistry()
    session = await registry.create("voice")

    with pytest.raises(InvalidSessionTransitionError):
        await registry.transition(session.id, SessionState.SPEAKING)


async def test_recreated_session_rejects_stale_lease_release() -> None:
    registry = SessionRegistry()
    first = await registry.create("voice")
    await registry.acquire_audio("voice", expected_generation=first.generation)
    await registry.transition("voice", SessionState.FAILED)
    await registry.reap("voice", expected_generation=first.generation)

    second = await registry.create("voice")
    await registry.acquire_audio("voice", expected_generation=second.generation)
    await registry.release_audio("voice", expected_generation=first.generation)

    assert second.generation == first.generation + 1
    assert await registry.audio_owner() == "voice"


async def test_reconnect_closes_previous_session_and_stale_clear_is_ignored() -> None:
    coordinator = LiveSessionCoordinator()
    closed: list[str] = []
    first = SessionHandle("first", 1)
    second = SessionHandle("second", 1)

    async def close_first() -> None:
        closed.append("first")

    async def close_second() -> None:
        closed.append("second")

    await coordinator.replace(first, close_first)
    await coordinator.replace(second, close_second)
    await coordinator.clear(first)

    assert closed == ["first"]
    assert await coordinator.active() == second


async def test_reconnect_starts_replacement_when_previous_cleanup_fails() -> None:
    coordinator = LiveSessionCoordinator()
    first = SessionHandle("first", 1)
    second = SessionHandle("second", 1)
    close_attempts = 0

    async def fail_close() -> None:
        nonlocal close_attempts
        close_attempts += 1
        raise RuntimeError("previous cleanup failed")

    async def close_second() -> None:
        return

    assert await coordinator.replace(first, fail_close)
    assert not await coordinator.replace(second, close_second)

    assert close_attempts == 1
    assert await coordinator.active() == second
