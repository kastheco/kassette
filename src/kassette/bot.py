"""Pipecat runner entry point for the local kassette service."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.runner.utils import create_transport  # pyright: ignore[reportUnknownVariableType]
from pipecat.services.fish.tts import FishAudioTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.workers.runner import WorkerRunner

from kassette.credentials import CodexCredentialProvider, PiAuthCredentialProvider
from kassette.diagnostics import LifecycleDiagnostics
from kassette.domain import SessionEvent, SessionEventType, SessionState
from kassette.providers.builtin import build_builtin_provider_registry, build_cascade_stt
from kassette.providers.cascade import (
    CascadedBargeInProcessor,
    CascadedVoiceEvents,
    handle_client_message,
)
from kassette.providers.quicksilver.service import GPTLiveService, TransportFactory
from kassette.providers.quicksilver.transport import QuicksilverTransport
from kassette.providers.runtime import VoiceProviderRuntime
from kassette.sessions import (
    LiveSessionCoordinator,
    SessionHandle,
    SessionNotFoundError,
    SessionRegistry,
)
from kassette.settings import KassetteSettings, load_settings
from kassette.transcript_grooming import (
    TranscriptGroomingProcessor,
    load_transcript_groomer,
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


async def _await_finalizer(finalize: Callable[[], Awaitable[None]]) -> None:
    task = asyncio.create_task(_invoke(finalize))
    caller_cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is None or not current.cancelling():
                raise
            caller_cancelled = True
        except BaseException:
            if not caller_cancelled:
                raise
    if caller_cancelled:
        if not task.cancelled():
            task.exception()
        raise asyncio.CancelledError
    await task


async def _invoke(callback: Callable[[], Awaitable[None]]) -> None:
    await callback()


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

        async def finalize() -> None:
            try:
                await close_session()
            finally:
                await lifecycle.clear(handle)
                try:
                    final_snapshot = await registry.get(session_id)
                except SessionNotFoundError:
                    final_snapshot = None
                if final_snapshot is not None and final_snapshot.state.value in {
                    "closed",
                    "failed",
                }:
                    await registry.reap(session_id, expected_generation=snapshot.generation)

        await _await_finalizer(finalize)


async def run_cascaded_session(
    transport: BaseTransport,
    runner_args: SmallWebRTCRunnerArguments,
    settings: KassetteSettings,
    *,
    registry: SessionRegistry = _registry,
    lifecycle: LiveSessionCoordinator = _lifecycle,
) -> None:
    """Run the selected transcription provider and Fish TTS around ClickClack's loop."""
    transcription_api_key, fish_api_key = settings.cascade_credentials()
    session_id = runner_args.session_id or str(uuid4())
    snapshot = await registry.create(session_id)

    async def collect(event: SessionEvent) -> None:
        if event.type.value == "session.state_changed" and event.state is not None:
            await registry.transition(
                session_id,
                event.state,
                expected_generation=snapshot.generation,
            )
        elif event.type.value == "session.error":
            await registry.transition(
                session_id,
                event.state or SessionState.FAILED,
                error_code=event.error_code,
                expected_generation=snapshot.generation,
            )
        await _log_event(event)

    vad = VADProcessor(
        vad_analyzer=SileroVADAnalyzer(
            params=VADParams(
                stop_secs=settings.vad_stop_secs,
                min_volume=settings.vad_min_volume,
            ),
        )
    )
    stt = build_cascade_stt(settings, transcription_api_key)
    transcript_grooming = TranscriptGroomingProcessor(
        load_transcript_groomer(settings.transcript_grooming_profile),
        timeout_secs=settings.transcript_grooming_timeout_secs,
    )
    tts = FishAudioTTSService(
        api_key=fish_api_key,
        sample_rate=24_000,
        settings=FishAudioTTSService.Settings(
            model=settings.fish_model,
            voice=settings.fish_voice_id,
        ),
    )
    transcript_events = CascadedVoiceEvents(
        session_id=session_id,
        event_sink=collect,
        name="CascadedTranscriptEvents",
        publish_speech=False,
    )
    barge_in = CascadedBargeInProcessor(name="CascadedBargeIn")
    speech_events = CascadedVoiceEvents(
        session_id=session_id,
        event_sink=collect,
        name="CascadedSpeechEvents",
        publish_transcripts=False,
        publish_start_state=False,
    )
    pipeline = Pipeline(
        [
            transport.input(),
            vad,
            stt,
            transcript_grooming,
            transcript_events,
            tts,
            barge_in,
            speech_events,
            transport.output(),
        ]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=False),
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    async def speak(text: str) -> None:
        await barge_in.queue_speech(text, worker.queue_frame)

    async def set_input_paused(paused: bool) -> None:
        if paused:
            await stt.pause_input()
        else:
            await stt.resume_input()
        await transcript_events.publish_input_state(paused=paused)

    @transport.event_handler("on_app_message")
    async def on_app_message(
        _transport: BaseTransport,
        message: Any,
        _sender: str,
    ) -> None:
        await handle_client_message(
            message,
            speak,
            set_input_paused=set_input_paused,
        )

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport: BaseTransport, _client: Any) -> None:
        logger.info("kassette client connected to cascaded voice session {}", session_id)

    async def close_state() -> None:
        try:
            current = await registry.get(session_id)
            if current.state not in {SessionState.CLOSED, SessionState.FAILED}:
                if current.state is not SessionState.CLOSING:
                    await registry.transition(
                        session_id,
                        SessionState.CLOSING,
                        expected_generation=snapshot.generation,
                    )
                await registry.transition(
                    session_id,
                    SessionState.CLOSED,
                    expected_generation=snapshot.generation,
                )
        finally:
            await registry.release_audio(
                session_id,
                expected_generation=snapshot.generation,
            )

    close_session = _SessionCloser(worker.cancel, close_state)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport: BaseTransport, _client: Any) -> None:
        logger.info("kassette client disconnected from cascaded voice session {}", session_id)
        await close_session()

    async def start_and_run() -> None:
        await registry.acquire_audio(
            session_id,
            expected_generation=snapshot.generation,
        )
        connecting = await registry.transition(
            session_id,
            SessionState.CONNECTING,
            expected_generation=snapshot.generation,
        )
        await _log_event(
            SessionEvent(
                session_id=session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=connecting.state,
            )
        )
        await runner.run()

    handle = SessionHandle(session_id, snapshot.generation)
    try:
        previous_closed = await lifecycle.replace(handle=handle, close=close_session)
        if not previous_closed:
            logger.warning("kassette previous voice session cleanup failed during replacement")
        started = await lifecycle.run_active(handle, start_and_run)
        if not started:
            logger.warning("kassette superseded cascaded voice session did not start")
    finally:

        async def finalize() -> None:
            try:
                await close_session()
            finally:
                await lifecycle.clear(handle)
                try:
                    final_snapshot = await registry.get(session_id)
                except SessionNotFoundError:
                    final_snapshot = None
                if final_snapshot is not None and final_snapshot.state.value in {
                    "closed",
                    "failed",
                }:
                    await registry.reap(session_id, expected_generation=snapshot.generation)

        await _await_finalizer(finalize)


