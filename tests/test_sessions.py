import pytest

from kassette.domain import SessionState
from kassette.sessions import (
    AudioDeviceBusyError,
    InvalidSessionTransitionError,
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
