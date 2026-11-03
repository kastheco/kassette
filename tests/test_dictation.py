from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable

import pytest
from amazon_transcribe.exceptions import BadRequestException
from amazon_transcribe.model import Alternative, Result, Transcript, TranscriptEvent

from kassette.dictation import (
    DictationController,
    DictationError,
    DictationState,
    PasteOutcome,
    TranscribeDictationRecording,
    TranscribeStream,
    paste_text,
)
from kassette.hotkey import COMMAND_MASK, CONTROL_MASK, OPTION_MASK, SHIFT_MASK, parse_hotkey
from kassette.settings import KassetteSettings


class FakeRecording:
    def __init__(self, transcript: str, *, start_error: str | None = None) -> None:
        self.transcript = transcript
        self.start_error = start_error
        self.release_finish = asyncio.Event()
        self.release_finish.set()
        self.started = False
        self.finished = False
        self.closed = False

    async def start(self) -> None:
        if self.start_error is not None:
            raise DictationError(self.start_error)
        self.started = True

    async def finish(self) -> str:
        await self.release_finish.wait()
        self.finished = True
        return self.transcript

    async def close(self) -> None:
        self.closed = True


class Harness:
    def __init__(self, *transcripts: str) -> None:
        self.pending = [FakeRecording(text) for text in transcripts]
        self.recordings: list[FakeRecording] = []
        self.delivered: list[str] = []
        self.messages: list[str] = []
        self.controller = DictationController(
            new_recording=self._new_recording,
            groom=self._groom,
            deliver=self._deliver,
            notify=self.messages.append,
        )

    def _new_recording(self) -> FakeRecording:
        recording = self.pending.pop(0)
        self.recordings.append(recording)
        return recording

    async def _groom(self, text: str) -> str:
        return text.replace("um ", "")

    async def _deliver(self, text: str) -> PasteOutcome:
        self.delivered.append(text)
        return PasteOutcome.PASTED


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_toggle_cycles_idle_recording_finalizing_idle() -> None:
    harness = Harness("um hello there")
    controller = harness.controller
    assert controller.state is DictationState.IDLE

    controller.toggle()
    await _settle()
    assert controller.state is DictationState.RECORDING
    harness.recordings[0].release_finish.clear()

    controller.toggle()
    await _settle()
    assert controller.state is DictationState.FINALIZING

    harness.recordings[0].release_finish.set()
    await controller.wait_idle()
    assert controller.state is DictationState.IDLE
    assert harness.delivered == ["hello there"]
    assert harness.recordings[0].closed
    assert any(message.startswith("pasted 11 characters") for message in harness.messages)
    assert not any("hello" in message for message in harness.messages)


async def test_toggle_while_finalizing_queues_one_recording() -> None:
    harness = Harness("first", "second")
    controller = harness.controller
    controller.toggle()
    await _settle()
    harness.recordings[0].release_finish.clear()
    controller.toggle()
    await _settle()

    controller.toggle()
    assert controller.state is DictationState.FINALIZING
    assert controller.queued
    assert "new recording starts after this paste" in harness.messages[-1]

    harness.recordings[0].release_finish.set()
    await _settle()
    await _settle()
    assert harness.delivered == ["first"]
    assert controller.state is DictationState.RECORDING
    assert len(harness.recordings) == 2

    controller.toggle()
    await controller.wait_idle()
    assert harness.delivered == ["first", "second"]


async def test_third_toggle_cancels_the_queued_recording_and_says_so() -> None:
    harness = Harness("only")
    controller = harness.controller
    controller.toggle()
    await _settle()
    harness.recordings[0].release_finish.clear()
    controller.toggle()
    await _settle()

    controller.toggle()
    controller.toggle()
    assert not controller.queued
    assert harness.messages[-1] == "queued recording cancelled"

    harness.recordings[0].release_finish.set()
    await controller.wait_idle()
    await _settle()
    assert controller.state is DictationState.IDLE
    assert len(harness.recordings) == 1


async def test_stop_during_start_still_finalizes() -> None:
    harness = Harness("quick")
    controller = harness.controller
    controller.toggle()
    controller.toggle()
    assert controller.state is DictationState.FINALIZING

    await controller.wait_idle()
    assert harness.delivered == ["quick"]


async def test_failed_start_returns_to_idle_with_a_message() -> None:
    harness = Harness()
    harness.pending.append(FakeRecording("", start_error="AWS Transcribe didn't connect"))
    controller = harness.controller

    controller.toggle()
    await controller.wait_idle()

    assert controller.state is DictationState.IDLE
    assert harness.messages == ["recording didn't start: AWS Transcribe didn't connect"]
    assert harness.recordings[0].closed


async def test_empty_transcript_is_not_pasted() -> None:
    harness = Harness("   ")
    controller = harness.controller
    controller.toggle()
    await _settle()
    controller.toggle()
    await controller.wait_idle()

    assert harness.delivered == []
    assert harness.messages[-1] == "no speech was recognized. nothing was pasted"


