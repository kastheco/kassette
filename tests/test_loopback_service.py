from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.error import URLError
from urllib.request import urlopen

import aiohttp
from aiortc import RTCPeerConnection, RTCSessionDescription


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_client(process: subprocess.Popen[str], url: str) -> str:
    deadline = time.monotonic() + 10
    while True:
        try:
            with urlopen(url, timeout=1) as response:
                return response.read().decode()
        except URLError:
            if process.poll() is not None or time.monotonic() >= deadline:
                output = process.stdout.read() if process.stdout is not None else ""
                raise AssertionError(f"loopback service did not start: {output}") from None
            time.sleep(0.05)


def test_executable_loopback_service_serves_client() -> None:
    port = _free_port()
    command = [
        sys.executable,
        "-m",
        "kassette.bot",
        "-t",
        "webrtc",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--allowed-origins",
        f"http://127.0.0.1:{port}",
    ]
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        client_url = f"http://127.0.0.1:{port}/client/"
        html = _wait_for_client(process, client_url)
        assert process.poll() is None
        assert "pipecat" in html.lower()
        with urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=1) as response:
            openapi = response.read().decode()
        assert '"/api/offer"' in openapi

        process.terminate()
        process.wait(timeout=10)
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        assert "pipecat" in _wait_for_client(process, client_url).lower()
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


async def _connect_peer(
    base_url: str,
    *,
    offer_path: str = "/api/offer",
    authorization: str | None = None,
) -> RTCPeerConnection:
    peer = RTCPeerConnection()
    peer.addTransceiver("audio", direction="sendrecv")
    peer.createDataChannel("rtvi-ai")
    offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    assert peer.localDescription is not None
    headers = {"Authorization": authorization} if authorization is not None else None
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"{base_url}{offer_path}",
            json={"sdp": peer.localDescription.sdp, "type": peer.localDescription.type},
            headers=headers,
        ) as response:
            assert response.status == 200, await response.text()
            answer = cast(dict[str, Any], await response.json())
    await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    return peer


async def _state(
    base_url: str,
    *,
    authorization: str | None = None,
) -> dict[str, Any]:
    headers = {"Authorization": authorization} if authorization is not None else None
    async with aiohttp.ClientSession() as client:
        async with client.get(f"{base_url}/test/state", headers=headers) as response:
            response.raise_for_status()
            return cast(dict[str, Any], await response.json())


async def _wait_for_state(
    base_url: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    authorization: str | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = await _state(base_url, authorization=authorization)
        if predicate(state):
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(
        f"state condition was not reached: {await _state(base_url, authorization=authorization)}"
    )


async def _start_hosted_session(base_url: str, authorization: str) -> str:
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"{base_url}/start",
            json={"transport": "webrtc"},
            headers={"Authorization": authorization},
        ) as response:
            assert response.status == 200, await response.text()
            body = cast(dict[str, Any], await response.json())
    return cast(str, body["sessionId"])


async def test_actual_loopback_reconnects_with_fresh_session_and_reaps_prior() -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    fixture = Path(__file__).parent / "fixtures" / "synthetic_service.py"
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(fixture),
        "-t",
        "webrtc",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--allowed-origins",
        base_url,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    first: RTCPeerConnection | None = None
    second: RTCPeerConnection | None = None
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                await _state(base_url)
                break
            except (aiohttp.ClientError, TimeoutError):
                if process.returncode is not None or time.monotonic() >= deadline:
                    if process.returncode is None:
                        process.terminate()
                    await asyncio.wait_for(process.wait(), timeout=10)
                    output = (
                        (await process.stdout.read()).decode() if process.stdout is not None else ""
                    )
                    raise AssertionError(f"synthetic service did not start: {output}") from None
                await asyncio.sleep(0.05)

        first = await _connect_peer(base_url)
        first_state = await _wait_for_state(
            base_url,
            lambda state: (
                len(state["sessions"]) == 1 and state["sessions"][0]["state"] == "listening"
            ),
        )
        first_id = first_state["sessions"][0]["id"]
        assert first_state["audio_owner"] == first_id

        second = await _connect_peer(base_url)
        second_state = await _wait_for_state(
            base_url,
            lambda state: (
                len(state["opened"]) == 2
                and len(state["sessions"]) == 1
                and state["sessions"][0]["state"] == "listening"
            ),
        )
        second_id = second_state["sessions"][0]["id"]
        assert second_id != first_id
        assert second_state["closed"] == [first_id]
        assert second_state["audio_owner"] == second_id

        await second.close()
        second = None
        final_state = await _wait_for_state(
            base_url,
            lambda state: state["sessions"] == [] and state["audio_owner"] is None,
        )
        assert final_state["closed"] == [first_id, second_id]
        assert final_state["active"] is None
    finally:
        if first is not None:
            await first.close()
        if second is not None:
            await second.close()
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=10)


