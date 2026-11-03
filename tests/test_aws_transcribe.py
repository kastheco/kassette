from __future__ import annotations

import asyncio
import binascii
import json
import struct
from collections.abc import AsyncGenerator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest
from pipecat.frames.frames import Frame, InputAudioRawFrame, StartFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.aws.utils import get_headers
from pipecat.workers.runner import WorkerRunner
from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve

from kassette.aws_credentials import AWSCredentialsUnavailableError, AWSTranscribeCredentials
from kassette.dictation import (
    DictationError,
    DictationTranscriptCollector,
    PipecatDictationRecording,
    build_dictation_stt,
)
from kassette.providers import aws_transcribe
from kassette.providers.aws_transcribe import (
    PausableAWSTranscribeSTTService,
    transcribe_stream_url,
)
from kassette.providers.builtin import build_builtin_provider_registry, build_cascade_stt
from kassette.providers.runtime import CredentialReadiness
from kassette.sessions import SessionRegistry
from kassette.settings import KassetteSettings, load_settings

ACCESS_KEY = "AKIDKASSETTETEST"
SECRET_KEY = "kassette-test-secret-access-key"
SESSION_TOKEN = "kassette-test-session-token"


@pytest.fixture
def isolated_aws(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the AWS chain away from the host's profiles and instance metadata."""
    for name in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        "KASSETTE_AWS_REGION",
        "KASSETTE_AWS_VOCABULARY_NAME",
        "KASSETTE_AWS_VOCABULARY_FILTER_NAME",
        "KASSETTE_TRANSCRIPTION_PROVIDER",
        "KASSETTE_DICTATE_HOTKEY",
        "FISH_API_KEY",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "missing-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "missing-credentials"))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_FULL_URI", raising=False)
    monkeypatch.delenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", raising=False)
    yield


def _settings(**values: str) -> KassetteSettings:
    return KassetteSettings.model_validate(values)


def _env(tmp_path: Path, body: str) -> Path:
    env_file = tmp_path / ".env"
    env_file.write_text(body, encoding="utf-8")
    return env_file


def test_aws_provider_resolves_env_credentials_and_region_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_aws: None
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", ACCESS_KEY)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET_KEY)
    monkeypatch.setenv("AWS_SESSION_TOKEN", SESSION_TOKEN)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    settings = load_settings(
        env_file=_env(tmp_path, "KASSETTE_TRANSCRIPTION_PROVIDER=aws\nFISH_API_KEY=fish\n")
    )

    credential, fish_key = settings.cascade_credentials()

    assert isinstance(credential, AWSTranscribeCredentials)
    assert credential.region == "eu-west-1"
    assert credential.session_token == SESSION_TOKEN
    assert fish_key == "fish"
    assert SECRET_KEY not in repr(credential)
    assert SESSION_TOKEN not in repr(credential)

    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert settings.aws_transcription_credentials().region == "us-west-2"
    explicit = load_settings(
        env_file=_env(
            tmp_path, "KASSETTE_TRANSCRIPTION_PROVIDER=aws\nKASSETTE_AWS_REGION=ap-south-1\n"
        )
    )
    assert explicit.aws_transcription_credentials().region == "ap-south-1"


def test_aws_provider_reads_a_named_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_aws: None
) -> None:
    config = tmp_path / "config"
    config.write_text("[profile personal]\nregion = us-east-2\n", encoding="utf-8")
    credentials = tmp_path / "credentials"
    credentials.write_text(
        f"[personal]\naws_access_key_id = {ACCESS_KEY}\naws_secret_access_key = {SECRET_KEY}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_PROFILE", "personal")

    resolved = _settings(KASSETTE_TRANSCRIPTION_PROVIDER="aws").aws_transcription_credentials()

    assert resolved.access_key_id == ACCESS_KEY
    assert resolved.region == "us-east-2"


def test_missing_aws_credentials_fail_with_setup_guidance(isolated_aws: None) -> None:
    settings = _settings(KASSETTE_TRANSCRIPTION_PROVIDER="aws")

    with pytest.raises(AWSCredentialsUnavailableError, match="AWS_PROFILE"):
        settings.transcription_credential()
    with pytest.raises(DictationError, match="AWS_PROFILE"):
        build_dictation_stt(settings)