class FakeClipboard:
    def __init__(self, contents: object) -> None:
        self.contents = contents
        self.count = 1
        self.external_copy: object | None = None

    def snapshot(self) -> object:
        return self.contents

    def write_text(self, text: str) -> int:
        self.contents = text
        self.count += 1
        return self.count

    def change_count(self) -> int:
        return self.count

    def restore(self, snapshot: object) -> None:
        self.contents = snapshot
        self.count += 1


class FakeKeystroke:
    def __init__(self, *, permitted: bool = True, error: Exception | None = None) -> None:
        self._permitted = permitted
        self._error = error
        self.clipboard_at_paste: list[object] = []
        self.clipboard: FakeClipboard | None = None
        self.copy_during_paste: object | None = None

    def permitted(self) -> bool:
        return self._permitted

    def send_paste(self) -> None:
        if self._error is not None:
            raise self._error
        assert self.clipboard is not None
        self.clipboard_at_paste.append(self.clipboard.contents)
        if self.copy_during_paste is not None:
            self.clipboard.write_text(str(self.copy_during_paste))


async def test_paste_writes_sends_cmd_v_and_restores_previous_clipboard() -> None:
    clipboard = FakeClipboard({"public.png": b"image"})
    keystroke = FakeKeystroke()
    keystroke.clipboard = clipboard
    messages: list[str] = []

    outcome = await paste_text(
        "groomed text", clipboard, keystroke, notify=messages.append, settle_secs=0
    )

    assert outcome is PasteOutcome.PASTED
    assert keystroke.clipboard_at_paste == ["groomed text"]
    assert clipboard.contents == {"public.png": b"image"}
    assert messages == []


async def test_paste_not_permitted_leaves_text_on_clipboard() -> None:
    clipboard = FakeClipboard("previous")
    keystroke = FakeKeystroke(permitted=False)
    keystroke.clipboard = clipboard
    messages: list[str] = []

    outcome = await paste_text("groomed", clipboard, keystroke, notify=messages.append)

    assert outcome is PasteOutcome.CLIPBOARD
    assert clipboard.contents == "groomed"
    assert keystroke.clipboard_at_paste == []
    assert "Accessibility" in messages[0]
    assert "groomed" not in messages[0]


async def test_failed_keystroke_leaves_text_on_clipboard() -> None:
    clipboard = FakeClipboard("previous")
    keystroke = FakeKeystroke(error=RuntimeError("no event source"))

    messages: list[str] = []
    outcome = await paste_text("groomed", clipboard, keystroke, notify=messages.append)

    assert outcome is PasteOutcome.CLIPBOARD
    assert clipboard.contents == "groomed"
    assert messages == ["paste failed (RuntimeError). the transcript is on the clipboard"]


async def test_newer_copy_during_paste_is_not_overwritten() -> None:
    clipboard = FakeClipboard("previous")
    keystroke = FakeKeystroke()
    keystroke.clipboard = clipboard
    keystroke.copy_during_paste = "user copied this"
    messages: list[str] = []

    outcome = await paste_text(
        "groomed", clipboard, keystroke, notify=messages.append, settle_secs=0
    )

    assert outcome is PasteOutcome.PASTED
    assert clipboard.contents == "user copied this"
    assert "wasn't restored" in messages[0]


@pytest.mark.parametrize("new_copy", [None, "user copied this"])
async def test_cancel_during_paste_restores_only_unchanged_clipboard(new_copy: str | None) -> None:
    clipboard = FakeClipboard({"public.png": b"image"})
    keystroke = FakeKeystroke()
    keystroke.clipboard = clipboard
    messages: list[str] = []
    task = asyncio.create_task(
        paste_text("groomed", clipboard, keystroke, notify=messages.append, settle_secs=60)
    )
    await asyncio.sleep(0)
    assert keystroke.clipboard_at_paste == ["groomed"]
    if new_copy is not None:
        clipboard.write_text(new_copy)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert clipboard.contents == (new_copy if new_copy is not None else {"public.png": b"image"})


@pytest.mark.parametrize(
    ("value", "keycode", "modifiers", "label"),
    [
        ("ctrl+option+d", 2, CONTROL_MASK | OPTION_MASK, "ctrl+option+d"),
        ("Cmd + Shift + Space", 49, COMMAND_MASK | SHIFT_MASK, "shift+cmd+space"),
        ("alt+`", 50, OPTION_MASK, "option+`"),
        ("f19", 80, 0, "f19"),
    ],
)
def test_hotkeys_parse_to_macos_keycodes(
    value: str, keycode: int, modifiers: int, label: str
) -> None:
    hotkey = parse_hotkey(value)

    assert (hotkey.keycode, hotkey.modifiers, hotkey.label) == (keycode, modifiers, label)


@pytest.mark.parametrize(
    "value", ["d", "ctrl+option", "ctrl+d+e", "ctrl+ctrl+d", "ctrl++d", "hyper+d", "ctrl+f21"]
)
def test_invalid_hotkeys_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="hotkey"):
        parse_hotkey(value)


