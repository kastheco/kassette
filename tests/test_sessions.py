import asyncio

import pytest

from kassette.domain import SessionState
from kassette.sessions import (
    AudioDeviceBusyError,
    InvalidSessionTransitionError,
    LiveSessionCoordinator,
    SessionGenerationMismatchError,
    SessionHandle,
    SessionRegistry,
    SessionRegistryError,
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


async def test_overlapping_replacements_are_serialized() -> None:
    coordinator = LiveSessionCoordinator()
    first = SessionHandle("first", 1)
    second = SessionHandle("second", 1)
    third = SessionHandle("third", 1)
    first_close_started = asyncio.Event()
    release_first_close = asyncio.Event()

    async def close_first() -> None:
        first_close_started.set()
        await release_first_close.wait()

    async def close_session() -> None:
        return

    await coordinator.replace(first, close_first)
    replace_second = asyncio.create_task(coordinator.replace(second, close_session))
    await first_close_started.wait()
    replace_third = asyncio.create_task(coordinator.replace(third, close_session))
    await asyncio.sleep(0)

    assert not replace_third.done()

    release_first_close.set()
    assert await replace_second
    assert await replace_third
    assert await coordinator.active() == third


async def test_replacement_propagates_caller_cancellation() -> None:
    coordinator = LiveSessionCoordinator()
    first = SessionHandle("first", 1)
    second = SessionHandle("second", 1)
    close_started = asyncio.Event()
    never_release = asyncio.Event()

    async def block_close() -> None:
        close_started.set()
        await never_release.wait()

    async def close_second() -> None:
        return

    await coordinator.replace(first, block_close)
    replacement = asyncio.create_task(coordinator.replace(second, close_second))
    await close_started.wait()
    replacement.cancel()

    with pytest.raises(asyncio.CancelledError):
        await replacement

    assert await coordinator.active() == first


async def test_newer_replacement_cancels_started_stale_runner() -> None:
    coordinator = LiveSessionCoordinator()
    first = SessionHandle("first", 1)
    second = SessionHandle("second", 1)
    runner_started = asyncio.Event()
    never_finish = asyncio.Event()
    close_attempts = 0

    async def close_first() -> None:
        nonlocal close_attempts
        close_attempts += 1

    async def close_second() -> None:
        return

    async def run_first() -> None:
        runner_started.set()
        await never_finish.wait()

    await coordinator.replace(first, close_first)
    running = asyncio.create_task(coordinator.run_active(first, run_first))
    await runner_started.wait()
    await coordinator.replace(second, close_second)

    with pytest.raises(asyncio.CancelledError):
        await running

    assert close_attempts == 1
    assert await coordinator.active() == second


async def test_replacement_waits_for_cancellation_delayed_stale_runner() -> None:
    coordinator = LiveSessionCoordinator()
    first = SessionHandle("first", 1)
    second = SessionHandle("second", 1)
    runner_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    allow_stale_exit = asyncio.Event()

    async def close_session() -> None:
        return

    async def run_first() -> None:
        runner_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await allow_stale_exit.wait()
            raise

    await coordinator.replace(first, close_session)
    running = asyncio.create_task(coordinator.run_active(first, run_first))
    await runner_started.wait()
    replacement = asyncio.create_task(coordinator.replace(second, close_session))
    await cancellation_seen.wait()

    assert not replacement.done()
    assert await coordinator.active() == first

    allow_stale_exit.set()
    assert await replacement
    with pytest.raises(asyncio.CancelledError):
        await running
    assert await coordinator.active() == second


@pytest.mark.parametrize("session_id", ["x" * 97, "line\nbreak"])
async def test_registry_rejects_unbounded_or_controlled_session_ids(session_id: str) -> None:
    registry = SessionRegistry()

    with pytest.raises(SessionRegistryError, match="invalid voice session identifier"):
        await registry.create(session_id)


async def test_provider_switch_generation_fences_stale_adapter_callbacks() -> None:
    registry = SessionRegistry()
    created = await registry.create("voice", initial_provider_id="cascade")
    await registry.transition(
        "voice",
        SessionState.CONNECTING,
        expected_provider_generation=created.provider_generation,
    )
    await registry.transition(
        "voice",
        SessionState.LISTENING,
        expected_provider_generation=created.provider_generation,
    )

    switching = await registry.begin_provider_switch(
        "voice",
        "quicksilver",
        expected_provider_generation=created.provider_generation,
    )
    active = await registry.activate_provider(
        "voice",
        "quicksilver",
        expected_provider_generation=switching.provider_generation,
    )

    with pytest.raises(SessionGenerationMismatchError):
        await registry.transition(
            "voice",
            SessionState.LISTENING,
            expected_provider_generation=created.provider_generation,
        )

    assert switching.state is SessionState.SWITCHING
    assert switching.active_provider == "cascade"
    assert switching.desired_provider == "quicksilver"
    assert switching.provider_generation == created.provider_generation + 1
    assert active.state is SessionState.CONNECTING
    assert active.active_provider == "quicksilver"
