import asyncio

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


async def test_session_cleanup_continues_after_caller_cancellation() -> None:
    cleanup_started = asyncio.Event()
    allow_cleanup = asyncio.Event()
    provider_closed = False

    async def cancel_worker() -> None:
        cleanup_started.set()
        await allow_cleanup.wait()

    async def close_provider() -> None:
        nonlocal provider_closed
        provider_closed = True

    close = _SessionCloser(cancel_worker, close_provider)
    cancelled_caller = asyncio.create_task(close())
    await cleanup_started.wait()
    cancelled_caller.cancel()

    with pytest.raises(asyncio.CancelledError):
        await cancelled_caller

    replacement_cleanup = asyncio.create_task(close())
    await asyncio.sleep(0)
    assert not replacement_cleanup.done()

    allow_cleanup.set()
    await replacement_cleanup
    assert provider_closed
