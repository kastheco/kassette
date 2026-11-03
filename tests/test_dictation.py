from __future__ import annotations

import asyncio

import pytest

from kassette.dictation import (
    DictationController,
    DictationError,
    DictationState,
    PasteOutcome,
    paste_text,
)
from kassette.hotkey import COMMAND_MASK, CONTROL_MASK, OPTION_MASK, SHIFT_MASK, parse_hotkey


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
