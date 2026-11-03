"""Parse and match the global dictation hotkey against macOS key events."""

from __future__ import annotations

from dataclasses import dataclass

# Quartz CGEventFlags masks. The values are fixed by the macOS SDK.
SHIFT_MASK = 1 << 17
CONTROL_MASK = 1 << 18
OPTION_MASK = 1 << 19
COMMAND_MASK = 1 << 20
MODIFIER_MASK = SHIFT_MASK | CONTROL_MASK | OPTION_MASK | COMMAND_MASK

_MODIFIERS: dict[str, int] = {
    "shift": SHIFT_MASK,
    "ctrl": CONTROL_MASK,
    "control": CONTROL_MASK,
    "option": OPTION_MASK,
    "opt": OPTION_MASK,
    "alt": OPTION_MASK,
    "cmd": COMMAND_MASK,
    "command": COMMAND_MASK,
}

_MODIFIER_LABELS = (
    (CONTROL_MASK, "ctrl"),
    (OPTION_MASK, "option"),
    (SHIFT_MASK, "shift"),
    (COMMAND_MASK, "cmd"),
)

# macOS virtual key codes (kVK_*) for the US ANSI layout. Codes name physical keys.
_FUNCTION_KEYS: dict[str, int] = {
    "f1": 122,
    "f2": 120,
    "f3": 99,
    "f4": 118,
    "f5": 96,
    "f6": 97,
    "f7": 98,
    "f8": 100,
    "f9": 101,
    "f10": 109,
    "f11": 103,
    "f12": 111,
    "f13": 105,
    "f14": 107,
    "f15": 113,
    "f16": 106,
    "f17": 64,
    "f18": 79,
    "f19": 80,
    "f20": 90,
}

_KEYS: dict[str, int] = {
    "a": 0,
    "s": 1,
    "d": 2,
    "f": 3,
    "h": 4,
    "g": 5,
    "z": 6,
    "x": 7,
    "c": 8,
    "v": 9,
    "b": 11,
    "q": 12,
    "w": 13,
    "e": 14,
    "r": 15,
    "y": 16,
    "t": 17,
    "1": 18,
    "2": 19,
    "3": 20,
    "4": 21,
    "6": 22,
    "5": 23,
    "=": 24,
    "9": 25,
    "7": 26,
    "-": 27,
    "8": 28,
    "0": 29,
    "]": 30,
    "o": 31,
    "u": 32,
    "[": 33,
    "i": 34,
    "p": 35,
    "return": 36,
    "enter": 36,
    "l": 37,
    "j": 38,
    "'": 39,
    "k": 40,
    ";": 41,
    "\\": 42,
    ",": 43,
    "/": 44,
    "n": 45,
    "m": 46,
    ".": 47,
    "tab": 48,
    "space": 49,
    "`": 50,
    "escape": 53,
    "esc": 53,
    **_FUNCTION_KEYS,
}

_FUNCTION_KEY_CODES = frozenset(_FUNCTION_KEYS.values())


@dataclass(frozen=True, slots=True)
class Hotkey:
    """One physical key plus an exact modifier set."""

    keycode: int
    modifiers: int
    key: str

    def matches(self, keycode: int, flags: int) -> bool:
        """Match a key event exactly, ignoring caps lock, fn, and keypad state."""
        return keycode == self.keycode and (flags & MODIFIER_MASK) == self.modifiers

    @property
    def label(self) -> str:
        names = [name for mask, name in _MODIFIER_LABELS if self.modifiers & mask]
        return "+".join([*names, self.key])


def parse_hotkey(value: str) -> Hotkey:
    """Parse `ctrl+option+d` style hotkeys. Raises ValueError with a usable message."""
    parts = [part.strip().casefold() for part in value.split("+")]
    if any(not part for part in parts):
        raise ValueError(f"hotkey {value!r} has an empty key name")
    modifiers = 0
    keys: list[str] = []
    for part in parts:
        mask = _MODIFIERS.get(part)
        if mask is None:
            keys.append(part)
        elif modifiers & mask:
            raise ValueError(f"hotkey {value!r} repeats a modifier")
        else:
            modifiers |= mask
    if len(keys) != 1:
        raise ValueError(f"hotkey {value!r} must name exactly one non-modifier key")
    key = keys[0]
    keycode = _KEYS.get(key)
    if keycode is None:
        raise ValueError(f"hotkey {value!r} uses an unsupported key {key!r}")
    if not modifiers and keycode not in _FUNCTION_KEY_CODES:
        raise ValueError(f"hotkey {value!r} needs a modifier; only function keys may be used alone")
    return Hotkey(keycode=keycode, modifiers=modifiers, key=key)
