"""Pipecat runner entry point for the local kassette service."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.runner.utils import create_transport  # pyright: ignore[reportUnknownVariableType]
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from kassette.credentials import CodexCredentialProvider, PiAuthCredentialProvider
from kassette.diagnostics import LifecycleDiagnostics
from kassette.domain import SessionEvent
from kassette.providers.quicksilver.service import GPTLiveService, TransportFactory
from kassette.providers.quicksilver.transport import QuicksilverTransport
from kassette.sessions import (
    LiveSessionCoordinator,
    SessionHandle,
    SessionNotFoundError,
    SessionRegistry,
)

_registry = SessionRegistry()
_lifecycle = LiveSessionCoordinator()
_diagnostics = LifecycleDiagnostics()


class _SessionCloser:
    def __init__(
        self,
        cancel_worker: Callable[[], Awaitable[None]],
        close_provider: Callable[[], Awaitable[None]],
    ) -> None:
        self._cancel_worker = cancel_worker
        self._close_provider = close_provider
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._failure_reported = False

    async def __call__(self) -> None:
        async with self._lock:
            if self._task is None:
                self._task = asyncio.create_task(self._close())
            task = self._task
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            await self._report_failure(error)
        except BaseException as error:
            await self._report_failure(error)

    async def _close(self) -> None:
        failure: BaseException | None = None
        try:
            await self._cancel_worker()
        except BaseException as error:
            failure = error
        try:
            await self._close_provider()
        except BaseException as error:
            failure = failure or error
        if failure is not None:
            raise failure

    async def _report_failure(self, error: BaseException) -> None:
        async with self._lock:
            if self._failure_reported:
                return
            self._failure_reported = True
        raise error


async def _log_event(event: SessionEvent) -> None:
    safe = await _diagnostics.record(event)
    logger.info("kassette_event {}", json.dumps(safe, ensure_ascii=True, sort_keys=True))


async def run_session(
    transport: BaseTransport,
    runner_args: SmallWebRTCRunnerArguments,
    *,
    registry: SessionRegistry = _registry,
    lifecycle: LiveSessionCoordinator = _lifecycle,
    credential_provider: CodexCredentialProvider | None = None,
    provider_transport_factory: TransportFactory = QuicksilverTransport,
) -> None:
    session_id = runner_args.session_id or str(uuid4())
    snapshot = await registry.create(session_id)
    service = GPTLiveService(
        session_id=session_id,
        generation=snapshot.generation,
        registry=registry,
        credentials=credential_provider or PiAuthCredentialProvider(),
        event_sink=_log_event,
        transport_factory=provider_transport_factory,
    )
    pipeline = Pipeline([transport.input(), service, transport.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=False),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport: BaseTransport, _client: Any) -> None:
        logger.info("kassette client connected to voice session {}", session_id)

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    close_session = _SessionCloser(
        worker.cancel,
        service._close,  # pyright: ignore[reportPrivateUsage]
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport: BaseTransport, _client: Any) -> None:
        logger.info("kassette client disconnected from voice session {}", session_id)
        await close_session()

    handle = SessionHandle(session_id, snapshot.generation)
    try:
        previous_closed = await lifecycle.replace(
            handle=handle,
            close=close_session,
        )
        if not previous_closed:
            logger.warning("kassette previous voice session cleanup failed during replacement")
        started = await lifecycle.run_active(handle, runner.run)
        if not started:
            logger.warning("kassette superseded voice session did not start")
    finally:
        try:
            await close_session()
        finally:
            await lifecycle.clear(handle)
            try:
                final_snapshot = await registry.get(session_id)
            except SessionNotFoundError:
                final_snapshot = None
            if final_snapshot is not None and final_snapshot.state.value in {"closed", "failed"}:
                await registry.reap(session_id, expected_generation=snapshot.generation)


async def bot(runner_args: RunnerArguments) -> None:
    """Create one kassette voice session for one SmallWebRTC client."""
    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise RuntimeError("the first kassette delivery only supports SmallWebRTC")
    transport = await create_transport(
        runner_args,
        {
            "webrtc": lambda: TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=16_000,
                audio_out_sample_rate=24_000,
            )
        },
    )
    await run_session(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
