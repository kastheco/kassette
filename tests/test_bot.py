import pytest

from kassette.bot import _SessionCloser  # pyright: ignore[reportPrivateUsage]


async def test_session_cleanup_is_attempted_once_after_failure() -> None:
    cancel_attempts = 0
    provider_close_attempts = 0

    async def fail_cancel() -> None:
        nonlocal cancel_attempts
        cancel_attempts += 1
        raise RuntimeError("worker cancellation failed")

    async def close_provider() -> None:
        nonlocal provider_close_attempts
        provider_close_attempts += 1

    close = _SessionCloser(fail_cancel, close_provider)

    with pytest.raises(RuntimeError, match="worker cancellation failed"):
        await close()
    await close()

    assert cancel_attempts == 1
    assert provider_close_attempts == 1