def test_unknown_aws_profile_fails_without_a_traceback_chain(
    monkeypatch: pytest.MonkeyPatch, isolated_aws: None
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "does-not-exist")

    with pytest.raises(AWSCredentialsUnavailableError, match="does-not-exist") as raised:
        _settings(KASSETTE_TRANSCRIPTION_PROVIDER="aws").aws_transcription_credentials()
    assert raised.value.__cause__ is None


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("KASSETTE_TRANSCRIPTION_PROVIDER", "azure"),
        ("KASSETTE_AWS_REGION", "us east 1"),
        ("KASSETTE_AWS_VOCABULARY_NAME", "name&inject=1"),
        ("KASSETTE_AWS_VOCABULARY_FILTER_NAME", "has space"),
        ("KASSETTE_DICTATE_HOTKEY", "d"),
    ],
)
def test_invalid_aws_and_dictation_settings_are_rejected(
    tmp_path: Path, isolated_aws: None, variable: str, value: str
) -> None:
    with pytest.raises(ValidationError, match=variable):
        load_settings(env_file=_env(tmp_path, f"{variable}={value}\n"))


def test_cascade_builds_aws_stt_with_vocabulary_options(tmp_path: Path, isolated_aws: None) -> None:
    settings = load_settings(
        env_file=_env(
            tmp_path,
            "KASSETTE_TRANSCRIPTION_PROVIDER=aws\n"
            "KASSETTE_AWS_VOCABULARY_NAME=kas-words\n"
            "KASSETTE_AWS_VOCABULARY_FILTER_NAME=fillers\n",
        )
    )
    credential = AWSTranscribeCredentials(ACCESS_KEY, SECRET_KEY, None, "us-east-1")

    service = build_cascade_stt(settings, credential)

    assert isinstance(service, PausableAWSTranscribeSTTService)
    with pytest.raises(TypeError):
        build_cascade_stt(settings, "not-aws-credentials")


def test_aws_cascade_readiness_waits_for_start_and_still_requires_fish(
    isolated_aws: None,
) -> None:
    def readiness(settings: KassetteSettings) -> CredentialReadiness:
        registry = build_builtin_provider_registry(settings, session_registry=SessionRegistry())
        provider = registry.resolve("cascade")
        assert provider is not None
        return provider.capabilities.credential_readiness

    assert (
        readiness(_settings(KASSETTE_TRANSCRIPTION_PROVIDER="aws")) is CredentialReadiness.MISSING
    )
    with_fish = _settings(KASSETTE_TRANSCRIPTION_PROVIDER="aws", FISH_API_KEY="fish")
    assert readiness(with_fish) is CredentialReadiness.UNKNOWN


def test_dictation_rejects_providers_without_a_definitive_end_of_stream() -> None:
    with pytest.raises(DictationError, match="KASSETTE_TRANSCRIPTION_PROVIDER=aws"):
        build_dictation_stt(_settings(KASSETTE_TRANSCRIPTION_PROVIDER="gemini"))


def test_presigned_url_applies_vocabulary_filter_with_remove_method() -> None:
    credentials = AWSTranscribeCredentials(ACCESS_KEY, SECRET_KEY, SESSION_TOKEN, "us-west-2")

    url = transcribe_stream_url(
        credentials,
        language_code="en-US",
        vocabulary_name="kas-words",
        vocabulary_filter_name="fillers",
    )
    plain = dict(
        parse_qsl(urlsplit(transcribe_stream_url(credentials, language_code="en-US")).query)
    )

    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    assert parts.scheme == "wss"
    assert parts.netloc == "transcribestreaming.us-west-2.amazonaws.com:8443"
    assert query["vocabulary-name"] == "kas-words"
    assert query["vocabulary-filter-name"] == "fillers"
    assert query["vocabulary-filter-method"] == "remove"
    assert query["sample-rate"] == "16000"
    assert query["X-Amz-Security-Token"] == SESSION_TOKEN
    assert SECRET_KEY not in url
    assert not {"vocabulary-name", "vocabulary-filter-name", "vocabulary-filter-method"} & set(
        plain
    )


def transcript_event(text: str, *, partial: bool) -> bytes:
    headers = bytearray()
    headers.extend(get_headers(":message-type", "event"))
    headers.extend(get_headers(":event-type", "TranscriptEvent"))
    headers.extend(get_headers(":content-type", "application/json"))
    payload = json.dumps(
        {
            "Transcript": {
                "Results": [{"Alternatives": [{"Transcript": text}], "IsPartial": partial}]
            }
        }
    ).encode()
    prelude = struct.pack(">II", len(headers) + len(payload) + 16, len(headers))
    message = bytearray(prelude)
    message.extend(struct.pack(">I", binascii.crc32(prelude) & 0xFFFFFFFF))
    message.extend(headers)
    message.extend(payload)
    message.extend(struct.pack(">I", binascii.crc32(bytes(message)) & 0xFFFFFFFF))
    return bytes(message)


def audio_payload(message: bytes) -> bytes:
    headers_length = struct.unpack(">I", message[4:8])[0]
    return message[12 + headers_length : -4]


