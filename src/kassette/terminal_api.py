"""Loopback API used by terminal voice clients."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from time import monotonic
from typing import Any, cast
from uuid import uuid4

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel

from kassette.terminal_protocol import (
    PROTOCOL_VERSION,
    REQUIRED_CAPABILITIES,
    ProtocolError,
    client_message,
    hello_message,
)

_TERMINAL_QUEUE_SIZE = 128


class TerminalQueueOverflow(RuntimeError):
    """A terminal client or service producer outran its bounded channel."""


class TerminalSessionRequest(BaseModel):
    """The protocol a terminal client is prepared to use."""

    protocol_version: int
    capabilities: list[str]


class TerminalSessionResponse(BaseModel):
    """One short-lived capability for attaching the control channel."""

    session_id: str
    token: str
    websocket_path: str


@dataclass
class TerminalSession:
    """One attached terminal client's bounded control and event queues."""

    session_id: str
    _incoming: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue[dict[str, Any]](maxsize=_TERMINAL_QUEUE_SIZE)
    )
    _outgoing: asyncio.Queue[dict[str, Any]] = field(
        default_factory=lambda: asyncio.Queue[dict[str, Any]](maxsize=_TERMINAL_QUEUE_SIZE)
    )
    closed: asyncio.Event = field(default_factory=asyncio.Event)

    async def receive(self) -> dict[str, Any]:
        return await self._incoming.get()

    async def send(self, message: dict[str, Any]) -> None:
        if self.closed.is_set():
            return
        try:
            self._outgoing.put_nowait(message)
        except asyncio.QueueFull as error:
            self.closed.set()
            raise TerminalQueueOverflow("terminal event queue is full") from error

    async def accept(self, message: dict[str, Any]) -> None:
        if self.closed.is_set():
            return
        try:
            self._incoming.put_nowait(message)
        except asyncio.QueueFull as error:
            self.closed.set()
            raise TerminalQueueOverflow("terminal control queue is full") from error

    async def next_event(self) -> dict[str, Any]:
        return await self._outgoing.get()


@dataclass
class _PendingSession:
    token: str
    session: TerminalSession
    expires_at: float


TerminalRunner = Callable[[TerminalSession], Awaitable[None]]


class TerminalSessionManager:
    """Negotiate and attach one-use terminal voice capabilities."""

    def __init__(self, runner: TerminalRunner) -> None:
        self._runner = runner
        self._pending: dict[str, _PendingSession] = {}

    def create(self, request: TerminalSessionRequest) -> TerminalSessionResponse:
        capabilities = set(request.capabilities)
        if request.protocol_version != PROTOCOL_VERSION:
            raise HTTPException(status_code=409, detail="incompatible terminal protocol")
        if not REQUIRED_CAPABILITIES.issubset(capabilities):
            raise HTTPException(
                status_code=409, detail="required terminal capabilities are missing"
            )
        session_id = str(uuid4())
        token = secrets.token_urlsafe(32)
        self._pending[session_id] = _PendingSession(
            token,
            TerminalSession(session_id),
            monotonic() + 30.0,
        )
        return TerminalSessionResponse(
            session_id=session_id,
            token=token,
            websocket_path=f"/api/terminal/sessions/{session_id}",
        )

    def attach(self, session_id: str, token: str) -> TerminalSession:
        pending = self._pending.pop(session_id, None)
        if (
            pending is None
            or pending.expires_at < monotonic()
            or not secrets.compare_digest(pending.token, token)
        ):
            if pending is not None and pending.expires_at >= monotonic():
                self._pending[session_id] = pending
            raise HTTPException(status_code=403, detail="invalid terminal session capability")
        return pending.session

    async def run(self, session: TerminalSession) -> None:
        try:
            await self._runner(session)
        except Exception as error:
            await session.send(
                {
                    "label": "kassette",
                    "type": "session.error",
                    "data": {"message": f"terminal voice failed: {type(error).__name__}"},
                }
            )


def create_terminal_router(manager: TerminalSessionManager) -> APIRouter:
    """Create the terminal session and control-channel routes."""
    router = APIRouter()

    @router.post(
        "/api/terminal/sessions",
        response_model=TerminalSessionResponse,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_session(request: TerminalSessionRequest) -> TerminalSessionResponse:
        return manager.create(request)

    @router.websocket("/api/terminal/sessions/{session_id}")
    async def terminal_socket(websocket: WebSocket, session_id: str) -> None:
        try:
            session = manager.attach(session_id, websocket.query_params.get("token", ""))
        except HTTPException:
            await websocket.close(code=1008, reason="invalid terminal session capability")
            return
        await websocket.accept()
        await websocket.send_json(hello_message(session_id))
        runner = asyncio.create_task(manager.run(session))
        sender = asyncio.create_task(_send_events(websocket, session))
        try:
            while True:
                raw = cast(object, await websocket.receive_json())
                await session.accept(client_message(raw))
        except (WebSocketDisconnect, ProtocolError, TerminalQueueOverflow):
            pass
        finally:
            session.closed.set()
            runner.cancel()
            sender.cancel()
            with suppress(asyncio.CancelledError):
                await runner
            with suppress(asyncio.CancelledError):
                await sender

    return router


async def _send_events(websocket: WebSocket, session: TerminalSession) -> None:
    while not session.closed.is_set():
        await websocket.send_json(await session.next_event())
