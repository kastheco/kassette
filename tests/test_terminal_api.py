from __future__ import annotations

# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false
import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from kassette.terminal_api import (
    TerminalQueueOverflow,
    TerminalSession,
    TerminalSessionManager,
    create_terminal_router,
)
from kassette.terminal_protocol import PROTOCOL_VERSION, REQUIRED_CAPABILITIES


def app_with_runner(
    runner: Callable[[TerminalSession], Awaitable[None]],
) -> FastAPI:
    app = FastAPI()
    app.include_router(create_terminal_router(TerminalSessionManager(runner)))
    return app


def compatible_request() -> dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "capabilities": sorted(REQUIRED_CAPABILITIES),
    }


def test_terminal_session_negotiates_before_opening_control_channel() -> None:
    async def run(session: TerminalSession) -> None:
        message = await session.receive()
        await session.send({"label": "kassette", "type": "test.echo", "data": message["data"]})

    client = TestClient(app_with_runner(run))
    response = client.post("/api/terminal/sessions", json=compatible_request())

    assert response.status_code == 201
    created = response.json()
    assert created["session_id"]
    assert created["token"]
    with client.websocket_connect(
        f"/api/terminal/sessions/{created['session_id']}?token={created['token']}"
    ) as websocket:
        assert websocket.receive_json()["type"] == "terminal.hello"
        websocket.send_json({"label": "kassette", "type": "input.pause", "data": {}})
        assert websocket.receive_json() == {
            "label": "kassette",
            "type": "test.echo",
            "data": {},
        }


def test_terminal_session_rejects_missing_capability_before_audio_runner() -> None:
    ran = False

    async def run(_session: TerminalSession) -> None:
        nonlocal ran
        ran = True

    request = compatible_request()
    request["capabilities"] = ["audio.input"]
    response = TestClient(app_with_runner(run)).post("/api/terminal/sessions", json=request)

    assert response.status_code == 409
    assert not ran


async def test_terminal_session_closes_instead_of_growing_unbounded_control_queue() -> None:
    session = TerminalSession("bounded")
    message: dict[str, Any] = {"label": "kassette", "type": "input.pause", "data": {}}

    for _ in range(128):
        await session.accept(message)

    with pytest.raises(TerminalQueueOverflow):
        await session.accept(message)
    assert session.closed.is_set()

    event_session = TerminalSession("bounded-events")
    for _ in range(128):
        await event_session.send(message)
    with pytest.raises(TerminalQueueOverflow):
        await event_session.send(message)
    assert event_session.closed.is_set()


def test_terminal_session_rejects_wrong_capability_token() -> None:
    async def run(_session: TerminalSession) -> None:
        await asyncio.Future()

    client = TestClient(app_with_runner(run))
    created = client.post("/api/terminal/sessions", json=compatible_request()).json()

    try:
        with client.websocket_connect(
            f"/api/terminal/sessions/{created['session_id']}?token=wrong"
        ):
            raise AssertionError("connection should fail")
    except WebSocketDisconnect as error:
        assert error.code == 1008
