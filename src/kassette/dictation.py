"""Toggle dictation: record on a hotkey, finalize, groom, and paste at the cursor.

Pipecat-free. Audio comes from sounddevice and goes straight to AWS Transcribe
streaming through amazon-transcribe, so `kassette[dictate]` stays small.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable, Awaitable, Callable, Sequence
from contextlib import suppress
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol

from kassette.aws_credentials import AWSCredentialsUnavailableError, AWSTranscribeCredentials
from kassette.hotkey import Hotkey
from kassette.settings import KassetteSettings
from kassette.transcript_grooming import (
    TranscriptGroomer,
    TranscriptGroomingRequest,
    groom_transcript,
    load_transcript_groomer,
)

Notify = Callable[[str], None]

_PASTE_SETTLE_SECS = 0.25


class DictationError(RuntimeError):
    """A dictation failure whose message is safe to print. Never contains transcript text."""


class DictationState(StrEnum):
    IDLE = "idle"
    RECORDING = "recording"
    FINALIZING = "finalizing"


class PasteOutcome(StrEnum):
    PASTED = "pasted"
    CLIPBOARD = "clipboard"


class Clipboard(Protocol):
    def snapshot(self) -> object: ...

    def write_text(self, text: str) -> int:
        """Replace the clipboard with text and return the resulting change count."""
        ...

    def change_count(self) -> int: ...

    def restore(self, snapshot: object) -> None: ...


class PasteKeystroke(Protocol):
    def permitted(self) -> bool: ...

    def send_paste(self) -> None: ...


async def paste_text(
    text: str,
    clipboard: Clipboard,
    keystroke: PasteKeystroke,
    *,
    notify: Notify,
    settle_secs: float = _PASTE_SETTLE_SECS,
) -> PasteOutcome:
    """Paste through the clipboard, restoring it afterwards when paste is possible.

    When the paste keystroke isn't permitted or fails, the transcript stays on the
    clipboard so it can be pasted by hand.
    """
    saved = clipboard.snapshot()
    written = clipboard.write_text(text)
    if not keystroke.permitted():
        notify(
            "paste isn't permitted (grant Accessibility to this terminal). "
            "the transcript is on the clipboard"
        )
        return PasteOutcome.CLIPBOARD
    try:
        keystroke.send_paste()
    except Exception as error:
        notify(f"paste failed ({type(error).__name__}). the transcript is on the clipboard")
        return PasteOutcome.CLIPBOARD
    # The focused app reads the pasteboard asynchronously after Cmd-V.
    try:
        await asyncio.sleep(settle_secs)
    finally:
        if clipboard.change_count() == written:
            clipboard.restore(saved)
        else:
            notify("the clipboard changed during paste, so the previous clipboard wasn't restored")
    return PasteOutcome.PASTED


class DictationRecording(Protocol):
    async def start(self) -> None: ...

    async def finish(self) -> str:
        """Stop input and return the raw final transcript."""
        ...

    async def close(self) -> None: ...


class DictationController:
    """Toggle state machine: idle → recording → finalizing → idle.

    A toggle while finalizing queues one new recording, and another toggle
    cancels that queued recording. Both are announced through `notify`.
    """

    def __init__(
        self,
        *,
        new_recording: Callable[[], DictationRecording],
        groom: Callable[[str], Awaitable[str]],
        deliver: Callable[[str], Awaitable[PasteOutcome]],
        notify: Notify,
    ) -> None:
        self._new_recording = new_recording
        self._groom = groom
        self._deliver = deliver
        self._notify = notify
        self._state = DictationState.IDLE
        self._stop_requested = asyncio.Event()
        self._queued = False
        self._task: asyncio.Task[None] | None = None
        self._idle = asyncio.Event()
        self._idle.set()

    @property
    def state(self) -> DictationState:
        return self._state

    @property
    def queued(self) -> bool:
        return self._queued

    def toggle(self) -> None:
        if self._state is DictationState.IDLE:
            self._begin()
        elif self._state is DictationState.RECORDING:
            self._state = DictationState.FINALIZING
            self._stop_requested.set()
        elif self._queued:
            self._queued = False
            self._notify("queued recording cancelled")
        else:
            self._queued = True
            self._notify("still finalizing. a new recording starts after this paste")

    async def wait_idle(self) -> None:
        await self._idle.wait()

    async def close(self) -> None:
        self._queued = False
        task = self._task
        if task is not None and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    def _begin(self) -> None:
        self._state = DictationState.RECORDING
        self._stop_requested = asyncio.Event()
        self._idle.clear()
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        recording = self._new_recording()
        try:
            try:
                await recording.start()
            except DictationError as error:
                self._notify(f"recording didn't start: {error}")
                return
            if not self._stop_requested.is_set():
                self._notify("recording")
            await self._stop_requested.wait()
            stopped = monotonic()
            self._notify("finalizing")
            raw = await recording.finish()
            if not raw.strip():
                self._notify("no speech was recognized. nothing was pasted")
                return
            text = await self._groom(raw)
            outcome = await self._deliver(text)
            elapsed = monotonic() - stopped
            if outcome is PasteOutcome.PASTED:
                self._notify(f"pasted {len(text)} characters {elapsed:.2f}s after stop")
            else:
                self._notify(f"copied {len(text)} characters {elapsed:.2f}s after stop")
        except DictationError as error:
            self._notify(f"dictation failed: {error}")
        except Exception as error:
            self._notify(f"dictation failed: {type(error).__name__}")
        finally:
            with suppress(Exception):
                await recording.close()
            self._state = DictationState.IDLE
            self._idle.set()
            if self._queued:
                self._queued = False
                self._begin()


TRANSCRIBE_SAMPLE_RATE = 16_000
TRANSCRIBE_LANGUAGE = "en-US"
# 100 ms of 16-bit mono PCM per audio event, well under the 32 KB event limit.
_CHUNK_FRAMES = TRANSCRIBE_SAMPLE_RATE // 10


def dictation_credentials(settings: KassetteSettings) -> AWSTranscribeCredentials:
    """Resolve AWS credentials for one recording, or fail with a printable reason."""
    if settings.transcription_provider != "aws":
        raise DictationError(
            "kassette dictate needs KASSETTE_TRANSCRIPTION_PROVIDER=aws, which ends the "
            f"stream with a definitive final transcript (got {settings.transcription_provider})"
        )
    try:
        return settings.aws_transcription_credentials()
    except AWSCredentialsUnavailableError as error:
        raise DictationError(str(error)) from None


def validate_dictation_settings(settings: KassetteSettings) -> TranscriptGroomer:
    """Fail fast on provider, credential, and grooming profile problems."""
    dictation_credentials(settings)
    try:
        return load_transcript_groomer(settings.transcript_grooming_profile)
    except (OSError, ValueError) as error:
        raise DictationError(
            f"transcript grooming profile can't be loaded ({type(error).__name__})"
        ) from None


class TranscribeAudioInput(Protocol):
    async def send_audio_event(self, audio_chunk: bytes | None) -> None: ...

    async def end_stream(self) -> None: ...


class TranscribeAlternative(Protocol):
    @property
    def transcript(self) -> str: ...


class TranscribeResult(Protocol):
    @property
    def is_partial(self) -> bool | None: ...

    @property
    def alternatives(self) -> Sequence[TranscribeAlternative] | None: ...


class TranscribeTranscript(Protocol):
    @property
    def results(self) -> Sequence[TranscribeResult]: ...


class TranscribeEvent(Protocol):
    @property
    def transcript(self) -> TranscribeTranscript: ...


class TranscribeStream(Protocol):
    """The part of amazon-transcribe's StartStreamTranscriptionEventStream we use."""

    @property
    def input_stream(self) -> TranscribeAudioInput: ...

    @property
    def output_stream(self) -> AsyncIterable[TranscribeEvent]: ...


