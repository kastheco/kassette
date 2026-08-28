from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import pytest
from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
    OutputTransportMessageUrgentFrame,
    StartFrame,
)
from pipecat.processors.frame_processor import (
    FrameDirection,
    FrameProcessorSetup,
)

from kassette.domain import SessionEvent, SessionEventType, SessionState, TranscriptRole
from kassette.providers.runtime import (
    CredentialReadiness,
    ProviderSwitchStatus,
    VoiceProviderAdapter,
    VoiceProviderBuildContext,
    VoiceProviderCapabilities,
    VoiceProviderDefinition,
    VoiceProviderMode,
    VoiceProviderRegistry,
    VoiceProviderRuntime,
)
from kassette.sessions import SessionRegistry


@dataclass
class FakeProviderPool:
    provider_id: str
    fail_starts: int = 0
    fail_closes: int = 0
    ready_error: bool = False
    instances: list[FakeProviderAdapter] = field(
        default_factory=lambda: list[FakeProviderAdapter]()
    )
    open_count: int = 0
    max_open_count: int = 0
    start_gate: asyncio.Event | None = None
    start_started: asyncio.Event = field(default_factory=asyncio.Event)

    def build(self, context: VoiceProviderBuildContext) -> VoiceProviderAdapter:
        fail_start = self.fail_starts > 0
        if fail_start:
            self.fail_starts -= 1
        adapter = FakeProviderAdapter(
            pool=self,
            context=context,
            fail_start=fail_start,
        )
        self.instances.append(adapter)
        return adapter


@dataclass
class FakeProviderAdapter:
    pool: FakeProviderPool
    context: VoiceProviderBuildContext
    fail_start: bool = False
    trace: list[str] = field(default_factory=lambda: list[str]())
    opened: bool = False
    closed: bool = False

    async def setup(self, setup: FrameProcessorSetup | None) -> None:
        self.trace.append("setup")

    async def start(self, frame: StartFrame) -> None:
        self.trace.append("start")
        self.pool.start_started.set()
        if self.pool.start_gate is not None:
            await self.pool.start_gate.wait()
        if self.fail_start:
            raise RuntimeError("synthetic provider start failure")
        if self.pool.ready_error:
            await self.context.event_sink(
                SessionEvent(
                    session_id=self.context.session_id,
                    type=SessionEventType.ERROR,
                    state=SessionState.FAILED,
                    error_code="synthetic_provider_error",
                )
            )
            return
        self.opened = True
        self.pool.open_count += 1
        self.pool.max_open_count = max(self.pool.max_open_count, self.pool.open_count)
        await self.context.event_sink(
            SessionEvent(
                session_id=self.context.session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=SessionState.LISTENING,
            )
        )

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        self.trace.append(type(frame).__name__)
        await self.context.frame_sink(frame, direction)

    async def interrupt(self) -> None:
        self.trace.append("interrupt")
        await self.context.event_sink(
            SessionEvent(
                session_id=self.context.session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=SessionState.INTERRUPTING,
            )
        )
        await self.context.event_sink(
            SessionEvent(
                session_id=self.context.session_id,
                type=SessionEventType.INTERRUPTED,
                state=SessionState.INTERRUPTING,
            )
        )
        await self.context.event_sink(
            SessionEvent(
                session_id=self.context.session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=SessionState.LISTENING,
            )
        )

    async def handle_client_message(self, message: Any) -> bool:
        self.trace.append("message")
        return message == {"label": "kassette", "type": "synthetic", "data": {}}

    async def close(self) -> None:
        if self.closed:
            return
        if self.pool.fail_closes > 0:
            self.pool.fail_closes -= 1
            self.trace.append("close_failed")
            raise RuntimeError("synthetic provider close failure")
        self.closed = True
        self.trace.append("close")
        if self.opened:
            self.opened = False
            self.pool.open_count -= 1


class RecordingVoiceProviderRuntime(VoiceProviderRuntime):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.pushed_frames: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        self.pushed_frames.append((frame, direction))


@dataclass(frozen=True)
class RuntimeFixture:
    runtime: RecordingVoiceProviderRuntime
    sessions: SessionRegistry
    events: list[SessionEvent]
    cascade: FakeProviderPool
    quicksilver: FakeProviderPool
    session_generation: int


