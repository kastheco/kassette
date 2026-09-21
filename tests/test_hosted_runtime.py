from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_SERVICE_SECRET = "hosted-service-secret-value-1234567890"
_TURN_CREDENTIAL = "turn-credential-must-not-appear-in-logs"


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _request(
    url: str,
    *,
    body: dict[str, object] | None = None,
    authorization: str | None = None,
) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if authorization is not None:
        headers["Authorization"] = authorization
    request = Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def _wait_for_health(process: subprocess.Popen[str], base_url: str) -> None:
    deadline = time.monotonic() + 15
    while True:
        try:
            status, body = _request(f"{base_url}/healthz")
            if status == 200 and json.loads(body) == {"status": "ok", "mode": "hosted"}:
                return
        except (URLError, TimeoutError):
            pass
        if process.poll() is not None or time.monotonic() >= deadline:
            output = process.stdout.read() if process.stdout is not None else ""
            raise AssertionError(f"hosted service did not start: {output}")
        time.sleep(0.05)


def test_executable_hosted_runtime_auth_health_turn_and_port() -> None:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    executable = Path(sys.executable).parent / "kassette"
    environment = os.environ.copy()
    environment.update(
        {
            "PORT": str(port),
            "KASSETTE_SERVICE_SECRET": _SERVICE_SECRET,
            "KASSETTE_ICE_SERVERS": json.dumps(
                [
                    {
                        "urls": "turns:relay.example:443?transport=tcp",
                        "username": "tower",
                        "credential": _TURN_CREDENTIAL,
                    }
                ]
            ),
        }
    )
    process = subprocess.Popen(
        [str(executable), "serve", "--hosted"],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    output = ""
    try:
        _wait_for_health(process, base_url)

        missing_status, missing_body = _request(
            f"{base_url}/start",
            body={"transport": "webrtc"},
        )
        wrong_status, wrong_body = _request(
            f"{base_url}/start",
            body={"transport": "webrtc"},
            authorization="Bearer incorrect",
        )
        offer_status, offer_body = _request(
            f"{base_url}/api/offer",
            body={},
        )
        accepted_status, accepted_body = _request(
            f"{base_url}/start",
            body={"transport": "webrtc"},
            authorization=f"Bearer {_SERVICE_SECRET}",
        )

        assert (missing_status, missing_body) == (401, b'{"detail":"unauthorized"}')
        assert (wrong_status, wrong_body) == (401, b'{"detail":"unauthorized"}')
        assert (offer_status, offer_body) == (401, b'{"detail":"unauthorized"}')
        assert accepted_status == 200
        response = cast(dict[str, Any], json.loads(accepted_body))
        assert response["sessionId"]
        assert response["iceConfig"] == {
            "iceServers": [
                {
                    "urls": "turns:relay.example:443?transport=tcp",
                    "username": "tower",
                    "credential": _TURN_CREDENTIAL,
                }
            ]
        }
        assert process.poll() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=15)
        output = process.stdout.read() if process.stdout is not None else ""

    assert _SERVICE_SECRET not in output
    assert _TURN_CREDENTIAL not in output
    assert f"0.0.0.0:{port}" in output
