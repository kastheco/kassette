import importlib.util
import sys
from importlib.machinery import ModuleSpec

import pytest

from kassette.dictate_cli import main


@pytest.mark.parametrize(
    "missing",
    [
        ("amazon_transcribe",),
        ("sounddevice",),
        ("AppKit",),
        ("ApplicationServices",),
        ("AVFoundation",),
        ("Quartz",),
        ("amazon_transcribe", "sounddevice", "AppKit"),
    ],
)
def test_dictate_reports_missing_extra_before_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], missing: tuple[str, ...]
) -> None:
    def find_spec(name: str) -> ModuleSpec | None:
        return None if name in missing else ModuleSpec(name, loader=None)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setenv("KASSETTE_TRANSCRIPTION_PROVIDER", "invalid-provider")

    with pytest.raises(SystemExit) as error:
        main([])

    assert error.value.code == 1
    message = capsys.readouterr().err
    assert "kassette[dictate]" in message
    assert f"missing: {', '.join(missing)}" in message
    assert "invalid configuration" not in message


def test_dictate_rejects_other_platforms_without_requiring_macos_modules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def find_spec(_name: str) -> None:
        return None

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(importlib.util, "find_spec", find_spec)

    with pytest.raises(SystemExit) as error:
        main([])

    assert error.value.code == 1
    assert capsys.readouterr().err == "kassette dictate: kassette dictate requires macOS\n"
