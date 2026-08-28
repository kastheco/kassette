"""kassette command-line entry points."""

from __future__ import annotations

import os
import sys
import webbrowser
from typing import Annotated
from urllib.parse import urlsplit

import typer

app = typer.Typer(no_args_is_help=True, help="Run and inspect the local kassette service.")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _loopback_url(host: str, port: int, path: str = "") -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}{path}"


def _loopback_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.hostname not in _LOOPBACK_HOSTS
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise typer.BadParameter(
            "client origins must be absolute HTTP(S) loopback origins without a path",
            param_hint="client-origin",
        )
    return f"{parsed.scheme}://{parsed.netloc}"


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Loopback address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Local service port.")] = 7860,
    client_origin: Annotated[
        list[str] | None,
        typer.Option("--client-origin", help="Additional loopback browser origin to allow."),
    ] = None,
) -> None:
    """Start the local kassette service."""
    if host not in _LOOPBACK_HOSTS:
        raise typer.BadParameter(
            "the first delivery only permits loopback addresses",
            param_hint="host",
        )
    origin = _loopback_url(host, port)
    allowed_origins = list(dict.fromkeys([origin, *map(_loopback_origin, client_origin or [])]))
    typer.echo(f"Starting kassette on {origin}")
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "kassette.bot",
            "-t",
            "webrtc",
            "--host",
            host,
            "--port",
            str(port),
            "--allowed-origins",
            *allowed_origins,
        ],
    )


@app.command()
def call(
    host: Annotated[str, typer.Option(help="kassette service host.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="kassette service port.")] = 7860,
) -> None:
    """Open the disposable local SmallWebRTC voice client."""
    if host not in _LOOPBACK_HOSTS:
        raise typer.BadParameter(
            "the first delivery only permits loopback addresses",
            param_hint="host",
        )
    url = _loopback_url(host, port, "/client")
    typer.echo(f"Opening kassette voice client at {url}")
    if not webbrowser.open(url):
        raise typer.Exit(code=1)
