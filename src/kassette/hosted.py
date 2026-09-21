"""Hosted-runtime configuration, authentication, health, and shutdown hooks."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import SecretStr
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from kassette.settings import KassetteSettings

_MAX_ICE_CONFIG_CHARS = 16_384
_MAX_ICE_SERVERS = 8
_MAX_ICE_URL_CHARS = 2_048
_MAX_AUTHORIZATION_CHARS = 4_096
_SHUTDOWN_TIMEOUT_SECS = 10.0


class HostedConfigurationError(RuntimeError):
    """Hosted mode is missing a required, safe runtime setting."""


@dataclass(frozen=True, slots=True)
class HostedRuntimeConfiguration:
    """Validated hosted settings without printable secret fields."""

    service_secret: SecretStr
    ice_servers_json: SecretStr

    @classmethod
    def from_settings(cls, settings: KassetteSettings) -> HostedRuntimeConfiguration:
        if not settings.hosted:
            raise HostedConfigurationError("hosted runtime mode is not enabled")
        if settings.service_secret is None:
            raise HostedConfigurationError("KASSETTE_SERVICE_SECRET is required in hosted mode")
        if settings.ice_servers is None:
            raise HostedConfigurationError("KASSETTE_ICE_SERVERS is required in hosted mode")
        ice_servers = _parse_relay_configuration(settings.ice_servers.get_secret_value())
        return cls(
            service_secret=settings.service_secret,
            ice_servers_json=SecretStr(json.dumps(ice_servers, separators=(",", ":"))),
        )


def _parse_relay_configuration(raw: str) -> list[dict[str, object]]:
    if len(raw) > _MAX_ICE_CONFIG_CHARS:
        raise HostedConfigurationError("KASSETTE_ICE_SERVERS is too large")
    try:
        parsed = cast(object, json.loads(raw))
    except (json.JSONDecodeError, TypeError) as error:
        raise HostedConfigurationError("KASSETTE_ICE_SERVERS must be valid JSON") from error
    entries = cast(list[object], parsed) if isinstance(parsed, list) else [parsed]
    if not entries or len(entries) > _MAX_ICE_SERVERS:
        raise HostedConfigurationError(
            f"KASSETTE_ICE_SERVERS must contain 1-{_MAX_ICE_SERVERS} servers"
        )

    normalized: list[dict[str, object]] = []
    has_authenticated_relay = False
    for entry in entries:
        if not isinstance(entry, Mapping):
            raise HostedConfigurationError("hosted ICE servers must be JSON objects")
        server_entry = cast(Mapping[str, object], entry)
        urls_value = server_entry.get("urls")
        if isinstance(urls_value, str):
            typed_urls = [urls_value]
        elif isinstance(urls_value, list):
            candidate_urls = cast(list[object], urls_value)
            if not candidate_urls or not all(isinstance(url, str) for url in candidate_urls):
                raise HostedConfigurationError("each hosted ICE server requires one or more URLs")
            typed_urls = cast(list[str], candidate_urls)
        else:
            raise HostedConfigurationError("each hosted ICE server requires one or more URLs")
        if any(not url or len(url) > _MAX_ICE_URL_CHARS for url in typed_urls):
            raise HostedConfigurationError("hosted ICE server URL is invalid")

        username = server_entry.get("username")
        credential = server_entry.get("credential")
        relay_urls = [url for url in typed_urls if url.startswith(("turn:", "turns:"))]
        if relay_urls:
            if not isinstance(username, str) or not username:
                raise HostedConfigurationError("hosted TURN servers require a username")
            if not isinstance(credential, str) or not credential:
                raise HostedConfigurationError("hosted TURN servers require a credential")
            has_authenticated_relay = True

        server: dict[str, object] = {
            "urls": typed_urls[0] if len(typed_urls) == 1 else typed_urls,
        }
        if isinstance(username, str):
            server["username"] = username
        if isinstance(credential, str):
            server["credential"] = credential
        normalized.append(server)

    if not has_authenticated_relay:
        raise HostedConfigurationError(
            "KASSETTE_ICE_SERVERS must include an authenticated TURN relay"
        )
    return normalized


class HostedAuthenticationMiddleware:
    """Authenticate every hosted route except the bounded health probe."""

    def __init__(self, app: ASGIApp, *, service_secret: str) -> None:
        self._app = app
        self._service_secret = service_secret.encode()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"} or (
            scope["type"] == "http" and scope.get("path") == "/healthz"
        ):
            await self._app(scope, receive, send)
            return

        supplied = self._bearer_token(scope)
        authenticated = supplied is not None and secrets.compare_digest(
            supplied, self._service_secret
        )
        if authenticated:
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send(
                cast(
                    Message,
                    {"type": "websocket.close", "code": 4401, "reason": "unauthorized"},
                )
            )
            return
        response = JSONResponse(status_code=401, content={"detail": "unauthorized"})
        await response(scope, receive, send)

    @staticmethod
    def _bearer_token(scope: Scope) -> bytes | None:
        for name, value in scope.get("headers", []):
            if name.lower() != b"authorization" or len(value) > _MAX_AUTHORIZATION_CHARS:
                continue
            scheme, separator, token = value.partition(b" ")
            if separator and scheme.lower() == b"bearer" and token:
                return token
        return None


def install_hosted_runtime(
    app: FastAPI,
    configuration: HostedRuntimeConfiguration,
    close_sessions: Callable[[], Awaitable[None]],
) -> None:
    """Install hosted auth, health, sanitized validation, and bounded shutdown."""
    if getattr(app.state, "kassette_hosted_runtime", False):
        return
    app.state.kassette_hosted_runtime = True
    app.add_middleware(
        HostedAuthenticationMiddleware,
        service_secret=configuration.service_secret.get_secret_value(),
    )

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok", "mode": "hosted"}

    async def sanitized_validation_error(
        _request: Request,
        _error: Exception,
    ) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": "invalid request"})

    app.add_exception_handler(RequestValidationError, sanitized_validation_error)

    @asynccontextmanager
    async def hosted_lifespan(_app: FastAPI):
        yield
        try:
            await asyncio.wait_for(close_sessions(), timeout=_SHUTDOWN_TIMEOUT_SECS)
        except TimeoutError:
            return

    existing_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(lifespan_app: FastAPI):
        async with existing_lifespan(lifespan_app):
            async with hosted_lifespan(lifespan_app):
                yield

    app.router.lifespan_context = combined_lifespan
