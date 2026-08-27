from __future__ import annotations

import asyncio
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


def test_executable_loopback_service_serves_client() -> None:
    port = _free_port()
    process = subprocess.Popen(
        [
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
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urlopen(f"http://127.0.0.1:{port}/client/", timeout=1) as response:
                    html = response.read().decode()
                break
            except URLError:
                if process.poll() is not None or time.monotonic() >= deadline:
                    output = process.stdout.read() if process.stdout is not None else ""
                    raise AssertionError(f"loopback service did not start: {output}") from None
                time.sleep(0.05)

        assert process.poll() is None
        assert "pipecat" in html.lower()
        with urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=1) as response:
            openapi = response.read().decode()
        assert '"/api/offer"' in openapi
    finally:
        process.terminate()
        process.wait(timeout=10)


async def _connect_peer(base_url: str) -> RTCPeerConnection:
    peer = RTCPeerConnection()
    peer.addTransceiver("audio", direction="sendrecv")
    peer.createDataChannel("rtvi-ai")
    offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    assert peer.localDescription is not None
    async with aiohttp.ClientSession() as client:
        async with client.post(
            f"{base_url}/api/offer",
            json={"sdp": peer.localDescription.sdp, "type": peer.localDescription.type},
        ) as response:
            assert response.status == 200, await response.text()
            answer = cast(dict[str, Any], await response.json())
    await peer.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    return peer


async def _state(base_url: str) -> dict[str, Any]:
    async with aiohttp.ClientSession() as client:
        async with client.get(f"{base_url}/test/state") as response:
            response.raise_for_status()
            return cast(dict[str, Any], await response.json())


async def _wait_for_state(
    base_url: str, predicate: Callable[[dict[str, Any]], bool]
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        state = await _state(base_url)
        if predicate(state):
            return state
        await asyncio.sleep(0.05)
    raise AssertionError(f"state condition was not reached: {await _state(base_url)}")


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