async def run_switchable_session(
    transport: BaseTransport,
    runner_args: SmallWebRTCRunnerArguments,
    settings: KassetteSettings,
    *,
    registry: SessionRegistry = _registry,
    lifecycle: LiveSessionCoordinator = _lifecycle,
    credential_provider: CodexCredentialProvider | None = None,
    provider_transport_factory: TransportFactory = QuicksilverTransport,
) -> None:
    """Run one stable WebRTC session around a replaceable provider adapter."""
    session_id = runner_args.session_id or str(uuid4())
    snapshot = await registry.create(
        session_id,
        initial_provider_id=settings.voice_backend,
    )
    providers = build_builtin_provider_registry(
        settings,
        session_registry=registry,
        credential_provider=credential_provider,
        quicksilver_transport_factory=provider_transport_factory,
        quicksilver_client_delegation=True,
        quicksilver_publish_client_events=True,
    )
    runtime = VoiceProviderRuntime(
        session_id=session_id,
        session_generation=snapshot.generation,
        initial_provider_id=settings.voice_backend,
        registry=registry,
        providers=providers,
        event_sink=_log_event,
        name="VoiceProviderRuntime",
    )
    pipeline = Pipeline([transport.input(), runtime, transport.output()])
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=False),
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    @transport.event_handler("on_app_message")
    async def on_app_message(
        _transport: BaseTransport,
        message: Any,
        _sender: str,
    ) -> None:
        await runtime.handle_client_message(message)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(_transport: BaseTransport, _client: Any) -> None:
        logger.info("kassette client connected to switchable voice session {}", session_id)

    async def close_state() -> None:
        try:
            current = await registry.get(session_id)
            if current.state not in {SessionState.CLOSED, SessionState.FAILED}:
                if current.state is not SessionState.CLOSING:
                    await registry.transition(
                        session_id,
                        SessionState.CLOSING,
                        expected_generation=snapshot.generation,
                    )
                await registry.transition(
                    session_id,
                    SessionState.CLOSED,
                    expected_generation=snapshot.generation,
                )
        finally:
            await registry.release_audio(
                session_id,
                expected_generation=snapshot.generation,
            )

    close_session = _SessionCloser(worker.cancel, close_state)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(_transport: BaseTransport, _client: Any) -> None:
        logger.info("kassette client disconnected from switchable voice session {}", session_id)
        await close_session()

    async def start_and_run() -> None:
        await registry.acquire_audio(
            session_id,
            expected_generation=snapshot.generation,
        )
        connecting = await registry.transition(
            session_id,
            SessionState.CONNECTING,
            expected_generation=snapshot.generation,
            expected_provider_generation=snapshot.provider_generation,
        )
        await _log_event(
            SessionEvent(
                session_id=session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=connecting.state,
                provider_type=connecting.active_provider,
                metadata={"provider_generation": connecting.provider_generation},
            )
        )
        await runner.run()

    handle = SessionHandle(session_id, snapshot.generation)
    try:
        previous_closed = await lifecycle.replace(handle=handle, close=close_session)
        if not previous_closed:
            logger.warning("kassette previous voice session cleanup failed during replacement")
        started = await lifecycle.run_active(handle, start_and_run)
        if not started:
            logger.warning("kassette superseded switchable voice session did not start")
    finally:

        async def finalize() -> None:
            try:
                await close_session()
            finally:
                await lifecycle.clear(handle)
                try:
                    final_snapshot = await registry.get(session_id)
                except SessionNotFoundError:
                    final_snapshot = None
                if final_snapshot is not None and final_snapshot.state in {
                    SessionState.CLOSED,
                    SessionState.FAILED,
                }:
                    await registry.reap(
                        session_id,
                        expected_generation=snapshot.generation,
                    )

        await _await_finalizer(finalize)


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
    settings = load_settings()
    await run_switchable_session(transport, runner_args, settings)


if __name__ == "__main__":
    from pipecat.runner.run import app, main

    from kassette.terminal_api import TerminalSessionManager, create_terminal_router
    from kassette.terminal_runtime import run_terminal_voice_session
    from kassette.tts_api import install_tts_route

    async def run_terminal(session: Any) -> None:
        await run_terminal_voice_session(
            session,
            load_settings(),
            registry=_registry,
            lifecycle=_lifecycle,
        )

    install_tts_route(app)
    app.include_router(create_terminal_router(TerminalSessionManager(run_terminal)))
    main()
