"""Pipecat runner entry point for the local kassette service."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

from loguru import logger
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.runner.utils import create_transport  # pyright: ignore[reportUnknownVariableType]
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from kassette.credentials import PiAuthCredentialProvider
from kassette.domain import SessionEvent
from kassette.providers.quicksilver.service import GPTLiveService
from kassette.sessions import SessionRegistry

_registry = SessionRegistry()


async def _log_event(event: SessionEvent) -> None:
    safe = {
        "session_id": event.session_id,
        "type": event.type,
        "state": event.state,
        "role": event.role,
        "has_text": event.text is not None,
        "text_chars": len(event.text) if event.text else 0,
        "provider_type": event.provider_type,
        "error_code": event.error_code,
        "metadata_keys": sorted(event.metadata),
    }
    logger.info("kassette_event {}", json.dumps(safe, default=str, ensure_ascii=False))


async def run_session(
    transport: BaseTransport,
    runner_args: SmallWebRTCRunnerArguments,
) -> None:
    session_id = runner_args.session_id or str(uuid4())
    await _registry.create(session_id)
    service = GPTLiveService(
        session_id=session_id,
        registry=_registry,
        credentials=PiAuthCredentialProvider(),
        event_sink=_log_event,
    )
    pipeline = Pipeline([transport.input(), service, transport.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=False),
    )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport: BaseTransport, _client: Any) -> None:
        logger.info("kassette client connected to voice session {}", session_id)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport: BaseTransport, _client: Any) -> None:
        logger.info("kassette client disconnected from voice session {}", session_id)
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    try:
        await runner.run()
    finally:
        snapshot = await _registry.get(session_id)
        if snapshot.state.value in {"closed", "failed"}:
            await _registry.reap(session_id)


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