def _definition(
    pool: FakeProviderPool,
    *,
    readiness: CredentialReadiness = CredentialReadiness.READY,
) -> VoiceProviderDefinition:
    mode = VoiceProviderMode.CASCADED if pool.provider_id == "cascade" else VoiceProviderMode.NATIVE
    return VoiceProviderDefinition(
        capabilities=VoiceProviderCapabilities(
            provider_id=pool.provider_id,
            mode=mode,
            credential_readiness=readiness,
            supports_input_pause=mode is VoiceProviderMode.CASCADED,
        ),
        factory=pool.build,
    )


async def _runtime_fixture(
    *,
    quicksilver_readiness: CredentialReadiness = CredentialReadiness.READY,
    quicksilver_fail_starts: int = 0,
    provider_ready_timeout_secs: float = 1.0,
) -> RuntimeFixture:
    sessions = SessionRegistry()
    snapshot = await sessions.create("voice-1", initial_provider_id="cascade")
    await sessions.acquire_audio("voice-1", expected_generation=snapshot.generation)
    await sessions.transition(
        "voice-1",
        SessionState.CONNECTING,
        expected_generation=snapshot.generation,
        expected_provider_generation=snapshot.provider_generation,
    )
    cascade = FakeProviderPool("cascade")
    quicksilver = FakeProviderPool("quicksilver", fail_starts=quicksilver_fail_starts)
    events: list[SessionEvent] = []

    async def collect(event: SessionEvent) -> None:
        events.append(event)

    runtime = RecordingVoiceProviderRuntime(
        session_id="voice-1",
        session_generation=snapshot.generation,
        initial_provider_id="cascade",
        registry=sessions,
        providers=VoiceProviderRegistry(
            [
                _definition(cascade),
                _definition(quicksilver, readiness=quicksilver_readiness),
            ]
        ),
        event_sink=collect,
        provider_ready_timeout_secs=provider_ready_timeout_secs,
    )
    await runtime.process_frame(StartFrame(), FrameDirection.DOWNSTREAM)
    return RuntimeFixture(
        runtime=runtime,
        sessions=sessions,
        events=events,
        cascade=cascade,
        quicksilver=quicksilver,
        session_generation=snapshot.generation,
    )


async def test_switches_between_real_registry_slots_without_releasing_audio() -> None:
    fixture = await _runtime_fixture()

    first = await fixture.runtime.switch_provider("quicksilver")
    second = await fixture.runtime.switch_provider("cascade")

    snapshot = await fixture.sessions.get("voice-1")
    assert first.status is ProviderSwitchStatus.ACTIVE
    assert second.status is ProviderSwitchStatus.ACTIVE
    assert snapshot.generation == fixture.session_generation
    assert snapshot.provider_generation == 3
    assert snapshot.active_provider == "cascade"
    assert snapshot.state is SessionState.LISTENING
    assert await fixture.sessions.audio_owner() == "voice-1"
    assert fixture.cascade.max_open_count == 1
    assert fixture.quicksilver.max_open_count == 1
    assert fixture.cascade.instances[0].trace[-1] == "close"
    assert fixture.quicksilver.instances[0].trace[-1] == "close"


async def test_busy_provider_requires_force_and_interrupts_before_close() -> None:
    fixture = await _runtime_fixture()
    snapshot = await fixture.sessions.get("voice-1")
    await fixture.sessions.transition(
        "voice-1",
        SessionState.SPEAKING,
        expected_generation=snapshot.generation,
        expected_provider_generation=snapshot.provider_generation,
    )

    refused = await fixture.runtime.switch_provider("quicksilver")
    switched = await fixture.runtime.switch_provider("quicksilver", force=True)

    assert refused.status is ProviderSwitchStatus.REFUSED
    assert refused.reason == "provider_busy"
    assert switched.status is ProviderSwitchStatus.ACTIVE
    assert fixture.cascade.instances[0].trace[-2:] == ["interrupt", "close"]
    event_types = [event.type for event in fixture.events]
    assert SessionEventType.INTERRUPTED in event_types
    assert SessionEventType.PROVIDER_ACTIVE in event_types


async def test_startup_error_event_rolls_back_before_provider_becomes_active() -> None:
    fixture = await _runtime_fixture()
    fixture.quicksilver.ready_error = True

    result = await fixture.runtime.switch_provider("quicksilver")

    snapshot = await fixture.sessions.get("voice-1")
    assert result.status is ProviderSwitchStatus.ROLLED_BACK
    assert snapshot.active_provider == "cascade"
    assert snapshot.state is SessionState.LISTENING
    assert fixture.quicksilver.instances[0].closed is True
    assert SessionEventType.ERROR not in {event.type for event in fixture.events}
    assert SessionEventType.PROVIDER_SWITCH_FAILED in {event.type for event in fixture.events}


