"""Python WebRTC transport for the experimental Quicksilver protocol."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from fractions import Fraction
from typing import Any
from uuid import uuid4

import aiohttp
from aiortc import (
    MediaStreamTrack,
    RTCDataChannel,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.mediastreams import MediaStreamError
from av import AudioFrame
from av.audio.resampler import AudioResampler

from kassette.credentials import CodexCredentialProvider
from kassette.domain import AudioChunk
from kassette.providers.quicksilver.protocol import (
    SIGNALING_URL,
    LiveVoice,
    ProviderEvent,
    build_live_headers,
    build_session_close,
    build_session_payload,
    parse_call_id,
    parse_provider_event,
    sideband_url,
)

ProviderEventSink = Callable[[ProviderEvent], Awaitable[None]]
ProviderAudioSink = Callable[[AudioChunk], Awaitable[None]]


class QuicksilverTransportError(RuntimeError):
    code = "quicksilver_transport_error"


def _quicksilver_offer_sdp(sdp: str) -> str:
    """Match the single-Opus payload shape accepted by the Codex voice endpoint."""
    lines: list[str] = []
    for line in sdp.splitlines():
        if line.startswith("m=audio "):
            parts = line.split()
            line = " ".join([*parts[:3], "111"])
        elif line.startswith(("a=rtpmap:96 ", "a=fmtp:96 ", "a=rtcp-fb:96 ")):
            line = line.replace(":96 ", ":111 ", 1)
        lines.append(line)
    return "\r\n".join(lines) + "\r\n"


class _InputAudioTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[AudioChunk | None] = asyncio.Queue(maxsize=128)
        self._pts = 0

    async def recv(self) -> AudioFrame:
        chunk = await self._queue.get()
        if chunk is None:
            raise MediaStreamError
        if chunk.sample_rate != 16_000 or chunk.num_channels != 1:
            raise QuicksilverTransportError(
                "Quicksilver input requires 16 kHz mono signed 16-bit PCM"
            )
        samples = len(chunk.audio) // 2
        frame = AudioFrame(format="s16", layout="mono", samples=samples)
        frame.planes[0].update(chunk.audio)
        frame.sample_rate = chunk.sample_rate
        frame.pts = self._pts
        frame.time_base = Fraction(1, chunk.sample_rate)
        self._pts += samples
        return frame

    def write(self, chunk: AudioChunk) -> None:
        if self.readyState == "ended":
            return
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull as error:
            raise QuicksilverTransportError("Quicksilver input audio queue is full") from error

    async def close(self) -> None:
        if self.readyState == "ended":
            return
        self.stop()
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(None)


class QuicksilverTransport:
    """Own provider signaling, media, and sideband events for one session."""

    def __init__(
        self,
        *,
        session_id: str,
        credentials: CodexCredentialProvider,
        instructions: str,
        voice: LiveVoice,
        event_sink: ProviderEventSink,
        audio_sink: ProviderAudioSink,
    ) -> None:
        self._session_id = session_id
        self._credentials = credentials
        self._instructions = instructions
        self._voice: LiveVoice = voice
        self._event_sink = event_sink
        self._audio_sink = audio_sink
        self._realtime_session_id = str(uuid4())
        self._peer: RTCPeerConnection | None = None
        self._input_track: _InputAudioTrack | None = None
        self._http: aiohttp.ClientSession | None = None
        self._sideband: aiohttp.ClientWebSocketResponse | None = None
        self._sideband_task: asyncio.Task[None] | None = None
        self._remote_audio_tasks: set[asyncio.Task[None]] = set()
        self._event_tasks: set[asyncio.Future[None]] = set()
        self._connected = False
        self._closed = False

    async def open(self) -> None:
        if self._closed:
            raise QuicksilverTransportError("Quicksilver transport is closed")
        if self._connected:
            return
        credentials = await self._credentials.load()
        headers = build_live_headers(credentials, self._session_id, self._realtime_session_id)
        peer = RTCPeerConnection()
        input_track = _InputAudioTrack()
        peer.addTrack(input_track)
        audio_transceiver = peer.getTransceivers()[0]
        opus = [
            codec
            for codec in RTCRtpSender.getCapabilities("audio").codecs
            if codec.mimeType.lower() == "audio/opus"
        ]
        audio_transceiver.setCodecPreferences(opus)
        channel = peer.createDataChannel("oai-events")
        self._peer = peer
        self._input_track = input_track
        self._install_peer_handlers(peer, channel)

        http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        self._http = http
        try:
            raw_offer = await peer.createOffer()
            offer = RTCSessionDescription(
                sdp=_quicksilver_offer_sdp(raw_offer.sdp),
                type=raw_offer.type,
            )
            async with http.post(
                SIGNALING_URL,
                headers={**headers, "Accept": "*/*", "Content-Type": "application/json"},
                json={
                    "sdp": offer.sdp,
                    "session": build_session_payload(self._instructions, self._voice),
                },
            ) as response:
                answer = await response.text()
                if not response.ok:
                    detail = " ".join(answer.strip().split())[:2_048]
                    suffix = f": {detail}" if detail else ""
                    raise QuicksilverTransportError(
                        f"Quicksilver signaling failed with HTTP {response.status}{suffix}"
                    )
                call_id = parse_call_id(response.headers.get("location"))
                if not call_id:
                    raise QuicksilverTransportError("Quicksilver signaling returned no call ID")
            await peer.setLocalDescription(offer)
            await peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            await self._wait_for_peer(peer)
            await self._connect_sideband(call_id, headers)
            self._connected = True
        except BaseException:
            await self.close()
            raise

    async def send_audio(self, chunk: AudioChunk) -> None:
        if not self._connected or self._input_track is None:
            raise QuicksilverTransportError("Quicksilver transport is not connected")
        self._input_track.write(chunk)

    async def send(self, message: dict[str, Any]) -> None:
        if not self._connected or self._sideband is None or self._sideband.closed:
            raise QuicksilverTransportError("Quicksilver sideband is not connected")
        await self._sideband.send_json(message)

    async def interrupt(self) -> None:
        """Input audio drives provider-side barge-in; no separate wire command is known."""

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connected = False
        sideband = self._sideband
        self._sideband = None
        if sideband is not None and not sideband.closed:
            with contextlib.suppress(Exception):
                await sideband.send_json(build_session_close())
            await sideband.close(code=1000, message=b"done")
        if self._sideband_task is not None:
            self._sideband_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sideband_task
            self._sideband_task = None
        for task in tuple(self._remote_audio_tasks):
            task.cancel()
        if self._remote_audio_tasks:
            await asyncio.gather(*self._remote_audio_tasks, return_exceptions=True)
        self._remote_audio_tasks.clear()
        for task in tuple(self._event_tasks):
            task.cancel()
        if self._event_tasks:
            await asyncio.gather(*self._event_tasks, return_exceptions=True)
        self._event_tasks.clear()
        if self._input_track is not None:
            await self._input_track.close()
            self._input_track = None
        if self._peer is not None:
            await self._peer.close()
            self._peer = None
        if self._http is not None:
            await self._http.close()
            self._http = None

    def _install_peer_handlers(self, peer: RTCPeerConnection, channel: RTCDataChannel) -> None:
        @channel.on("message")
        def on_message(message: str | bytes) -> None:
            if self._sideband is not None and not self._sideband.closed:
                return
            event = parse_provider_event(message)
            if event is not None:
                task = asyncio.ensure_future(self._event_sink(event))
                self._event_tasks.add(task)
                task.add_done_callback(self._event_tasks.discard)

        @peer.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind != "audio":
                return
            task = asyncio.create_task(self._read_remote_audio(track))
            self._remote_audio_tasks.add(task)
            task.add_done_callback(self._remote_audio_tasks.discard)

    async def _read_remote_audio(self, track: MediaStreamTrack) -> None:
        resampler = AudioResampler(format="s16", layout="mono", rate=24_000)
        try:
            while not self._closed:
                frame: object = await track.recv()  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
                if not isinstance(frame, AudioFrame):
                    continue
                for output in resampler.resample(frame):
                    size = output.samples * 2
                    audio = bytes(output.planes[0])[:size]
                    if audio:
                        await self._audio_sink(
                            AudioChunk(audio=audio, sample_rate=24_000, num_channels=1)
                        )
        except (MediaStreamError, asyncio.CancelledError):
            return

    async def _connect_sideband(self, call_id: str, headers: dict[str, str]) -> None:
        if self._http is None:
            raise QuicksilverTransportError("HTTP session is unavailable")
        failure: Exception = QuicksilverTransportError("sideband connection failed")
        for attempt in range(5):
            try:
                sideband = await self._http.ws_connect(
                    sideband_url(call_id),
                    headers=headers,
                )
                self._sideband = sideband
                self._sideband_task = asyncio.create_task(self._read_sideband(sideband))
                return
            except (aiohttp.ClientError, TimeoutError) as error:
                failure = error
                if attempt < 4:
                    await asyncio.sleep(0.2 * 2**attempt)
        raise QuicksilverTransportError("Quicksilver sideband connection failed") from failure

    async def _read_sideband(self, sideband: aiohttp.ClientWebSocketResponse) -> None:
        async for message in sideband:
            if message.type == aiohttp.WSMsgType.TEXT:
                event = parse_provider_event(message.data)
                if event is not None:
                    await self._event_sink(event)
            elif message.type in {aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED}:
                break
        if not self._closed:
            await self._event_sink(
                ProviderEvent(type="error", message="Quicksilver sideband disconnected")
            )

    @staticmethod
    async def _wait_for_ice(peer: RTCPeerConnection) -> None:
        if peer.iceGatheringState == "complete":
            return
        ready = asyncio.Event()

        @peer.on("icegatheringstatechange")
        def on_state() -> None:
            if peer.iceGatheringState == "complete":
                ready.set()

        await asyncio.wait_for(ready.wait(), timeout=10)

    @staticmethod
    async def _wait_for_peer(peer: RTCPeerConnection) -> None:
        if peer.connectionState == "connected":
            return
        ready = asyncio.Event()

        @peer.on("connectionstatechange")
        def on_state() -> None:
            if peer.connectionState == "connected":
                ready.set()
            elif peer.connectionState in {"failed", "closed"}:
                ready.set()

        await asyncio.wait_for(ready.wait(), timeout=15)
        if peer.connectionState != "connected":
            raise QuicksilverTransportError(
                f"Quicksilver WebRTC connection ended in state {peer.connectionState}"
            )