def test_hotkey_matches_exact_modifiers_and_ignores_caps_lock() -> None:
    hotkey = parse_hotkey("ctrl+option+d")
    caps_lock = 1 << 16

    assert hotkey.matches(2, CONTROL_MASK | OPTION_MASK | caps_lock)
    assert not hotkey.matches(2, CONTROL_MASK | OPTION_MASK | SHIFT_MASK)
    assert not hotkey.matches(2, CONTROL_MASK)
    assert not hotkey.matches(3, CONTROL_MASK | OPTION_MASK)


def _event(text: str, *, partial: bool) -> TranscriptEvent:
    return TranscriptEvent(
        Transcript(
            [
                Result(
                    is_partial=partial,
                    alternatives=[Alternative(transcript=text, items=[], entities=[])],
                )
            ]
        )
    )


class FakeTranscribeStream:
    """Partial on first audio. Finals only after end of stream, then the stream closes."""

    def __init__(self, finals: list[str], *, closes: bool = True) -> None:
        self.finals = finals
        self.closes = closes
        self.audio: list[bytes] = []
        self.audio_after_end = 0
        self.ended = False
        self._events: asyncio.Queue[TranscriptEvent | None] = asyncio.Queue()

    @property
    def input_stream(self) -> FakeTranscribeStream:
        return self

    @property
    def output_stream(self) -> AsyncIterator[TranscriptEvent]:
        return self._output()

    async def _output(self) -> AsyncIterator[TranscriptEvent]:
        while (event := await self._events.get()) is not None:
            yield event

    async def send_audio_event(self, audio_chunk: bytes | None) -> None:
        if self.ended:
            self.audio_after_end += 1
            return
        if not self.audio:
            self._events.put_nowait(_event("partial words", partial=True))
        self.audio.append(audio_chunk or b"")

    async def end_stream(self) -> None:
        self.ended = True
        for text in self.finals:
            self._events.put_nowait(_event(text, partial=False))
        if self.closes:
            self._events.put_nowait(None)


class FakeMicrophone:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.on_chunk: Callable[[bytes], None] | None = None
        self.stopped = False

    def start(self, on_chunk: Callable[[bytes], None]) -> None:
        if self.error is not None:
            raise self.error
        self.on_chunk = on_chunk

    def stop(self) -> None:
        self.stopped = True

    def emit(self, chunk: bytes) -> None:
        assert self.on_chunk is not None and not self.stopped
        self.on_chunk(chunk)


def _recording(
    stream: FakeTranscribeStream | Exception, microphone: FakeMicrophone
) -> TranscribeDictationRecording:
    async def open_stream(_settings: KassetteSettings) -> TranscribeStream:
        if isinstance(stream, Exception):
            raise stream
        return stream

    return TranscribeDictationRecording(
        KassetteSettings(KASSETTE_TRANSCRIPTION_PROVIDER="aws"),  # pyright: ignore[reportCallIssue]
        microphone=microphone,
        open_stream=open_stream,
    )


async def test_recording_drains_captured_audio_before_end_of_stream() -> None:
    stream = FakeTranscribeStream(["um so the jepp release", "ships on friday."])
    microphone = FakeMicrophone()
    recording = _recording(stream, microphone)
    await recording.start()
    try:
        microphone.emit(b"\x01\x00" * 1600)
        await _settle()
        # The audio thread scheduled this chunk, but its queue insertion is still pending.
        microphone.emit(b"\x02\x00" * 1600)
        text = await recording.finish()
    finally:
        await recording.close()

    assert text == "um so the jepp release ships on friday."
    assert microphone.stopped
    assert stream.audio == [b"\x01\x00" * 1600, b"\x02\x00" * 1600]
    assert stream.ended
    assert stream.audio_after_end == 0


async def test_recording_falls_back_to_the_partial_when_aws_never_closes() -> None:
    stream = FakeTranscribeStream([], closes=False)
    microphone = FakeMicrophone()
    recording = _recording(stream, microphone)
    recording._FINISH_TIMEOUT_SECS = 0.05  # pyright: ignore[reportPrivateUsage]
    await recording.start()
    try:
        microphone.emit(b"\x01\x00" * 1600)
        await _settle()
        text = await recording.finish()
    finally:
        await recording.close()

    assert text == "partial words"


async def test_rejected_stream_fails_start_with_the_aws_reason() -> None:
    recording = _recording(
        BadRequestException("The requested vocabulary filter couldn't be found."),
        FakeMicrophone(),
    )

    with pytest.raises(DictationError, match="BadRequestException: The requested vocabulary"):
        await recording.start()
    await recording.close()


async def test_microphone_failure_fails_start_and_close_ends_the_stream_tasks() -> None:
    stream = FakeTranscribeStream([])
    recording = _recording(stream, FakeMicrophone(error=OSError("PortAudio library not found")))

    with pytest.raises(DictationError, match=r"microphone unavailable \(OSError\)"):
        await recording.start()
    await asyncio.wait_for(recording.close(), 1)
