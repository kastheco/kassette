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


def test_serve_allows_additional_loopback_browser_origins(monkeypatch: MonkeyPatch) -> None:
    called_argv: list[str] = []

    def fake_execv(_path: str, argv: list[str]) -> None:
        called_argv.extend(argv)

    monkeypatch.setattr(os, "execv", fake_execv)
    result = runner.invoke(
        app,
        [
            "serve",
            "--client-origin",
            "http://127.0.0.1:5173",
            "--client-origin",
            "http://localhost:8080/",
        ],
    )

    assert result.exit_code == 0
    assert called_argv[-4:] == [
        "--allowed-origins",
        "http://127.0.0.1:7860",
        "http://127.0.0.1:5173",
        "http://localhost:8080",
    ]


def test_serve_rejects_non_loopback_host() -> None:
    result = runner.invoke(app, ["serve", "--host", "0.0.0.0"])

    assert result.exit_code == 2
    assert "only permits loopback" in result.output


def test_serve_rejects_non_loopback_client_origin() -> None:
    result = runner.invoke(app, ["serve", "--client-origin", "https://clickclack.example"])

    assert result.exit_code == 2
    assert "loopback origins without a path" in result.output


def test_call_opens_local_client(monkeypatch: MonkeyPatch) -> None:
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)
    result = runner.invoke(app, ["call"])

    assert result.exit_code == 0
    assert opened == ["http://127.0.0.1:7860/client"]


def test_ipv6_loopback_urls_are_bracketed(monkeypatch: MonkeyPatch) -> None:
    opened: list[str] = []

    def fake_open(url: str) -> bool:
        opened.append(url)
        return True

    monkeypatch.setattr(webbrowser, "open", fake_open)

    result = runner.invoke(app, ["call", "--host", "::1"])

    assert result.exit_code == 0
    assert opened == ["http://[::1]:7860/client"]