async def test_failed_replacement_rebuilds_previous_provider_with_new_generation() -> None:
    fixture = await _runtime_fixture(quicksilver_fail_starts=1)

    result = await fixture.runtime.switch_provider("quicksilver")

    snapshot = await fixture.sessions.get("voice-1")
    assert result.status is ProviderSwitchStatus.ROLLED_BACK
    assert result.reason == "provider_start_failed"
    assert snapshot.active_provider == "cascade"
    assert snapshot.provider_generation == 3
    assert snapshot.state is SessionState.LISTENING
    assert len(fixture.cascade.instances) == 2
    assert fixture.quicksilver.instances[0].closed is True
    assert SessionEventType.PROVIDER_SWITCH_FAILED in {event.type for event in fixture.events}
    assert SessionEventType.PROVIDER_FALLBACK_ACTIVE in {event.type for event in fixture.events}


async def test_stale_provider_callbacks_are_dropped_after_switch() -> None:
    fixture = await _runtime_fixture()
    stale_event_sink = fixture.cascade.instances[0].context.event_sink
    stale_frame_sink = fixture.cascade.instances[0].context.frame_sink
    await fixture.runtime.switch_provider("quicksilver")
    event_count = len(fixture.events)
    frame_count = len(fixture.runtime.pushed_frames)

    await stale_event_sink(
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.TRANSCRIPT_FINAL,
            text="stale transcript",
        )
    )

    await stale_frame_sink(
        OutputAudioRawFrame(audio=b"\x00\x00", sample_rate=24_000, num_channels=1),
        FrameDirection.DOWNSTREAM,
    )

    assert len(fixture.events) == event_count
    assert len(fixture.runtime.pushed_frames) == frame_count
    assert (await fixture.sessions.get("voice-1")).active_provider == "quicksilver"


async def test_missing_credentials_are_refused() -> None:
    fixture = await _runtime_fixture(quicksilver_readiness=CredentialReadiness.MISSING)

    result = await fixture.runtime.switch_provider("quicksilver")

    assert result.status is ProviderSwitchStatus.REFUSED
    assert result.reason == "credentials_unavailable"
    assert (await fixture.sessions.get("voice-1")).provider_generation == 1


async def test_stale_switch_request_is_generation_fenced() -> None:
    fixture = await _runtime_fixture()

    result = await fixture.runtime.switch_provider(
        "quicksilver",
        expected_provider_generation=99,
    )

    assert result.status is ProviderSwitchStatus.REFUSED
    assert result.reason == "stale_provider_generation"
    assert (await fixture.sessions.get("voice-1")).provider_generation == 1


async def test_provisional_transcript_requires_force_and_reports_discard() -> None:
    fixture = await _runtime_fixture()
    await fixture.cascade.instances[0].context.event_sink(
        SessionEvent(
            session_id="voice-1",
            type=SessionEventType.TRANSCRIPT_DELTA,
            role=TranscriptRole.USER,
            text="unfinished",
        )
    )

    refused = await fixture.runtime.switch_provider("quicksilver")
    switched = await fixture.runtime.switch_provider("quicksilver", force=True)

    assert refused.status is ProviderSwitchStatus.REFUSED
    assert refused.reason == "provisional_transcript_active"
    assert switched.status is ProviderSwitchStatus.ACTIVE
    switching = next(
        event
        for event in reversed(fixture.events)
        if event.type is SessionEventType.PROVIDER_SWITCHING
    )
    assert switching.metadata["discarded_provisional_transcript"] is True


async def test_uncloseable_provider_fails_closed_without_starting_replacement() -> None:
    fixture = await _runtime_fixture()
    fixture.cascade.fail_closes = 2

    result = await fixture.runtime.switch_provider("quicksilver")

    snapshot = await fixture.sessions.get("voice-1")
    assert result.status is ProviderSwitchStatus.FAILED
    assert result.reason == "provider_cleanup_failed"
    assert snapshot.state is SessionState.FAILED
    assert await fixture.sessions.audio_owner() is None
    assert fixture.cascade.open_count == 1
    assert fixture.quicksilver.instances == []


async def test_failed_replacement_and_failed_rollback_fail_the_session() -> None:
    fixture = await _runtime_fixture(quicksilver_fail_starts=1)
    fixture.cascade.fail_starts = 1

    result = await fixture.runtime.switch_provider("quicksilver")

    snapshot = await fixture.sessions.get("voice-1")
    assert result.status is ProviderSwitchStatus.FAILED
    assert result.reason == "provider_rollback_failed"
    assert snapshot.state is SessionState.FAILED
    assert await fixture.sessions.audio_owner() is None


