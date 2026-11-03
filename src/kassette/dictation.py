"""Toggle dictation: record on a hotkey, finalize, groom, and paste at the cursor."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from enum import StrEnum
from time import monotonic
from typing import Protocol

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InterimTranscriptionFrame,
    StartFrame,
    TranscriptionFrame,
)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.local.audio import LocalAudioTransportParams
from pipecat.workers.runner import WorkerRunner

from kassette.aws_credentials import AWSCredentialsUnavailableError
from kassette.hotkey import Hotkey
from kassette.providers.aws_transcribe import (
    TRANSCRIBE_SAMPLE_RATE,
    PausableAWSTranscribeSTTService,
)
from kassette.settings import KassetteSettings
from kassette.terminal_audio import StableLocalAudioTransport
from kassette.transcript_grooming import (
    TranscriptGroomer,
    TranscriptGroomingRequest,
    groom_transcript,
    load_transcript_groomer,
)

Notify = Callable[[str], None]

_START_TIMEOUT_SECS = 10.0
_FLUSH_TIMEOUT_SECS = 2.0
_CLOSE_TIMEOUT_SECS = 5.0
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


class DictationTranscriptCollector(FrameProcessor):
    """Collect final transcript segments in order at the end of the dictation pipeline."""

    def __init__(self) -> None:
        super().__init__(name="DictationTranscriptCollector")  # pyright: ignore[reportUnknownMemberType]
        self.started = asyncio.Event()
        self._finals: list[str] = []
        self._interim: str | None = None

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, InputAudioRawFrame):
            return
        if isinstance(frame, TranscriptionFrame):
            self._finals.append(frame.text)
            self._interim = None
        elif isinstance(frame, InterimTranscriptionFrame):
            self._interim = frame.text
        await self.push_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.started.set()

    def transcript(self, *, include_interim: bool = False) -> str:
        parts = [*self._finals]
        if include_interim and self._interim:
            parts.append(self._interim)
        return " ".join(part.strip() for part in parts if part.strip())


def build_dictation_stt(settings: KassetteSettings) -> PausableAWSTranscribeSTTService:
    """Build the transcription service for one recording."""
    if settings.transcription_provider != "aws":
        raise DictationError(
            "kassette dictate needs KASSETTE_TRANSCRIPTION_PROVIDER=aws, which ends the "
            f"stream with a definitive final transcript (got {settings.transcription_provider})"
        )
    try:
        credentials = settings.aws_transcription_credentials()
    except AWSCredentialsUnavailableError as error:
        raise DictationError(str(error)) from None
    return PausableAWSTranscribeSTTService(
        credentials=credentials,
        vocabulary_name=settings.aws_vocabulary_name,
        vocabulary_filter_name=settings.aws_vocabulary_filter_name,
    )


def validate_dictation_settings(settings: KassetteSettings) -> TranscriptGroomer:
    """Fail fast on provider, credential, and grooming profile problems."""
    build_dictation_stt(settings)
    try:
        return load_transcript_groomer(settings.transcript_grooming_profile)
    except (OSError, ValueError) as error:
        raise DictationError(
            f"transcript grooming profile can't be loaded ({type(error).__name__})"
        ) from None


InputFactory = Callable[[KassetteSettings], FrameProcessor]


def local_microphone_input(settings: KassetteSettings) -> FrameProcessor:
    transport = StableLocalAudioTransport(
        LocalAudioTransportParams(
            audio_in_enabled=True,
            audio_out_enabled=False,
            audio_in_sample_rate=TRANSCRIBE_SAMPLE_RATE,
            input_device_index=settings.input_device_index,
        ),
        input_name=settings.input_device_name,
    )
    return transport.input()


class PipecatDictationRecording:
    """One recording: microphone → AWS Transcribe → collector, torn down after finish."""

    def __init__(
        self,
        settings: KassetteSettings,
        *,
        input_factory: InputFactory = local_microphone_input,
        stt_factory: Callable[[KassetteSettings], PausableAWSTranscribeSTTService] = (
            build_dictation_stt
        ),
    ) -> None:
        self._settings = settings
        self._input_factory = input_factory
        self._stt_factory = stt_factory
        self._stt: PausableAWSTranscribeSTTService | None = None
        self._collector = DictationTranscriptCollector()
        self._worker: PipelineWorker | None = None
        self._run_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        stt = self._stt_factory(self._settings)
        self._stt = stt
        try:
            source = self._input_factory(self._settings)
        except Exception as error:
            raise DictationError(f"microphone unavailable ({error})") from None
        worker = PipelineWorker(
            Pipeline([source, stt, self._collector]),
            params=PipelineParams(enable_metrics=False, enable_usage_metrics=False),
            idle_timeout_secs=None,
            enable_rtvi=False,
        )
        self._worker = worker
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        run_task = asyncio.create_task(runner.run())
        self._run_task = run_task
        started = asyncio.create_task(self._collector.started.wait())
        done, _pending = await asyncio.wait(
            {started, run_task},
            timeout=_START_TIMEOUT_SECS,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if started not in done:
            started.cancel()
            if run_task in done and not run_task.cancelled() and run_task.exception():
                failure = type(run_task.exception()).__name__
                raise DictationError(f"audio pipeline failed ({failure})")
            raise DictationError(stt.last_error or "the microphone stream didn't start in time")
        if not stt.connected:
            raise DictationError(stt.last_error or "AWS Transcribe didn't connect")

    async def finish(self) -> str:
        stt = self._stt
        worker = self._worker
        if stt is None or worker is None:
            raise DictationError("recording was not started")
        finalized = await stt.finish_input()
        await worker.flush_pipeline(timeout=_FLUSH_TIMEOUT_SECS)
        text = self._collector.transcript(include_interim=not finalized)
        if not text and stt.last_error:
            raise DictationError(stt.last_error)
        return text

    async def close(self) -> None:
        if self._worker is not None:
            await self._worker.cancel()
        run_task = self._run_task
        if run_task is not None:
            with suppress(Exception, asyncio.CancelledError):
                await asyncio.wait_for(run_task, _CLOSE_TIMEOUT_SECS)


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
        new_recording=lambda: PipecatDictationRecording(settings),
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