StreamOpener = Callable[[KassetteSettings], Awaitable[TranscribeStream]]


async def open_transcribe_stream(settings: KassetteSettings) -> TranscribeStream:
    """Start one AWS Transcribe stream. Credentials never reach logs or messages."""
    from amazon_transcribe.auth import StaticCredentialResolver
    from amazon_transcribe.client import TranscribeStreamingClient

    credentials = await asyncio.to_thread(dictation_credentials, settings)
    client = TranscribeStreamingClient(
        region=credentials.region,
        credential_resolver=StaticCredentialResolver(
            credentials.access_key_id,
            credentials.secret_access_key,
            credentials.session_token,
        ),
    )
    filter_name = settings.aws_vocabulary_filter_name
    stream: Any = await client.start_stream_transcription(  # pyright: ignore[reportUnknownMemberType]
        language_code=TRANSCRIBE_LANGUAGE,
        media_sample_rate_hz=TRANSCRIBE_SAMPLE_RATE,
        media_encoding="pcm",
        vocabulary_name=settings.aws_vocabulary_name,
        vocab_filter_name=filter_name,
        vocab_filter_method="remove" if filter_name else None,
        enable_partial_results_stabilization=True,
        partial_results_stability="high",
    )
    return stream


class AudioInput(Protocol):
    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        """Start capture. `on_chunk` runs on the audio thread with 16 kHz mono PCM."""
        ...

    def stop(self) -> None:
        """Stop capture and wait for audio callbacks to finish. Safe to call repeatedly."""
        ...