async def test_unknown_provider_is_refused_without_changing_generation() -> None:
    fixture = await _runtime_fixture()

    result = await fixture.runtime.switch_provider("unknown")

    assert result.status is ProviderSwitchStatus.REFUSED
    assert result.reason == "provider_unknown"
    assert (await fixture.sessions.get("voice-1")).provider_generation == 1


async def test_provider_ready_timeout_rolls_back() -> None:
    fixture = await _runtime_fixture(provider_ready_timeout_secs=0.01)
    fixture.quicksilver.start_gate = asyncio.Event()

    result = await fixture.runtime.switch_provider("quicksilver")

    snapshot = await fixture.sessions.get("voice-1")
    assert result.status is ProviderSwitchStatus.ROLLED_BACK
    assert result.reason == "provider_start_failed"
    assert snapshot.state is SessionState.LISTENING
    assert snapshot.active_provider == "cascade"
    assert fixture.quicksilver.instances[0].closed is True


async def test_cancellation_rolls_back_before_propagating() -> None:
    fixture = await _runtime_fixture()
    fixture.quicksilver.start_gate = asyncio.Event()
    switching = asyncio.create_task(fixture.runtime.switch_provider("quicksilver"))
    await fixture.quicksilver.start_started.wait()

    switching.cancel()
    with pytest.raises(asyncio.CancelledError):
        await switching

    snapshot = await fixture.sessions.get("voice-1")
    assert snapshot.state is SessionState.LISTENING
    assert snapshot.active_provider == "cascade"
    assert snapshot.provider_generation == 3
    assert fixture.quicksilver.instances[0].closed is True
    assert len(fixture.cascade.instances) == 2
    failed = next(
        event for event in fixture.events if event.type is SessionEventType.PROVIDER_SWITCH_FAILED
    )
    assert failed.metadata["reason"] == "switch_cancelled"


async def test_concurrent_duplicate_switches_are_serialized_and_idempotent() -> None:
    fixture = await _runtime_fixture()

    first, second = await asyncio.gather(
        fixture.runtime.switch_provider("quicksilver"),
        fixture.runtime.switch_provider("quicksilver"),
    )

    assert {first.status, second.status} == {
        ProviderSwitchStatus.ACTIVE,
        ProviderSwitchStatus.UNCHANGED,
    }
    assert (await fixture.sessions.get("voice-1")).provider_generation == 2
    assert len(fixture.quicksilver.instances) == 1


async def test_client_switch_control_is_bounded_and_other_messages_are_delegated() -> None:
    fixture = await _runtime_fixture()

    listed = await fixture.runtime.handle_client_message(
        {"label": "kassette", "type": "provider.list", "data": {}}
    )
    invalid = await fixture.runtime.handle_client_message(
        {
            "label": "kassette",
            "type": "provider.switch",
            "data": {"provider_id": "quicksilver", "force": "yes"},
        }
    )
    delegated = await fixture.runtime.handle_client_message(
        {"label": "kassette", "type": "synthetic", "data": {}}
    )
    switched = await fixture.runtime.handle_client_message(
        {
            "label": "kassette",
            "type": "provider.switch",
            "data": {
                "provider_id": "quicksilver",
                "expected_provider_generation": 1,
            },
        }
    )

    messages = [
        frame.message
        for frame, _direction in fixture.runtime.pushed_frames
        if isinstance(frame, OutputTransportMessageUrgentFrame)
    ]
    catalog = next(
        message for message in reversed(messages) if message["type"] == "provider.available"
    )
    assert listed is True
    assert invalid is False
    assert delegated is True
    assert switched is True
    assert catalog["data"]["active_provider"] == "cascade"
    assert [item["provider_id"] for item in catalog["data"]["providers"]] == [
        "cascade",
        "quicksilver",
    ]
    assert (await fixture.sessions.get("voice-1")).active_provider == "quicksilver"


async def test_active_adapter_receives_audio_frames() -> None:
    fixture = await _runtime_fixture()
    frame = InputAudioRawFrame(audio=b"\x00\x00", sample_rate=16_000, num_channels=1)

    await fixture.runtime.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert fixture.cascade.instances[0].trace[-1] == "InputAudioRawFrame"
    assert fixture.runtime.pushed_frames[-1] == (frame, FrameDirection.DOWNSTREAM)