async def test_actual_hosted_sessions_run_concurrently_and_reconnect_independently() -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    fixture = Path(__file__).parent / "fixtures" / "synthetic_service.py"
    secret = "hosted-synthetic-secret-value-123456"
    authorization = f"Bearer {secret}"
    environment = os.environ.copy()
    environment.update(
        {
            "KASSETTE_RUNTIME_MODE": "hosted",
            "KASSETTE_SERVICE_SECRET": secret,
            "KASSETTE_ICE_SERVERS": (
                '[{"urls":"turns:relay.example:443","username":"tower","credential":"test-only"}]'
            ),
        }
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(fixture),
        "-t",
        "webrtc",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--allowed-origins",
        base_url,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    first_a: RTCPeerConnection | None = None
    second_a: RTCPeerConnection | None = None
    first_b: RTCPeerConnection | None = None
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                await _state(base_url, authorization=authorization)
                break
            except (aiohttp.ClientError, TimeoutError):
                if process.returncode is not None or time.monotonic() >= deadline:
                    output = (
                        (await process.stdout.read()).decode() if process.stdout is not None else ""
                    )
                    raise AssertionError(
                        f"synthetic hosted service did not start: {output}"
                    ) from None
                await asyncio.sleep(0.05)

        session_a = await _start_hosted_session(base_url, authorization)
        session_b = await _start_hosted_session(base_url, authorization)
        first_a = await _connect_peer(
            base_url,
            offer_path=f"/sessions/{session_a}/api/offer",
            authorization=authorization,
        )
        first_b = await _connect_peer(
            base_url,
            offer_path=f"/sessions/{session_b}/api/offer",
            authorization=authorization,
        )
        concurrent = await _wait_for_state(
            base_url,
            lambda state: (
                len(state["sessions"]) == 2
                and all(session["state"] == "listening" for session in state["sessions"])
            ),
            authorization=authorization,
        )
        generations = {session["id"]: session["generation"] for session in concurrent["sessions"]}
        assert set(concurrent["audio_owners"]) == {session_a, session_b}
        assert {handle["id"] for handle in concurrent["active_handles"]} == {
            session_a,
            session_b,
        }

        second_a = await _connect_peer(
            base_url,
            offer_path=f"/sessions/{session_a}/api/offer",
            authorization=authorization,
        )
        reconnected = await _wait_for_state(
            base_url,
            lambda state: len(state["opened"]) == 3 and len(state["sessions"]) == 2,
            authorization=authorization,
        )
        new_generations = {
            session["id"]: session["generation"] for session in reconnected["sessions"]
        }
        assert new_generations[session_a] > generations[session_a]
        assert new_generations[session_b] == generations[session_b]
        assert reconnected["closed"] == [session_a]
        assert set(reconnected["audio_owners"]) == {session_a, session_b}

        await second_a.close()
        second_a = None
        only_b = await _wait_for_state(
            base_url,
            lambda state: len(state["sessions"]) == 1 and state["sessions"][0]["id"] == session_b,
            authorization=authorization,
        )
        assert only_b["audio_owners"] == [session_b]
        assert only_b["sessions"][0]["generation"] == generations[session_b]
    finally:
        for peer in (first_a, second_a, first_b):
            if peer is not None:
                await peer.close()
        if process.returncode is None:
            process.terminate()
        await asyncio.wait_for(process.wait(), timeout=10)