class SoundDeviceMicrophone:
    """PortAudio capture through sounddevice, which bundles PortAudio on macOS."""

    def __init__(self, device: int | str | None = None) -> None:
        self._device = device
        self._stream: Any = None

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        import sounddevice

        def callback(data: Any, _frames: int, _time: Any, _status: Any) -> None:
            on_chunk(bytes(data))

        stream = sounddevice.RawInputStream(
            samplerate=TRANSCRIBE_SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=_CHUNK_FRAMES,
            device=self._device,
            callback=callback,
        )
        self._stream = stream
        stream.start()

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            stream.stop()
            stream.close()


def settings_microphone(settings: KassetteSettings) -> AudioInput:
    if settings.input_device_index is not None:
        return SoundDeviceMicrophone(settings.input_device_index)
    return SoundDeviceMicrophone(settings.input_device_name)


def _describe_stream_error(error: BaseException) -> str:
    message = getattr(error, "message", None)
    name = type(error).__name__
    return f"{name}: {message}" if isinstance(message, str) and message else name


class TranscribeDictationRecording:
    """One recording: microphone → AWS Transcribe, ended with a drained final result."""

    _START_TIMEOUT_SECS = 10.0
    _FINISH_TIMEOUT_SECS = 3.0

    def __init__(
        self,
        settings: KassetteSettings,
        *,
        microphone: AudioInput,
        open_stream: StreamOpener = open_transcribe_stream,
    ) -> None:
        self._settings = settings
        self._microphone = microphone
        self._open_stream = open_stream
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._finals: list[str] = []
        self._partial: str | None = None
        self._sender: asyncio.Task[None] | None = None
        self._receiver: asyncio.Task[None] | None = None

    async def start(self) -> None:
        try:
            async with asyncio.timeout(self._START_TIMEOUT_SECS):
                stream = await self._open_stream(self._settings)
        except DictationError:
            raise
        except TimeoutError:
            raise DictationError("AWS Transcribe didn't accept the stream in time") from None
        except Exception as error:
            raise DictationError(
                f"AWS Transcribe rejected the stream ({_describe_stream_error(error)})"
            ) from None
        self._receiver = asyncio.create_task(self._receive(stream))
        self._sender = asyncio.create_task(self._send(stream))
        loop = asyncio.get_running_loop()
        audio = self._audio

        def on_chunk(chunk: bytes) -> None:
            loop.call_soon_threadsafe(audio.put_nowait, chunk)

        try:
            self._microphone.start(on_chunk)
        except Exception as error:
            raise DictationError(f"microphone unavailable ({type(error).__name__})") from None

    async def _send(self, stream: TranscribeStream) -> None:
        while (chunk := await self._audio.get()) is not None:
            await stream.input_stream.send_audio_event(audio_chunk=chunk)
        # AWS answers the end of stream with the remaining finals, then closes.
        await stream.input_stream.end_stream()

    async def _receive(self, stream: TranscribeStream) -> None:
        async for event in stream.output_stream:
            for result in event.transcript.results:
                if not result.alternatives:
                    continue
                text = result.alternatives[0].transcript
                if result.is_partial:
                    self._partial = text
                else:
                    self._finals.append(text)
                    self._partial = None

    def transcript(self, *, include_partial: bool = False) -> str:
        parts = [*self._finals]
        if include_partial and self._partial:
            parts.append(self._partial)
        return " ".join(part.strip() for part in parts if part.strip())

    async def finish(self) -> str:
        sender, receiver = self._sender, self._receiver
        if sender is None or receiver is None:
            raise DictationError("recording was not started")
        self._microphone.stop()
        # Stop joins the audio callbacks. Schedule the marker behind their pending
        # queue insertions so every captured chunk reaches AWS before end_stream.
        asyncio.get_running_loop().call_soon(self._audio.put_nowait, None)
        done, _pending = await asyncio.wait(
            {sender, receiver},
            timeout=self._FINISH_TIMEOUT_SECS,
            return_when=asyncio.ALL_COMPLETED,
        )
        failure = next(
            (task.exception() for task in (sender, receiver) if task in done and task.exception()),
            None,
        )
        finalized = receiver in done and failure is None
        text = self.transcript(include_partial=not finalized)
        if not text and failure is not None:
            raise DictationError(f"AWS Transcribe failed ({_describe_stream_error(failure)})")
        return text

    async def close(self) -> None:
        with suppress(Exception):
            self._microphone.stop()
        for task in (self._sender, self._receiver):
            if task is not None and not task.done():
                task.cancel()
            if task is not None:
                with suppress(BaseException):
                    await task


