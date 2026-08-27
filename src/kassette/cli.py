"""kassette command-line entry points."""

from __future__ import annotations

import os
import sys
import webbrowser
from typing import Annotated

import typer

app = typer.Typer(no_args_is_help=True, help="Run and inspect the local kassette service.")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _loopback_url(host: str, port: int, path: str = "") -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}{path}"


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Loopback address to bind.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Local service port.")] = 7860,
) -> None:
    """Start the local kassette service."""
    if host not in _LOOPBACK_HOSTS:
        raise typer.BadParameter(
            "the first delivery only permits loopback addresses",
            param_hint="host",
        )
    origin = _loopback_url(host, port)
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
            origin,
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
