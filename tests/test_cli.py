import os
import webbrowser

from pytest import MonkeyPatch
from typer.testing import CliRunner

from kassette.cli import app

runner = CliRunner()


def test_serve_defaults_to_loopback(monkeypatch: MonkeyPatch) -> None:
    called_argv: list[str] = []

    def fake_execv(_path: str, argv: list[str]) -> None:
        called_argv.extend(argv)

    monkeypatch.setattr(os, "execv", fake_execv)
    result = runner.invoke(app, ["serve"])

    assert result.exit_code == 0
    assert "http://127.0.0.1:7860" in result.stdout
    assert called_argv[-4:] == [
        "--port",
        "7860",
        "--allowed-origins",
        "http://127.0.0.1:7860",
    ]


def test_serve_rejects_non_loopback_host() -> None:
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "only permits loopback" in result.output


def test_call_opens_local_client(monkeypatch: MonkeyPatch) -> None:
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    result = runner.invoke(app, ["call"])

    assert result.exit_code == 0
    assert opened == ["http://127.0.0.1:7860/client"]
