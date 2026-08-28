"""Runtime provider registry and generation-fenced hot swapping."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, cast

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    InterruptionFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.pipeline.pipeline import Pipeline, PipelineSink, PipelineSource
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessor,
    FrameProcessorSetup,
)

from kassette.domain import (
    TERMINAL_SESSION_STATES,
    EventSink,
    SessionEvent,
    SessionEventType,
    SessionState,
    TranscriptRole,
    VoiceSessionSnapshot,
)
from kassette.sessions import (
    InvalidSessionTransitionError,
    SessionGenerationMismatchError,
    SessionRegistry,
    SessionRegistryError,
)

_MAX_PROVIDER_ID_CHARS = 64
_DEFAULT_PROVIDER_READY_TIMEOUT_SECS = 15.0


class VoiceProviderMode(StrEnum):
    """Provider topology hidden behind the runtime seam."""

    NATIVE = "native"
    CASCADED = "cascaded"


class CredentialReadiness(StrEnum):
    """Whether a provider can be started without exposing its credentials."""

    READY = "ready"
    MISSING = "missing"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class VoiceProviderCapabilities:
    """Bounded provider facts safe to expose to a localhost client."""

    provider_id: str
    mode: VoiceProviderMode
    credential_readiness: CredentialReadiness
    supports_transcripts: bool = True
    supports_interruptions: bool = True
    supports_speech_output: bool = True
    supports_input_pause: bool = False
    supports_live_switch: bool = True

    def __post_init__(self) -> None:
        if (
            not self.provider_id
            or len(self.provider_id) > _MAX_PROVIDER_ID_CHARS
            or not self.provider_id.isprintable()
        ):
            raise ValueError("invalid voice provider identifier")

    def client_data(self) -> dict[str, str | bool]:
        """Return the non-secret capability summary sent to clients."""
        return {
            "provider_id": self.provider_id,
            "mode": self.mode.value,
            "credential_readiness": self.credential_readiness.value,
            "supports_transcripts": self.supports_transcripts,
            "supports_interruptions": self.supports_interruptions,
            "supports_speech_output": self.supports_speech_output,
            "supports_input_pause": self.supports_input_pause,
            "supports_live_switch": self.supports_live_switch,
        }


FrameSink = Callable[[Frame, FrameDirection], Coroutine[Any, Any, None]]
ProviderMessageHandler = Callable[[Any], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class VoiceProviderBuildContext:
    """Stable session context supplied to one disposable provider adapter."""

    session_id: str
    session_generation: int
    provider_generation: int
    event_sink: EventSink
    frame_sink: FrameSink


class VoiceProviderAdapter(Protocol):
    """Small interface implemented by native and cascaded provider adapters."""

    async def setup(self, setup: FrameProcessorSetup | None) -> None: ...

    async def start(self, frame: StartFrame) -> None: ...

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None: ...

    async def interrupt(self) -> None: ...

    async def handle_client_message(self, message: Any) -> bool: ...

    async def close(self) -> None: ...


VoiceProviderFactory = Callable[[VoiceProviderBuildContext], VoiceProviderAdapter]


@dataclass(frozen=True, slots=True)
class VoiceProviderDefinition:
    capabilities: VoiceProviderCapabilities
    factory: VoiceProviderFactory


class VoiceProviderRegistry:
    """Resolve stable provider IDs to capabilities and adapter factories."""

    def __init__(self, definitions: Sequence[VoiceProviderDefinition]) -> None:
        self._definitions: dict[str, VoiceProviderDefinition] = {}
        for definition in definitions:
            provider_id = definition.capabilities.provider_id
            if provider_id in self._definitions:
                raise ValueError(f"duplicate voice provider: {provider_id}")
            self._definitions[provider_id] = definition
        if not self._definitions:
            raise ValueError("at least one voice provider is required")

    def resolve(self, provider_id: str) -> VoiceProviderDefinition | None:
        return self._definitions.get(provider_id)

    def available(self) -> tuple[VoiceProviderCapabilities, ...]:
        return tuple(
            definition.capabilities
            for _provider_id, definition in sorted(self._definitions.items())
        )


class ProviderSwitchStatus(StrEnum):
    ACTIVE = "active"
    UNCHANGED = "unchanged"
    REFUSED = "refused"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ProviderSwitchResult:
    status: ProviderSwitchStatus
    active_provider: str | None
    provider_generation: int
    reason: str | None = None


class PipelineProviderAdapter:
    """Adapt a Pipecat processor chain to the runtime provider interface."""

    def __init__(
        self,
        processors: Sequence[FrameProcessor],
        *,
        frame_sink: FrameSink,
        message_handler: ProviderMessageHandler | None = None,
    ) -> None:
        self._pipeline = Pipeline(
            processors,
            source=PipelineSource(frame_sink),
            sink=PipelineSink(frame_sink),
        )
        self._message_handler = message_handler
        self._close_lock = asyncio.Lock()
        self._closed = False

    @property
    def pipeline(self) -> Pipeline:
        """Return the nested pipeline for provider-owned control frame injection."""
        return self._pipeline

    async def setup(self, setup: FrameProcessorSetup | None) -> None:
        if setup is not None:
            await self._pipeline.setup(setup)

    async def start(self, frame: StartFrame) -> None:
        await self._pipeline.process_frame(frame, FrameDirection.DOWNSTREAM)

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if not self._closed:
            await self._pipeline.process_frame(frame, direction)

    async def interrupt(self) -> None:
        if not self._closed:
            await self._pipeline.process_frame(
                InterruptionFrame(),
                FrameDirection.DOWNSTREAM,
            )

    async def handle_client_message(self, message: Any) -> bool:
        if self._closed or self._message_handler is None:
            return False
        return await self._message_handler(message)

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            try:
                await self._pipeline.process_frame(
                    CancelFrame(),
                    FrameDirection.DOWNSTREAM,
                )
            finally:
                await self._pipeline.cleanup()


async def _discard_event(_: SessionEvent) -> None:
    return


class VoiceProviderRuntime(FrameProcessor):
    """Keep one transport stable while replacing its disposable provider adapter."""

    def __init__(
        self,
        *,
        session_id: str,
        session_generation: int,
        initial_provider_id: str,
        registry: SessionRegistry,
        providers: VoiceProviderRegistry,
        event_sink: EventSink | None = None,
        provider_ready_timeout_secs: float = _DEFAULT_PROVIDER_READY_TIMEOUT_SECS,
        name: str | None = None,
    ) -> None:
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            name=name,
            enable_direct_mode=True,
        )
        if providers.resolve(initial_provider_id) is None:
            raise ValueError(f"unknown initial voice provider: {initial_provider_id}")
        if provider_ready_timeout_secs <= 0:
            raise ValueError("provider_ready_timeout_secs must be positive")
        self._session_id = session_id
        self._session_generation = session_generation
        self._provider_id = initial_provider_id
        self._provider_generation = 1
        self._registry = registry
        self._providers = providers
        self._event_sink = event_sink or _discard_event
        self._provider_ready_timeout_secs = provider_ready_timeout_secs
        self._adapter: VoiceProviderAdapter | None = None
        self._provider_setup: FrameProcessorSetup | None = None
        self._start_frame: StartFrame | None = None
        self._switch_lock = asyncio.Lock()
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._sequence = 0
        self._provisional_user_transcript = False
        self._ready_generation: int | None = None
        self._ready_event: asyncio.Event | None = None
        self._ready_error: str | None = None

    async def setup(self, setup: FrameProcessorSetup) -> None:
        await super().setup(setup)
        self._provider_setup = setup

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)

        if isinstance(frame, StartFrame) and direction is FrameDirection.DOWNSTREAM:
            self._start_frame = frame
            await self.push_frame(frame, direction)
            async with self._switch_lock:
                await self._start_initial_provider(frame)
            return

        if isinstance(frame, (EndFrame, CancelFrame)):
            await self._close_active_adapter()
            await self.push_frame(frame, direction)
            return

        async with self._switch_lock:
            adapter = self._adapter
            if adapter is None:
                await self.push_frame(frame, direction)
                return
            await adapter.process_frame(frame, direction)

    async def cleanup(self) -> None:
        await self._close_active_adapter()
        await super().cleanup()

    async def handle_client_message(self, message: Any) -> bool:
        """Route one bounded control message to the runtime or active adapter."""
        if not isinstance(message, dict):
            return False
        record = cast(dict[str, object], message)
        if record.get("label") != "kassette":
            return False
        message_type = record.get("type")
        data = record.get("data")
        if not isinstance(data, dict):
            return False
        data_record = cast(dict[str, object], data)
        if message_type == "provider.list":
            await self._emit_provider_catalog()
            return True
        if message_type == "provider.switch":
            provider_id = data_record.get("provider_id")
            force = data_record.get("force", False)
            expected_generation = data_record.get("expected_provider_generation")
            if (
                not isinstance(provider_id, str)
                or not provider_id
                or len(provider_id) > _MAX_PROVIDER_ID_CHARS
                or not provider_id.isprintable()
                or not isinstance(force, bool)
                or (
                    expected_generation is not None
                    and (
                        not isinstance(expected_generation, int)
                        or isinstance(expected_generation, bool)
                        or expected_generation < 0
                    )
                )
            ):
                return False
            await self.switch_provider(
                provider_id,
                force=force,
                expected_provider_generation=expected_generation,
            )
            return True

        async with self._switch_lock:
            adapter = self._adapter
            if adapter is None:
                return False
            return await adapter.handle_client_message(message)

    async def switch_provider(
        self,
        provider_id: str,
        *,
        force: bool = False,
        expected_provider_generation: int | None = None,
    ) -> ProviderSwitchResult:
        """Replace the active adapter while preserving the session and audio lease."""
        async with self._switch_lock:
            snapshot = await self._registry.get(self._session_id)
            await self._emit_provider_event(
                SessionEventType.PROVIDER_SWITCH_REQUESTED,
                provider_id,
                state=snapshot.state,
                metadata={"force": force},
            )
            definition = self._providers.resolve(provider_id)
            if definition is None:
                return await self._refuse(provider_id, snapshot, "provider_unknown")
            if not definition.capabilities.supports_live_switch:
                return await self._refuse(provider_id, snapshot, "live_switch_unsupported")
            if definition.capabilities.credential_readiness is CredentialReadiness.MISSING:
                return await self._refuse(provider_id, snapshot, "credentials_unavailable")
            if (
                expected_provider_generation is not None
                and snapshot.provider_generation != expected_provider_generation
            ):
                return await self._refuse(provider_id, snapshot, "stale_provider_generation")
            if provider_id == snapshot.active_provider:
                await self._emit_active(snapshot, definition.capabilities)
                return ProviderSwitchResult(
                    ProviderSwitchStatus.UNCHANGED,
                    snapshot.active_provider,
                    snapshot.provider_generation,
                )
            if self._start_frame is None or self._adapter is None or self._closed:
                return await self._refuse(provider_id, snapshot, "session_not_active")
            interrupted_speech = False
            discarded_provisional_transcript = False
            if snapshot.state is SessionState.SPEAKING:
                if not force:
                    return await self._refuse(provider_id, snapshot, "provider_busy")
                try:
                    await self._adapter.interrupt()
                except BaseException:
                    return await self._refuse(provider_id, snapshot, "interruption_failed")
                interrupted_speech = True
                snapshot = await self._registry.get(self._session_id)
            if self._provisional_user_transcript:
                if not force:
                    return await self._refuse(
                        provider_id,
                        snapshot,
                        "provisional_transcript_active",
                    )
                self._provisional_user_transcript = False
                discarded_provisional_transcript = True
            if snapshot.state is not SessionState.LISTENING:
                return await self._refuse(provider_id, snapshot, "provider_busy")

            previous_provider = snapshot.active_provider
            switch_snapshot = await self._registry.begin_provider_switch(
                self._session_id,
                provider_id,
                expected_generation=self._session_generation,
                expected_provider_generation=snapshot.provider_generation,
            )
            self._provider_generation = switch_snapshot.provider_generation
            await self._publish_state(switch_snapshot.state)
            await self._emit_provider_event(
                SessionEventType.PROVIDER_SWITCHING,
                provider_id,
                state=switch_snapshot.state,
                metadata={
                    "interrupted_speech": interrupted_speech,
                    "discarded_provisional_transcript": discarded_provisional_transcript,
                },
            )

            previous_adapter = self._adapter
            self._adapter = None
            candidate: VoiceProviderAdapter | None = None
            failed_adapter = previous_adapter
            failure_reason = "provider_stop_failed"
            try:
                await previous_adapter.close()
                failure_reason = "provider_start_failed"
                candidate = self._build_adapter(provider_id, switch_snapshot.provider_generation)
                failed_adapter = candidate
                await candidate.setup(self._provider_setup)
                active_snapshot = await self._registry.activate_provider(
                    self._session_id,
                    provider_id,
                    expected_generation=self._session_generation,
                    expected_provider_generation=switch_snapshot.provider_generation,
                )
                self._adapter = candidate
                self._provider_id = provider_id
                await self._publish_state(active_snapshot.state)
                await self._start_adapter(
                    candidate,
                    self._start_frame,
                    switch_snapshot.provider_generation,
                )
            except asyncio.CancelledError:
                cleanup = asyncio.create_task(
                    self._recover_switch(
                        failed_adapter,
                        previous_provider,
                        provider_id,
                        "switch_cancelled",
                    )
                )
                await asyncio.shield(cleanup)
                raise
            except BaseException:
                return await self._recover_switch(
                    failed_adapter,
                    previous_provider,
                    provider_id,
                    failure_reason,
                )

            active_snapshot = await self._registry.get(self._session_id)
            await self._emit_active(active_snapshot, definition.capabilities)
            return ProviderSwitchResult(
                ProviderSwitchStatus.ACTIVE,
                active_snapshot.active_provider,
                active_snapshot.provider_generation,
            )

    async def _start_initial_provider(self, frame: StartFrame) -> None:
        definition = self._providers.resolve(self._provider_id)
        if definition is None:
            raise RuntimeError("initial voice provider disappeared from registry")
        if definition.capabilities.credential_readiness is CredentialReadiness.MISSING:
            await self._fail_session("credentials_unavailable")
            raise RuntimeError("initial voice provider credentials are unavailable")
        try:
            adapter = self._build_adapter(self._provider_id, self._provider_generation)
            await adapter.setup(self._provider_setup)
            self._adapter = adapter
            await self._start_adapter(adapter, frame, self._provider_generation)
        except BaseException:
            if self._adapter is not None:
                await self._adapter.close()
                self._adapter = None
            await self._fail_session("initial_provider_start_failed")
            raise
        snapshot = await self._registry.get(self._session_id)
        await self._emit_active(snapshot, definition.capabilities)
        await self._emit_provider_catalog()

    async def _start_adapter(
        self,
        adapter: VoiceProviderAdapter,
        frame: StartFrame,
        provider_generation: int,
    ) -> None:
        ready_event = asyncio.Event()
        self._ready_generation = provider_generation
        self._ready_event = ready_event
        self._ready_error = None

        async def start_and_wait() -> None:
            await adapter.start(frame)
            await ready_event.wait()

        try:
            await asyncio.wait_for(
                start_and_wait(),
                timeout=self._provider_ready_timeout_secs,
            )
            if self._ready_error is not None:
                raise RuntimeError("voice provider failed before becoming ready")
        finally:
            if self._ready_generation == provider_generation:
                self._ready_generation = None
                self._ready_event = None
                self._ready_error = None

    def _build_adapter(self, provider_id: str, provider_generation: int) -> VoiceProviderAdapter:
        definition = self._providers.resolve(provider_id)
        if definition is None:
            raise RuntimeError("voice provider disappeared from registry")

        async def collect(event: SessionEvent) -> None:
            await self._collect_provider_event(event, provider_id, provider_generation)

        async def forward(frame: Frame, direction: FrameDirection) -> None:
            await self._forward_provider_frame(
                frame,
                direction,
                provider_generation,
            )

        return definition.factory(
            VoiceProviderBuildContext(
                session_id=self._session_id,
                session_generation=self._session_generation,
                provider_generation=provider_generation,
                event_sink=collect,
                frame_sink=forward,
            )
        )

    async def _collect_provider_event(
        self,
        event: SessionEvent,
        provider_id: str,
        provider_generation: int,
    ) -> None:
        startup_error = (
            event.type is SessionEventType.ERROR and self._ready_generation == provider_generation
        )
        try:
            snapshot = await self._registry.get(self._session_id)
            if (
                snapshot.provider_generation != provider_generation
                or snapshot.state in TERMINAL_SESSION_STATES
                or snapshot.state is SessionState.CLOSING
            ):
                return
            if (
                event.type is SessionEventType.TRANSCRIPT_DELTA
                and event.role is TranscriptRole.USER
                and event.text
            ):
                self._provisional_user_transcript = True
            elif (
                event.type is SessionEventType.TRANSCRIPT_FINAL
                and event.role is TranscriptRole.USER
            ):
                self._provisional_user_transcript = False
            provider_session_id = event.metadata.get("provider_session_id")
            if event.type is SessionEventType.SESSION_STATE_CHANGED and event.state is not None:
                snapshot = await self._registry.transition(
                    self._session_id,
                    event.state,
                    provider_session_id=(
                        provider_session_id if isinstance(provider_session_id, str) else None
                    ),
                    expected_generation=self._session_generation,
                    expected_provider_generation=provider_generation,
                )
                event = replace(event, state=snapshot.state)
            elif event.type is SessionEventType.ERROR:
                if self._ready_generation != provider_generation:
                    snapshot = await self._registry.transition(
                        self._session_id,
                        SessionState.FAILED,
                        error_code=event.error_code,
                        expected_generation=self._session_generation,
                        expected_provider_generation=provider_generation,
                    )
                    event = replace(event, state=snapshot.state)
        except (SessionGenerationMismatchError, InvalidSessionTransitionError):
            return
        if self._ready_generation == provider_generation and self._ready_event is not None:
            if (
                event.type is SessionEventType.SESSION_STATE_CHANGED
                and event.state is SessionState.LISTENING
            ):
                self._ready_event.set()
            elif event.type is SessionEventType.ERROR:
                self._ready_error = event.error_code or "provider_error"
                self._ready_event.set()
        if startup_error:
            return
        metadata = {**event.metadata, "provider_generation": provider_generation}
        await self._event_sink(
            replace(
                event,
                provider_type=provider_id,
                metadata=metadata,
            )
        )

    async def _recover_switch(
        self,
        failed_adapter: VoiceProviderAdapter,
        previous_provider: str | None,
        failed_provider: str,
        reason: str,
    ) -> ProviderSwitchResult:
        try:
            await failed_adapter.close()
        except BaseException:
            self._adapter = None
            snapshot = await self._registry.get(self._session_id)
            await self._emit_provider_event(
                SessionEventType.PROVIDER_SWITCH_FAILED,
                failed_provider,
                state=snapshot.state,
                metadata={"reason": "provider_cleanup_failed"},
                error_code="provider_cleanup_failed",
            )
            await self._fail_session("provider_cleanup_failed")
            snapshot = await self._registry.get(self._session_id)
            return ProviderSwitchResult(
                ProviderSwitchStatus.FAILED,
                snapshot.active_provider,
                snapshot.provider_generation,
                "provider_cleanup_failed",
            )
        self._adapter = None
        return await self._rollback(previous_provider, failed_provider, reason)

    async def _rollback(
        self,
        previous_provider: str | None,
        failed_provider: str,
        reason: str,
    ) -> ProviderSwitchResult:
        failed_snapshot = await self._registry.get(self._session_id)
        await self._emit_provider_event(
            SessionEventType.PROVIDER_SWITCH_FAILED,
            failed_provider,
            state=failed_snapshot.state,
            metadata={"reason": reason},
            error_code=reason,
        )
        if previous_provider is None:
            await self._fail_session("provider_switch_failed")
            snapshot = await self._registry.get(self._session_id)
            return ProviderSwitchResult(
                ProviderSwitchStatus.FAILED,
                snapshot.active_provider,
                snapshot.provider_generation,
                reason,
            )

        recovery = await self._registry.restart_provider_switch(
            self._session_id,
            previous_provider,
            expected_generation=self._session_generation,
            expected_provider_generation=failed_snapshot.provider_generation,
        )
        self._provider_generation = recovery.provider_generation
        await self._publish_state(recovery.state)
        try:
            adapter = self._build_adapter(previous_provider, recovery.provider_generation)
            await adapter.setup(self._provider_setup)
            active_snapshot = await self._registry.activate_provider(
                self._session_id,
                previous_provider,
                expected_generation=self._session_generation,
                expected_provider_generation=recovery.provider_generation,
            )
            self._adapter = adapter
            self._provider_id = previous_provider
            await self._publish_state(active_snapshot.state)
            if self._start_frame is None:
                raise RuntimeError("voice session start frame is unavailable")
            await self._start_adapter(
                adapter,
                self._start_frame,
                recovery.provider_generation,
            )
        except BaseException:
            if self._adapter is not None:
                await self._adapter.close()
                self._adapter = None
            await self._fail_session("provider_rollback_failed")
            snapshot = await self._registry.get(self._session_id)
            return ProviderSwitchResult(
                ProviderSwitchStatus.FAILED,
                snapshot.active_provider,
                snapshot.provider_generation,
                "provider_rollback_failed",
            )

        snapshot = await self._registry.get(self._session_id)
        definition = self._providers.resolve(previous_provider)
        if definition is None:
            raise RuntimeError("rollback provider disappeared from registry")
        await self._emit_provider_event(
            SessionEventType.PROVIDER_FALLBACK_ACTIVE,
            previous_provider,
            state=snapshot.state,
            metadata={"failed_provider": failed_provider},
            capabilities=definition.capabilities,
        )
        return ProviderSwitchResult(
            ProviderSwitchStatus.ROLLED_BACK,
            snapshot.active_provider,
            snapshot.provider_generation,
            reason,
        )

    async def _refuse(
        self,
        provider_id: str,
        snapshot: VoiceSessionSnapshot,
        reason: str,
    ) -> ProviderSwitchResult:
        await self._emit_provider_event(
            SessionEventType.PROVIDER_SWITCH_REFUSED,
            provider_id,
            state=snapshot.state,
            metadata={"reason": reason},
        )
        return ProviderSwitchResult(
            ProviderSwitchStatus.REFUSED,
            snapshot.active_provider,
            snapshot.provider_generation,
            reason,
        )

    async def _emit_active(
        self,
        snapshot: VoiceSessionSnapshot,
        capabilities: VoiceProviderCapabilities,
    ) -> None:
        await self._emit_provider_event(
            SessionEventType.PROVIDER_ACTIVE,
            capabilities.provider_id,
            state=snapshot.state,
            capabilities=capabilities,
        )

    async def _emit_provider_catalog(self) -> None:
        snapshot = await self._registry.get(self._session_id)
        capabilities = self._providers.available()
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.PROVIDER_AVAILABLE,
                state=snapshot.state,
                provider_type=snapshot.active_provider,
                metadata={
                    "provider_generation": snapshot.provider_generation,
                    "provider_count": len(capabilities),
                },
            )
        )
        self._sequence += 1
        await self.push_frame(
            OutputTransportMessageUrgentFrame(
                message={
                    "label": "kassette",
                    "type": SessionEventType.PROVIDER_AVAILABLE.value,
                    "data": {
                        "session_id": self._session_id,
                        "active_provider": snapshot.active_provider,
                        "desired_provider": snapshot.desired_provider,
                        "provider_generation": snapshot.provider_generation,
                        "providers": [item.client_data() for item in capabilities],
                        "sequence": self._sequence,
                    },
                }
            )
        )

    async def _emit_provider_event(
        self,
        event_type: SessionEventType,
        provider_id: str,
        *,
        state: SessionState | None = None,
        metadata: dict[str, str | int | float | bool | None] | None = None,
        error_code: str | None = None,
        capabilities: VoiceProviderCapabilities | None = None,
    ) -> None:
        snapshot = await self._registry.get(self._session_id)
        safe_metadata = {
            "provider_generation": snapshot.provider_generation,
            **(metadata or {}),
        }
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=event_type,
                state=state,
                provider_type=provider_id,
                error_code=error_code,
                metadata=safe_metadata,
            )
        )
        data: dict[str, Any] = {
            "session_id": self._session_id,
            "provider_id": provider_id,
            "provider_generation": snapshot.provider_generation,
            **(metadata or {}),
        }
        if state is not None:
            data["state"] = state.value
        if capabilities is not None:
            data["capabilities"] = capabilities.client_data()
        self._sequence += 1
        data["sequence"] = self._sequence
        await self.push_frame(
            OutputTransportMessageUrgentFrame(
                message={"label": "kassette", "type": event_type.value, "data": data}
            )
        )

    async def _publish_state(self, state: SessionState) -> None:
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=state,
                provider_type=self._provider_id,
                metadata={"provider_generation": self._provider_generation},
            )
        )

    async def _fail_session(self, error_code: str) -> None:
        try:
            snapshot = await self._registry.get(self._session_id)
            if snapshot.state is not SessionState.FAILED:
                snapshot = await self._registry.transition(
                    self._session_id,
                    SessionState.FAILED,
                    error_code=error_code,
                    expected_generation=self._session_generation,
                    expected_provider_generation=snapshot.provider_generation,
                )
        except SessionRegistryError:
            return
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.ERROR,
                state=SessionState.FAILED,
                provider_type=self._provider_id,
                error_code=error_code,
                metadata={"provider_generation": snapshot.provider_generation},
            )
        )

    async def _forward_provider_frame(
        self,
        frame: Frame,
        direction: FrameDirection,
        provider_generation: int,
    ) -> None:
        if isinstance(frame, (StartFrame, EndFrame, CancelFrame)):
            return
        try:
            snapshot = await self._registry.get(self._session_id)
        except SessionRegistryError:
            return
        if (
            snapshot.provider_generation != provider_generation
            or snapshot.state in TERMINAL_SESSION_STATES
            or snapshot.state is SessionState.CLOSING
        ):
            return
        await self.push_frame(frame, direction)

    async def _close_active_adapter(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            adapter = self._adapter
            self._adapter = None
            if adapter is not None:
                await adapter.close()
