"""macOS adapters for dictation: permissions, global hotkey, pasteboard, and Cmd-V.

Imported only on macOS. PyObjC ships without type information, so this module
opts out of strict checking.
"""

# pyright: basic, reportMissingImports=false, reportMissingModuleSource=false, reportAttributeAccessIssue=false

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

import AppKit
import ApplicationServices
import AVFoundation
import Quartz

from kassette.dictation import DictationError
from kassette.hotkey import COMMAND_MASK, Hotkey

_V_KEYCODE = 9
_PASTE_EVENT_TAG = 0x4B41535345545445  # "KASSETTE" in the event's user data.
_MIC_PROMPT_TIMEOUT_SECS = 120.0
_HOTKEY_START_TIMEOUT_SECS = 10.0
_PERMISSION_PATH = "System Settings → Privacy & Security"


class PermissionDeniedError(DictationError):
    """A macOS privacy permission is missing."""


def require_microphone() -> None:
    """Check microphone access, prompting once when macOS hasn't asked yet."""
    media = AVFoundation.AVMediaTypeAudio
    status = AVFoundation.AVCaptureDevice.authorizationStatusForMediaType_(media)
    if status == AVFoundation.AVAuthorizationStatusAuthorized:
        return
    if status == AVFoundation.AVAuthorizationStatusNotDetermined:
        answered = threading.Event()
        granted: list[bool] = []

        def handler(allowed: bool) -> None:
            granted.append(bool(allowed))
            answered.set()

        AVFoundation.AVCaptureDevice.requestAccessForMediaType_completionHandler_(media, handler)
        if answered.wait(_MIC_PROMPT_TIMEOUT_SECS) and granted and granted[0]:
            return
    raise PermissionDeniedError(
        "microphone access is denied. allow this terminal under "
        f"{_PERMISSION_PATH} → Microphone, then run kassette dictate again"
    )


def require_accessibility() -> None:
    """Check Accessibility access, showing the system prompt when it's missing."""
    options = {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
    if ApplicationServices.AXIsProcessTrustedWithOptions(options):
        return
    raise PermissionDeniedError(
        "Accessibility access is denied. allow this terminal under "
        f"{_PERMISSION_PATH} → Accessibility, then run kassette dictate again"
    )


class MacClipboard:
    """General pasteboard with full multi-type snapshot and restore."""

    def __init__(self) -> None:
        self._pasteboard = AppKit.NSPasteboard.generalPasteboard()

    def snapshot(self) -> object:
        items: list[list[tuple[Any, Any]]] = []
        for item in self._pasteboard.pasteboardItems() or []:
            entries = []
            for kind in item.types():
                data = item.dataForType_(kind)
                if data is not None:
                    entries.append((kind, data))
            items.append(entries)
        return items

    def write_text(self, text: str) -> int:
        self._pasteboard.clearContents()
        self._pasteboard.setString_forType_(text, AppKit.NSPasteboardTypeString)
        return int(self._pasteboard.changeCount())

    def change_count(self) -> int:
        return int(self._pasteboard.changeCount())

    def restore(self, snapshot: object) -> None:
        items: list[list[tuple[Any, Any]]] = snapshot  # type: ignore[assignment]
        self._pasteboard.clearContents()
        restored = []
        for entries in items:
            item = AppKit.NSPasteboardItem.alloc().init()
            for kind, data in entries:
                item.setData_forType_(data, kind)
            restored.append(item)
        if restored:
            self._pasteboard.writeObjects_(restored)


class MacPasteKeystroke:
    """Post Cmd-V to the focused app through Quartz events."""

    def permitted(self) -> bool:
        return bool(ApplicationServices.AXIsProcessTrusted())

    def send_paste(self) -> None:
        source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        for key_down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(source, _V_KEYCODE, key_down)
            if event is None:
                raise RuntimeError("keyboard event could not be created")
            # Exact flags, so modifiers still held from the hotkey don't leak in.
            Quartz.CGEventSetFlags(event, COMMAND_MASK)
            Quartz.CGEventSetIntegerValueField(
                event, Quartz.kCGEventSourceUserData, _PASTE_EVENT_TAG
            )
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, event)


class MacHotkeyListener:
    """Active Quartz event tap on its own run loop thread. Swallows the hotkey."""

    def __init__(self, hotkey: Hotkey, on_press: Callable[[], None]) -> None:
        self._hotkey = hotkey
        self._on_press = on_press
        self._held = False
        self._tap: Any = None
        self._run_loop: Any = None
        self._ready = threading.Event()
        self._failed = False
        self._thread = threading.Thread(target=self._run, name="kassette-hotkey", daemon=True)

    def start(self) -> None:
        self._thread.start()
        if not self._ready.wait(_HOTKEY_START_TIMEOUT_SECS) or self._failed:
            raise PermissionDeniedError(
                "the global hotkey can't be installed. allow this terminal under "
                f"{_PERMISSION_PATH} → Accessibility (and Input Monitoring if macOS "
                "lists it), then run kassette dictate again"
            )

    def stop(self) -> None:
        if self._run_loop is not None:
            Quartz.CFRunLoopStop(self._run_loop)
        self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            self._install_tap_and_run()
        finally:
            if not self._ready.is_set():
                self._failed = True
                self._ready.set()

    def _install_tap_and_run(self) -> None:
        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(
            Quartz.kCGEventKeyUp
        )
        self._tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionDefault,
            mask,
            self._handle,
            None,
        )
        if self._tap is None:
            self._failed = True
            self._ready.set()
            return
        source = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
        self._run_loop = Quartz.CFRunLoopGetCurrent()
        Quartz.CFRunLoopAddSource(self._run_loop, source, Quartz.kCFRunLoopCommonModes)
        Quartz.CGEventTapEnable(self._tap, True)
        self._ready.set()
        Quartz.CFRunLoopRun()

    def _handle(self, _proxy: Any, event_type: int, event: Any, _refcon: Any) -> Any:
        if event_type in (
            Quartz.kCGEventTapDisabledByTimeout,
            Quartz.kCGEventTapDisabledByUserInput,
        ):
            Quartz.CGEventTapEnable(self._tap, True)
            return event
        if (
            Quartz.CGEventGetIntegerValueField(event, Quartz.kCGEventSourceUserData)
            == _PASTE_EVENT_TAG
        ):
            return event
        keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
        if event_type == Quartz.kCGEventKeyUp:
            if self._held and keycode == self._hotkey.keycode:
                self._held = False
                return None
            return event
        if event_type != Quartz.kCGEventKeyDown:
            return event
        if not self._hotkey.matches(keycode, Quartz.CGEventGetFlags(event)):
            return event
        repeat = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
        if not repeat:
            self._held = True
            try:
                self._on_press()
            except RuntimeError:
                # The asyncio loop is already closed during shutdown.
                pass
        return None


class MacDictationPlatform:
    def __init__(self) -> None:
        self._clipboard = MacClipboard()
        self._keystroke = MacPasteKeystroke()

    @property
    def clipboard(self) -> MacClipboard:
        return self._clipboard

    @property
    def keystroke(self) -> MacPasteKeystroke:
        return self._keystroke

    def listen(self, hotkey: Hotkey, on_press: Callable[[], None]) -> Callable[[], None]:
        listener = MacHotkeyListener(hotkey, on_press)
        listener.start()
        return listener.stop
