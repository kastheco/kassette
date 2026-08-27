"""Executable loopback fixture with no credentials or external provider traffic."""

from __future__ import annotations

from typing import Any

from pipecat.runner.run import app
from pipecat.runner.types import RunnerArguments, SmallWebRTCRunnerArguments
from pipecat.runner.utils import create_transport  # pyright: ignore[reportUnknownVariableType]
from pipecat.transports.base_transport import TransportParams

from kassette.bot import run_session
from kassette.credentials import CodexCredentials
from kassette.domain import AudioChunk
from kassette.providers.quicksilver.protocol import ProviderEvent
from kassette.sessions import LiveSessionCoordinator, SessionRegistry

registry = SessionRegistry()
lifecycle = LiveSessionCoordinator()
opened: list[str] = []
closed: list[str] = []


class SyntheticCredentials:
    async def load(self) -> CodexCredentials:
        raise AssertionError("synthetic provider must not load credentials")


class SyntheticProvider:
    def __init__(self, *, session_id: str, event_sink: Any, **_kwargs: Any) -> None:
        self._session_id = session_id
        self._event_sink = event_sink
        self._closed = False

    async def open(self) -> None:
        opened.append(self._session_id)
        await self._event_sink(
            ProviderEvent(type="session.started", session_id=f"synthetic-{len(opened)}")
        )

    async def send_audio(self, chunk: AudioChunk) -> None:
        del chunk
        return

    async def send(self, message: dict[str, Any]) -> None:
        del message
        return

    async def interrupt(self) -> None:
        return

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        closed.append(self._session_id)


@app.get("/test/state")
async def test_state() -> dict[str, object]:
    sessions = await registry.list()
    active = await lifecycle.active()
    return {
        "sessions": [
            {"id": session.id, "state": session.state.value, "generation": session.generation}
            for session in sessions
        ],
        "audio_owner": await registry.audio_owner(),
        "active": active.id if active is not None else None,
        "opened": opened,
        "closed": closed,
    }


async def bot(runner_args: RunnerArguments) -> None:
    if not isinstance(runner_args, SmallWebRTCRunnerArguments):
        raise RuntimeError("synthetic fixture only supports SmallWebRTC")
    transport = await create_transport(
        runner_args,
        {
            "webrtc": lambda: TransportParams(
                audio_in_enabled=True,
                audio_out_enabled=True,
                audio_in_sample_rate=16_000,
                audio_out_sample_rate=24_000,
            )
        },
    )
    await run_session(
        transport,
        runner_args,
        registry=registry,
        lifecycle=lifecycle,
        credential_provider=SyntheticCredentials(),
        provider_transport_factory=SyntheticProvider,
    )


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
