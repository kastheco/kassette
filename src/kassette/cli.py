"""kassette command-line entry points."""

from __future__ import annotations

import os
import sys
import webbrowser
from typing import Annotated
from urllib.parse import urlsplit

import typer
from pydantic import ValidationError

from kassette.hosted import HostedConfigurationError, HostedRuntimeConfiguration
from kassette.settings import load_settings

app = typer.Typer(no_args_is_help=True, help="Run and inspect the kassette voice service.")
_LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}


def _loopback_url(host: str, port: int, path: str = "") -> str:
    authority = f"[{host}]" if ":" in host else host
    return f"http://{authority}:{port}{path}"


def _client_origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise typer.BadParameter(
            "client origins must be absolute HTTP(S) origins without a path",
            param_hint="client-origin",
        )
    return f"{parsed.scheme}://{parsed.netloc}"


@app.command()
def serve(
    host: Annotated[
        str | None,
        typer.Option(help="Address to bind. Non-loopback addresses require --hosted."),
    ] = None,
    port: Annotated[
        int | None,
        typer.Option(help="Service port. Hosted mode defaults to PORT."),
    ] = None,
    client_origin: Annotated[
        list[str] | None,
        typer.Option("--client-origin", help="Additional exact browser origin to allow."),
    ] = None,
    hosted: Annotated[
        bool,
        typer.Option(help="Enable authenticated hosted runtime mode."),
    ] = False,
) -> None:
    """Start kassette in local mode or explicit hosted mode."""
    resolved_host = host or ("0.0.0.0" if hosted else "127.0.0.1")
    resolved_port = _service_port(port, hosted=hosted)
    if not hosted and resolved_host not in _LOOPBACK_HOSTS:
        raise typer.BadParameter(
            "local mode only permits loopback addresses",
            param_hint="host",
        )
    if hosted:
        configuration = _hosted_configuration()
        os.environ["KASSETTE_RUNTIME_MODE"] = "hosted"
        os.environ["PIPECAT_ICE_SERVERS"] = configuration.ice_servers_json.get_secret_value()
    else:
        os.environ["KASSETTE_RUNTIME_MODE"] = "local"
    origin = _loopback_url(resolved_host, resolved_port)
    allowed_origins = list(dict.fromkeys([origin, *map(_client_origin, client_origin or [])]))
    mode = "hosted" if hosted else "local"
    typer.echo(f"Starting kassette in {mode} mode on {origin}")
    os.execv(
        sys.executable,
        [
            sys.executable,
            "-m",
            "kassette.bot",
            "-t",
            "webrtc",
            "--host",
            resolved_host,
            "--port",
            str(resolved_port),
            "--allowed-origins",
            *allowed_origins,
        ],
    )


def _service_port(port: int | None, *, hosted: bool) -> int:
    if port is not None:
        resolved = port
    elif hosted:
        raw_port = os.getenv("PORT")
        if raw_port is None:
            raise typer.BadParameter("PORT is required in hosted mode", param_hint="port")
        try:
            resolved = int(raw_port)
        except ValueError as error:
            raise typer.BadParameter("PORT must be an integer", param_hint="port") from error
    else:
        resolved = 7860
    if not 1 <= resolved <= 65_535:
        raise typer.BadParameter("port must be between 1 and 65535", param_hint="port")
    return resolved


def _hosted_configuration() -> HostedRuntimeConfiguration:
    os.environ["KASSETTE_RUNTIME_MODE"] = "hosted"
    try:
        settings = load_settings()
        return HostedRuntimeConfiguration.from_settings(settings)
    except ValidationError as error:
        fields = {str(item["loc"][0]) for item in error.errors() if item.get("loc")}
        if "KASSETTE_SERVICE_SECRET" in fields or "service_secret" in fields:
            message = "KASSETTE_SERVICE_SECRET must contain at least 32 characters"
        else:
            message = "hosted configuration is invalid"
        raise typer.BadParameter(message, param_hint="hosted") from error
    except HostedConfigurationError as error:
        raise typer.BadParameter(str(error), param_hint="hosted") from error


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
