"""Run one cascaded voice session on the service's local audio devices."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol, cast

from pipecat.frames.frames import InterruptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection
from pipecat.transports.local.audio import LocalAudioTransportParams
from pipecat.workers.runner import WorkerRunner

from kassette.domain import SessionEvent, SessionEventType, SessionState, VoiceSessionSnapshot
from kassette.providers.builtin import build_builtin_provider_registry
from kassette.providers.runtime import VoiceProviderRuntime
from kassette.sessions import (
    LiveSessionCoordinator,
    SessionHandle,
    SessionNotFoundError,
    SessionRegistry,
)
from kassette.settings import KassetteSettings
from kassette.terminal_api import TerminalSession
from kassette.terminal_audio import (
    StableLocalAudioTransport,
    TerminalInputProcessor,
    TerminalOutputProcessor,
)
from kassette.terminal_protocol import envelope


class ClientMessageRuntime(Protocol):
    async def handle_client_message(self, message: object) -> bool: ...


class CancellableWorker(Protocol):
    async def cancel(self) -> None: ...


async def close_terminal_session(
    worker: CancellableWorker,
    lease: TerminalAudioLease,
) -> None:
    """Stop the pipeline and release its audio lease before a replacement starts."""
    try:
        await worker.cancel()
    finally:
        await lease.close()


class TerminalInputControl:
    """Retain the latest desired mic state until the provider adapter can acknowledge it."""

    def __init__(self) -> None:
        self.desired_paused: bool | None = None
        self.applied_paused: bool | None = None
        self._lock = asyncio.Lock()

    async def request(
        self,
        paused: bool,
        runtime: ClientMessageRuntime,
        set_telemetry_paused: Callable[[bool], Awaitable[None]],
    ) -> bool:
        self.desired_paused = paused
        async with self._lock:
            return await self._apply(paused, runtime, set_telemetry_paused)

    async def adapter_ready(
        self,
        runtime: ClientMessageRuntime,
        set_telemetry_paused: Callable[[bool], Awaitable[None]],
    ) -> bool:
        async with self._lock:
            desired = self.desired_paused
            if desired is None:
                return False
            if self.applied_paused == desired:
                return True
            return await self._apply(desired, runtime, set_telemetry_paused)

    async def _apply(
        self,
        paused: bool,
        runtime: ClientMessageRuntime,
        set_telemetry_paused: Callable[[bool], Awaitable[None]],
    ) -> bool:
        applied = await runtime.handle_client_message(
            envelope("input.pause" if paused else "input.resume", {})
        )
        if applied:
            self.applied_paused = paused
            await set_telemetry_paused(paused)
        return applied


class TerminalAudioLease:
    """Own the terminal session's audio lease and close it idempotently."""

    def __init__(self, registry: SessionRegistry, snapshot: VoiceSessionSnapshot) -> None:
        self._registry = registry
        self._snapshot = snapshot
        self._closed = False

    async def open(self) -> None:
        await self._registry.acquire_audio(
            self._snapshot.id,
            expected_generation=self._snapshot.generation,
        )
        await self._registry.transition(
            self._snapshot.id,
            SessionState.CONNECTING,
            expected_generation=self._snapshot.generation,
            expected_provider_generation=self._snapshot.provider_generation,
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            current = await self._registry.get(self._snapshot.id)
        except SessionNotFoundError:
            return
        if current.state not in {SessionState.CLOSED, SessionState.FAILED}:
            if current.state is not SessionState.CLOSING:
                await self._registry.transition(
                    self._snapshot.id,
                    SessionState.CLOSING,
                    expected_generation=self._snapshot.generation,
                )
            await self._registry.transition(
                self._snapshot.id,
                SessionState.CLOSED,
                expected_generation=self._snapshot.generation,
            )
        await self._registry.release_audio(
            self._snapshot.id,
            expected_generation=self._snapshot.generation,
        )
        await self._registry.reap(
            self._snapshot.id,
            expected_generation=self._snapshot.generation,
        )


def session_event_envelope(event: SessionEvent) -> dict[str, object] | None:
    """Map runtime events needed by terminal clients onto the shared envelope."""
    if event.type not in {
        SessionEventType.SESSION_STATE_CHANGED,
        SessionEventType.SPEECH_STARTED,
        SessionEventType.SPEECH_STOPPED,
        SessionEventType.INTERRUPTED,
        SessionEventType.ERROR,
    }:
        return None
    data: dict[str, object] = dict(event.metadata)
    data["session_id"] = event.session_id
    if event.state is not None:
        data["state"] = event.state.value
    if event.provider_type is not None:
        data["provider_type"] = event.provider_type
    if event.error_code is not None:
        data["error_code"] = event.error_code
    return envelope(event.type.value, data)


async def run_terminal_voice_session(
    session: TerminalSession,
    settings: KassetteSettings,
    *,
    registry: SessionRegistry,
    lifecycle: LiveSessionCoordinator,
) -> None:
    """Attach a negotiated terminal control channel to one local audio pipeline."""
    if settings.voice_backend != "cascade":
        await session.send(
            envelope("session.error", {"message": "Pi terminal voice requires cascade"})
        )
        return

    snapshot = await registry.create(session.session_id, initial_provider_id="cascade")
    lease = TerminalAudioLease(registry, snapshot)
    transport = StableLocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=True,
            audio_in_sample_rate=16_000,
            audio_out_sample_rate=24_000,
            input_device_index=settings.input_device_index,
            output_device_index=settings.output_device_index,
        ),
        input_name=settings.input_device_name,
        output_name=settings.output_device_name,
    )
    input_processor = TerminalInputProcessor(session.send)
    output_processor = TerminalOutputProcessor(session.send)
    input_control = TerminalInputControl()
    pause_tasks: set[asyncio.Task[bool]] = set()
    providers = build_builtin_provider_registry(settings, session_registry=registry)

    def finish_pause_task(task: asyncio.Task[bool]) -> None:
        pause_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            session.closed.set()

    async def event_sink(event: SessionEvent) -> None:
        message = session_event_envelope(event)
        if message is not None:
            await session.send(message)
        if (
            event.type is SessionEventType.SESSION_STATE_CHANGED
            and event.state is SessionState.LISTENING
        ):
            task = asyncio.create_task(
                input_control.adapter_ready(runtime, input_processor.set_paused)
            )
            pause_tasks.add(task)
            task.add_done_callback(finish_pause_task)

    runtime = VoiceProviderRuntime(
        session_id=session.session_id,
        session_generation=snapshot.generation,
        initial_provider_id="cascade",
        registry=registry,
        providers=providers,
        event_sink=event_sink,
        name="TerminalVoiceProviderRuntime",
    )
    pipeline = Pipeline(
        [transport.input(), input_processor, runtime, output_processor, transport.output()]
    )
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=False),
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)

    async def close() -> None:
        await close_terminal_session(worker, lease)

    handle = SessionHandle(session.session_id, snapshot.generation)
    previous_closed = await lifecycle.replace(handle=handle, close=close)
    if not previous_closed:
        await session.send(
            envelope("session.error", {"message": "previous session cleanup failed"})
        )

    async def pump_messages() -> None:
        while not session.closed.is_set():
            message = await session.receive()
            message_type = cast(str, message["type"])
            data = cast(dict[str, object], message["data"])
            if message_type in {"input.pause", "input.resume"}:
                await input_control.request(
                    message_type == "input.pause",
                    runtime,
                    input_processor.set_paused,
                )
            elif message_type == "output.mute":
                output_processor.muted = bool(data.get("muted", True))
                await session.send(
                    envelope("output.state_changed", {"muted": output_processor.muted})
                )
            elif message_type == "output.cancel":
                await runtime.process_frame(InterruptionFrame(), FrameDirection.DOWNSTREAM)
            else:
                await runtime.handle_client_message(message)

    async def run() -> None:
        await lease.open()
        pipeline_task = asyncio.create_task(runner.run())
        pump_task = asyncio.create_task(pump_messages())
        closed_task = asyncio.create_task(session.closed.wait())
        try:
            done, _pending = await asyncio.wait(
                {closed_task, pipeline_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if pipeline_task in done and not pipeline_task.cancelled():
                failure = pipeline_task.exception()
                if failure is not None:
                    await session.send(
                        envelope(
                            "session.error",
                            {"message": f"local audio failed: {type(failure).__name__}"},
                        )
                    )
        finally:
            await worker.cancel()
            for task in tuple(pause_tasks):
                task.cancel()
            closed_task.cancel()
            pump_task.cancel()
            with suppress(asyncio.CancelledError):
                await closed_task
            with suppress(asyncio.CancelledError):
                await pump_task
            with suppress(asyncio.CancelledError):
                await pipeline_task

    try:
        await lifecycle.run_active(handle, run)
    finally:
        await lifecycle.clear(handle)
        await lease.close()
