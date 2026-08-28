"""Factories for Kassette's built-in runtime-switchable providers."""

from __future__ import annotations

import asyncio
from typing import Any

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.services.fish.tts import FishAudioTTSService
from pipecat.services.google.gemini_live.stt import GeminiSTTService

from kassette.credentials import CodexCredentialProvider, PiAuthCredentialProvider
from kassette.providers.cascade import (
    CascadedBargeInProcessor,
    CascadedVoiceEvents,
    handle_client_message,
)
from kassette.providers.quicksilver.service import GPTLiveService, TransportFactory
from kassette.providers.quicksilver.transport import QuicksilverTransport
from kassette.providers.runtime import (
    CredentialReadiness,
    PipelineProviderAdapter,
    VoiceProviderBuildContext,
    VoiceProviderCapabilities,
    VoiceProviderDefinition,
    VoiceProviderMode,
    VoiceProviderRegistry,
)
from kassette.sessions import SessionRegistry
from kassette.settings import KassetteSettings
from kassette.transcript_grooming import (
    TranscriptGroomingProcessor,
    load_transcript_groomer,
)


class PausableGeminiSTTService(GeminiSTTService):
    """Gemini STT adapter whose billable input can be paused independently."""

    _INPUT_PAUSE_GRACE_SECS = 0.2
    _input_paused = False
    _input_pause_lock: asyncio.Lock | None = None

    def _pause_lock(self) -> asyncio.Lock:
        if self._input_pause_lock is None:
            self._input_pause_lock = asyncio.Lock()
        return self._input_pause_lock

    async def pause_input(self) -> None:
        async with self._pause_lock():
            if self._input_paused:
                return
            await self._send_finalization_signal()
            await asyncio.sleep(self._INPUT_PAUSE_GRACE_SECS)
            await self._disconnect()
            self._input_paused = True

    async def resume_input(self) -> None:
        async with self._pause_lock():
            if not self._input_paused:
                return
            await self._connect()
            self._input_paused = False


def build_builtin_provider_registry(
    settings: KassetteSettings,
    *,
    session_registry: SessionRegistry,
    credential_provider: CodexCredentialProvider | None = None,
    quicksilver_transport_factory: TransportFactory = QuicksilverTransport,
) -> VoiceProviderRegistry:
    """Build the two real provider adapters behind one runtime registry."""
    cascade_readiness = (
        CredentialReadiness.READY
        if settings.google_api_key is not None and settings.fish_api_key is not None
        else CredentialReadiness.MISSING
    )
    quicksilver_credentials = credential_provider or PiAuthCredentialProvider()

    def build_cascade(context: VoiceProviderBuildContext) -> PipelineProviderAdapter:
        google_api_key, fish_api_key = settings.cascade_credentials()
        vad = VADProcessor(
            vad_analyzer=SileroVADAnalyzer(
                params=VADParams(stop_secs=settings.vad_stop_secs),
            )
        )
        stt = PausableGeminiSTTService(api_key=google_api_key, sample_rate=16_000)
        grooming = TranscriptGroomingProcessor(
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
            session_id=context.session_id,
            event_sink=context.event_sink,
            name="CascadedTranscriptEvents",
            publish_speech=False,
        )
        barge_in = CascadedBargeInProcessor(name="CascadedBargeIn")
        speech_events = CascadedVoiceEvents(
            session_id=context.session_id,
            event_sink=context.event_sink,
            name="CascadedSpeechEvents",
            publish_transcripts=False,
            publish_start_state=False,
        )
        adapter: PipelineProviderAdapter | None = None

        async def speak(text: str) -> None:
            if adapter is None:
                raise RuntimeError("cascade provider adapter is not ready")
            await barge_in.queue_speech(text, adapter.pipeline.queue_frame)

        async def set_input_paused(paused: bool) -> None:
            if paused:
                await stt.pause_input()
            else:
                await stt.resume_input()
            await transcript_events.publish_input_state(paused=paused)

        async def route_message(message: Any) -> bool:
            return await handle_client_message(
                message,
                speak,
                set_input_paused=set_input_paused,
            )

        adapter = PipelineProviderAdapter(
            [
                vad,
                stt,
                grooming,
                transcript_events,
                tts,
                barge_in,
                speech_events,
            ],
            frame_sink=context.frame_sink,
            message_handler=route_message,
        )
        return adapter

    def build_quicksilver(context: VoiceProviderBuildContext) -> PipelineProviderAdapter:
        service = GPTLiveService(
            session_id=context.session_id,
            generation=context.session_generation,
            registry=session_registry,
            credentials=quicksilver_credentials,
            event_sink=context.event_sink,
            transport_factory=quicksilver_transport_factory,
            manage_session_lifecycle=False,
        )
        return PipelineProviderAdapter([service], frame_sink=context.frame_sink)

    return VoiceProviderRegistry(
        [
            VoiceProviderDefinition(
                capabilities=VoiceProviderCapabilities(
                    provider_id="cascade",
                    mode=VoiceProviderMode.CASCADED,
                    credential_readiness=cascade_readiness,
                    supports_input_pause=True,
                ),
                factory=build_cascade,
            ),
            VoiceProviderDefinition(
                capabilities=VoiceProviderCapabilities(
                    provider_id="quicksilver",
                    mode=VoiceProviderMode.NATIVE,
                    credential_readiness=CredentialReadiness.UNKNOWN,
                    supports_input_pause=False,
                ),
                factory=build_quicksilver,
            ),
        ]
    )
