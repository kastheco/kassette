# pyright: reportPrivateUsage=false

import asyncio

import pytest

from kassette.bot import _await_finalizer, _SessionCloser
from kassette.providers.builtin import PausableGeminiSTTService


class RecordingPausableGeminiSTTService(PausableGeminiSTTService):
    _INPUT_PAUSE_GRACE_SECS = 0

    def __init__(self) -> None:
        self._input_paused = False
        self._input_pause_lock = None
        self.calls: list[str] = []

    async def _send_finalization_signal(self) -> None:
        self.calls.append("finalize")

    async def _disconnect(self) -> None:
        self.calls.append("disconnect")

    async def _connect(self) -> None:
        self.calls.append("connect")


async def test_paused_gemini_input_disconnects_and_reconnects_idempotently() -> None:
    service = RecordingPausableGeminiSTTService()

    await service.pause_input()
    await service.pause_input()
    await service.resume_input()
    await service.resume_input()

    assert service.calls == ["finalize", "disconnect", "connect"]


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


async def test_finalizer_completes_before_caller_cancellation_propagates() -> None:
    finalizer_started = asyncio.Event()
    allow_finalizer = asyncio.Event()
    finalized = False

    async def finalize() -> None:
        nonlocal finalized
        finalizer_started.set()
        await allow_finalizer.wait()
        finalized = True

    caller = asyncio.create_task(_await_finalizer(finalize))
    await finalizer_started.wait()
    caller.cancel()
    await asyncio.sleep(0)

    assert not caller.done()
    assert not finalized

    allow_finalizer.set()
    with pytest.raises(asyncio.CancelledError):
        await caller
    assert finalized
