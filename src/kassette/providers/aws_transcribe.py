"""AWS Transcribe streaming adapter with vocabulary support and a drained end of stream."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from typing import cast

from botocore.auth import SigV4QueryAuth
from botocore.awsrequest import AWSRequest
from botocore.credentials import Credentials
from loguru import logger
from pipecat.frames.frames import Frame
from pipecat.services.aws.stt import AWSTranscribeSTTService
from pipecat.services.aws.utils import build_event_message
from pipecat.utils.errors import ErrorCategory, extract_http_status_code
from pipecat.utils.types import assert_given
from websockets import Subprotocol
from websockets.asyncio.client import ClientConnection
from websockets.protocol import State

from kassette.aws_credentials import AWSTranscribeCredentials

TRANSCRIBE_SAMPLE_RATE = 16_000
_STREAM_PATH = "/stream-transcription-websocket"
_URL_EXPIRES_SECS = 300
# AWS ends a stream on an empty audio event, then sends the remaining final
# results and closes the websocket.
_END_OF_STREAM = build_event_message(b"")


def transcribe_stream_url(
    credentials: AWSTranscribeCredentials,
    *,
    language_code: str,
    sample_rate: int = TRANSCRIBE_SAMPLE_RATE,
    vocabulary_name: str | None = None,
    vocabulary_filter_name: str | None = None,
) -> str:
    """Return a SigV4-presigned Transcribe streaming websocket URL."""
    params = {
        "enable-partial-results-stabilization": "true",
        "language-code": language_code,
        "media-encoding": "pcm",
        "partial-results-stability": "high",
        "sample-rate": str(sample_rate),
    }
    if vocabulary_name:
        params["vocabulary-name"] = vocabulary_name
    if vocabulary_filter_name:
        params["vocabulary-filter-name"] = vocabulary_filter_name
        params["vocabulary-filter-method"] = "remove"
    suffix = "amazonaws.com.cn" if credentials.region.startswith("cn-") else "amazonaws.com"
    host = f"transcribestreaming.{credentials.region}.{suffix}:8443"
    request = AWSRequest(method="GET", url=f"https://{host}{_STREAM_PATH}", params=params)
    SigV4QueryAuth(
        Credentials(
            credentials.access_key_id,
            credentials.secret_access_key,
            credentials.session_token,
        ),
        "transcribe",
        credentials.region,
        expires=_URL_EXPIRES_SECS,
    ).add_auth(request)
    url = str(request.prepare().url)
    return "wss://" + url.removeprefix("https://")


class PausableAWSTranscribeSTTService(AWSTranscribeSTTService):
    """AWS Transcribe adapter that can end its stream and wait for final results."""

    _FINISH_TIMEOUT_SECS = 3.0

    def __init__(
        self,
        *,
        credentials: AWSTranscribeCredentials,
        credentials_resolver: Callable[[], AWSTranscribeCredentials] | None = None,
        vocabulary_name: str | None = None,
        vocabulary_filter_name: str | None = None,
        sample_rate: int = TRANSCRIBE_SAMPLE_RATE,
    ) -> None:
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            api_key=credentials.secret_access_key,
            aws_access_key_id=credentials.access_key_id,
            aws_session_token=credentials.session_token,
            region=credentials.region,
            sample_rate=sample_rate,
        )
        self._aws_credentials = credentials
        self._credentials_resolver = credentials_resolver
        self._vocabulary_name = vocabulary_name
        self._vocabulary_filter_name = vocabulary_filter_name
        self._input_finished = False
        self._input_paused = False
        self._input_pause_lock: asyncio.Lock | None = None
        self.last_error: str | None = None

    def _socket(self) -> ClientConnection | None:
        return cast("ClientConnection | None", self._websocket)  # pyright: ignore[reportUnknownMemberType]

    @property
    def connected(self) -> bool:
        websocket = self._socket()
        return websocket is not None and websocket.state is State.OPEN

    def _pause_lock(self) -> asyncio.Lock:
        if self._input_pause_lock is None:
            self._input_pause_lock = asyncio.Lock()
        return self._input_pause_lock

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        if self._input_finished:
            yield None
            return
        async for frame in super().run_stt(audio):
            yield frame

    async def push_error(
        self,
        error_msg: str,
        exception: Exception | None = None,
        fatal: bool = False,
        category: ErrorCategory | None = None,
        force_treat_as_permanent: bool = False,
    ) -> None:
        self.last_error = error_msg
        await super().push_error(
            error_msg,
            exception=exception,
            fatal=fatal,
            category=category,
            force_treat_as_permanent=force_treat_as_permanent,
        )

    async def finish_input(self, timeout_secs: float = _FINISH_TIMEOUT_SECS) -> bool:
        """End the audio stream and wait until AWS delivers its final results.

        Returns False when the stream was not open or AWS didn't close it in time.
        Audio received after this call is dropped.
        """
        self._input_finished = True
        websocket = self._socket()
        receive_task = cast("asyncio.Task[None] | None", self._receive_task)  # pyright: ignore[reportUnknownMemberType]
        if websocket is None or websocket.state is not State.OPEN or receive_task is None:
            return False
        # The server closes the socket after the last result. That close is the
        # expected end of this stream, not a reason to reconnect.
        self._disconnecting = True
        try:
            await websocket.send(_END_OF_STREAM)
        except Exception as error:
            await self.push_error(
                error_msg=f"Error ending AWS Transcribe stream: {type(error).__name__}"
            )
            return False
        done, _pending = await asyncio.wait({receive_task}, timeout=timeout_secs)
        return receive_task in done

    async def pause_input(self) -> None:
        async with self._pause_lock():
            if self._input_paused:
                return
            await self.finish_input()
            await self._disconnect()
            self._input_paused = True

    async def resume_input(self) -> None:
        async with self._pause_lock():
            if not self._input_paused:
                return
            self._input_finished = False
            await self._connect()
            if not self.connected:
                return
            self._input_paused = False

    async def _connect_websocket(self) -> None:
        """Connect with vocabulary options. Never logs the presigned URL."""
        try:
            websocket = self._socket()
            if websocket is not None and websocket.state is State.OPEN:
                return
            credentials = (
                await asyncio.to_thread(self._credentials_resolver)
                if self._credentials_resolver is not None
                else self._aws_credentials
            )
            url = transcribe_stream_url(
                credentials,
                language_code=str(assert_given(self._settings.language)),
                sample_rate=TRANSCRIBE_SAMPLE_RATE,
                vocabulary_name=self._vocabulary_name,
                vocabulary_filter_name=self._vocabulary_filter_name,
            )
            # websockets generates the handshake key, version, and upgrade headers.
            self._websocket = await self._websocket_connect(  # pyright: ignore[reportUnknownMemberType]
                url,
                additional_headers={"Origin": "https://localhost"},
                subprotocols=[Subprotocol("mqtt")],
                ping_interval=None,
                ping_timeout=None,
                compression=None,
            )
            await self._call_event_handler("on_connected")  # pyright: ignore[reportUnknownMemberType]
            logger.debug("{} connected to AWS Transcribe", self)
        except Exception as error:
            self._websocket = None
            status = extract_http_status_code(error)
            detail = f"HTTP {status}" if status is not None else type(error).__name__
            await self.push_error(error_msg=f"Unable to connect to AWS Transcribe: {detail}")