@dataclass
class FakeTranscribe:
    """Sends a partial on first audio, finals only after the empty end-of-stream event."""

    finals: list[str]
    partial: str = "partial words"
    connections: int = 0
    audio_bytes: int = 0
    ended: bool = False
    audio_after_end: int = 0
    server: Server | None = field(default=None, repr=False)

    @property
    def url(self) -> str:
        assert self.server is not None
        host, port = next(iter(self.server.sockets)).getsockname()[:2]
        return f"ws://{host}:{port}/stream-transcription-websocket"

    async def start(self) -> None:
        self.server = await serve(self._handle, "127.0.0.1", 0)

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handle(self, connection: ServerConnection) -> None:
        self.connections += 1
        sent_partial = False
        async for message in connection:
            assert isinstance(message, bytes)
            payload = audio_payload(message)
            if self.ended:
                self.audio_after_end += len(payload)
                continue
            if payload:
                self.audio_bytes += len(payload)
                if not sent_partial:
                    await connection.send(transcript_event(self.partial, partial=True))
                    sent_partial = True
                continue
            self.ended = True
            for text in self.finals:
                await connection.send(transcript_event(text, partial=False))
            await connection.close()
            return


def _fixed_url(url: str) -> Callable[..., str]:
    def build(*_args: object, **_kwargs: object) -> str:
        return url

    return build


class _SilentMicrophone(FrameProcessor):
    """Stands in for PortAudio: emits 20 ms PCM frames once the pipeline starts."""

    def __init__(self) -> None:
        super().__init__(name="SilentMicrophone")  # pyright: ignore[reportUnknownMemberType]
        self._task: asyncio.Task[None] | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame) and self._task is None:
            self._task = asyncio.create_task(self._produce())

    async def _produce(self) -> None:
        while True:
            await self.push_frame(
                InputAudioRawFrame(audio=b"\x01\x00" * 320, sample_rate=16_000, num_channels=1)
            )
            await asyncio.sleep(0.02)

    async def cleanup(self) -> None:
        if self._task is not None:
            self._task.cancel()
        await super().cleanup()


@pytest.fixture
async def fake_transcribe(monkeypatch: pytest.MonkeyPatch) -> AsyncGenerator[FakeTranscribe, None]:
    server = FakeTranscribe(finals=["um so the jepp release", "ships on friday."])
    await server.start()
    monkeypatch.setattr(aws_transcribe, "transcribe_stream_url", _fixed_url(server.url))
    yield server
    await server.stop()


def _aws_stt(_settings: KassetteSettings) -> PausableAWSTranscribeSTTService:
    return PausableAWSTranscribeSTTService(
        credentials=AWSTranscribeCredentials(ACCESS_KEY, SECRET_KEY, None, "us-east-1")
    )


async def test_recording_waits_for_finals_after_end_of_stream(
    fake_transcribe: FakeTranscribe,
) -> None:
    recording = PipecatDictationRecording(
        _settings(KASSETTE_TRANSCRIPTION_PROVIDER="aws"),
        input_factory=lambda _settings: _SilentMicrophone(),
        stt_factory=_aws_stt,
    )
    await recording.start()
    try:
        await asyncio.sleep(0.2)
        text = await recording.finish()
    finally:
        await recording.close()

    assert text == "um so the jepp release ships on friday."
    assert fake_transcribe.audio_bytes > 0
    assert fake_transcribe.ended
    assert fake_transcribe.audio_after_end == 0
    assert fake_transcribe.connections == 1


async def test_cascade_pause_drains_finals_and_resume_reconnects(
    fake_transcribe: FakeTranscribe,
) -> None:
    stt = _aws_stt(_settings())
    collector = DictationTranscriptCollector()
    worker = PipelineWorker(
        Pipeline([_SilentMicrophone(), stt, collector]),
        idle_timeout_secs=None,
        enable_rtvi=False,
    )
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    run_task = asyncio.create_task(runner.run())
    try:
        await asyncio.wait_for(collector.started.wait(), 5)
        await stt.pause_input()
        await worker.flush_pipeline(timeout=2)
        assert collector.transcript() == "um so the jepp release ships on friday."
        assert not stt.connected

        await stt.resume_input()
        assert stt.connected
        assert fake_transcribe.connections == 2
    finally:
        await worker.cancel()
        await asyncio.wait_for(run_task, 5)


async def test_recording_start_reports_rejected_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        aws_transcribe, "transcribe_stream_url", _fixed_url("ws://127.0.0.1:9/closed")
    )
    recording = PipecatDictationRecording(
        _settings(KASSETTE_TRANSCRIPTION_PROVIDER="aws"),
        input_factory=lambda _settings: _SilentMicrophone(),
        stt_factory=_aws_stt,
    )
    try:
        with pytest.raises(DictationError, match="Unable to connect to AWS Transcribe"):
            await recording.start()
    finally:
        await recording.close()