class DictationPlatform(Protocol):
    @property
    def clipboard(self) -> Clipboard: ...

    @property
    def keystroke(self) -> PasteKeystroke: ...

    def listen(self, hotkey: Hotkey, on_press: Callable[[], None]) -> Callable[[], None]:
        """Start the global hotkey listener and return a function that stops it."""
        ...


async def run_dictation(
    settings: KassetteSettings,
    groomer: TranscriptGroomer,
    *,
    platform: DictationPlatform,
    notify: Notify,
) -> None:
    """Run until cancelled. Recording only happens between hotkey toggles."""
    hotkey = settings.dictation_hotkey
    loop = asyncio.get_running_loop()

    async def groom(text: str) -> str:
        return await groom_transcript(
            groomer,
            TranscriptGroomingRequest(text=text, final=True),
            timeout_secs=settings.transcript_grooming_timeout_secs,
        )

    async def deliver(text: str) -> PasteOutcome:
        return await paste_text(text, platform.clipboard, platform.keystroke, notify=notify)

    controller = DictationController(
        new_recording=lambda: TranscribeDictationRecording(
            settings, microphone=settings_microphone(settings)
        ),
        groom=groom,
        deliver=deliver,
        notify=notify,
    )

    def on_press() -> None:
        loop.call_soon_threadsafe(controller.toggle)

    stop_listening = platform.listen(hotkey, on_press)
    notify(f"ready. press {hotkey.label} to start and stop dictation. ctrl-c quits")
    try:
        await asyncio.Event().wait()
    finally:
        stop_listening()
        await controller.close()
