from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from kassette.hotkey import COMMAND_MASK, parse_hotkey


@pytest.fixture
def macos(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    quartz = Mock()
    quartz.kCGEventKeyDown = 10
    quartz.kCGEventKeyUp = 11
    quartz.kCGEventTapDisabledByTimeout = 12
    quartz.kCGEventTapDisabledByUserInput = 13
    quartz.kCGKeyboardEventKeycode = "keycode"
    quartz.kCGKeyboardEventAutorepeat = "repeat"
    quartz.kCGEventSourceUserData = "user_data"

    def keyboard_event(_source: object, keycode: int, down: bool) -> dict[str, int]:
        return {"keycode": keycode, "down": down, "repeat": 0, "user_data": 0}

    def set_flags(event: dict[str, int], flags: int) -> None:
        event["flags"] = flags

    def set_field(event: dict[str, int], field: str, value: int) -> None:
        event[field] = value

    def get_field(event: dict[str, int], field: str) -> int:
        return event[field]

    def get_flags(event: dict[str, int]) -> int:
        return event["flags"]

    quartz.CGEventCreateKeyboardEvent.side_effect = keyboard_event
    quartz.CGEventSetFlags.side_effect = set_flags
    quartz.CGEventSetIntegerValueField.side_effect = set_field
    quartz.CGEventGetIntegerValueField.side_effect = get_field
    quartz.CGEventGetFlags.side_effect = get_flags
    for name in ("AppKit", "ApplicationServices", "AVFoundation"):
        monkeypatch.setitem(sys.modules, name, Mock())
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    spec = importlib.util.find_spec("kassette.macos")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_own_paste_bypasses_cmd_v_hotkey_without_releasing_physical_key(macos: ModuleType) -> None:
    presses: list[bool] = []
    listener = macos.MacHotkeyListener(parse_hotkey("cmd+v"), lambda: presses.append(True))
    quartz = macos.Quartz
    physical = {"keycode": 9, "repeat": 0, "user_data": 0, "flags": COMMAND_MASK}
    assert listener._handle(None, quartz.kCGEventKeyDown, physical, None) is None
    assert presses == [True]

    passed: list[object] = []

    def post(_tap: object, event: dict[str, object]) -> None:
        kind = quartz.kCGEventKeyDown if event["down"] else quartz.kCGEventKeyUp
        assert listener._handle(None, kind, event, None) is event
        passed.append(event)

    quartz.CGEventPost.side_effect = post
    macos.MacPasteKeystroke().send_paste()
    assert presses == [True]
    assert len(passed) == 2
    assert listener._handle(None, quartz.kCGEventKeyUp, physical, None) is None
    assert listener._handle(None, quartz.kCGEventKeyDown, physical, None) is None
    assert presses == [True, True]
