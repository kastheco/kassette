"""Gemini Live adapter that delegates substantive answers to the active client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

from google.genai.types import FunctionResponse, InteractionStatus, LiveServerMessage
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import (
    Frame,
    InterruptionFrame,
    LLMFullResponseStartFrame,
    LLMTextFrame,
    OutputTransportMessageUrgentFrame,
    TranscriptionFrame,
    TTSStartedFrame,
    TTSTextFrame,
)
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.utils.text.base_text_aggregator import AggregationType
from pipecat.utils.time import time_now_iso8601

from kassette.domain import EventSink, SessionEvent, SessionEventType, SessionState, TranscriptRole

CONSULT_TOOL_NAME = "kassette_agent_consult"
_MAX_DELEGATION_TEXT_CHARS = 32_000
_MAX_TRANSCRIPT_CHARS = 32_000
# An open user turn with no new text for this long settles, so every delta gets a final.
_USER_SETTLE_SECS = 2.0
DELEGATED_GEMINI_LIVE_INSTRUCTIONS = """You are Kassette's realtime voice layer.

For every non-empty user request, call kassette_agent_consult before giving a
substantive answer. The active client owns reasoning, tools, memory, and the
final answer. You may offer one short acknowledgment while the tool is running,
but do not answer the request yourself. When the tool returns, speak its response
naturally without adding facts, advice, or commentary."""


class _PrivateTranscriptionFrame(TranscriptionFrame):
    def __str__(self) -> str:
        return f"{self.name}(text redacted)"


class _PrivateLLMTextFrame(LLMTextFrame):
    def __str__(self) -> str:
        return f"{self.name}(text redacted)"


class _PrivateTTSTextFrame(TTSTextFrame):
    def __str__(self) -> str:
        return f"{self.name}(text redacted)"


@dataclass(slots=True)
class _PendingDelegation:
    text: str
    response: asyncio.Future[str]


class DelegatedGeminiLiveService(GeminiLiveLLMService):
    """Native Gemini audio with every substantive response delegated to Kassette's client."""

    def __init__(
        self,
        *,
        session_id: str,
        api_key: str,
        model: str,
        event_sink: EventSink,
        thinking_level: str | None = None,
        publish_client_events: bool = False,
        name: str | None = None,
    ) -> None:
        self._session_id = session_id
        self._event_sink = event_sink
        self._sequence = 0
        self._publish_client_events = publish_client_events
        self._pending_delegations: dict[str, _PendingDelegation] = {}
        self._closed = False
        self._user_transcript = ""
        self._user_turn_id: str | None = None
        self._user_turn_number = 0
        self._assistant_transcript = ""
        self._user_settle_secs = _USER_SETTLE_SECS
        self._user_settle_task: asyncio.Task[None] | None = None
        thinking = {"thinking_level": thinking_level.upper()} if thinking_level is not None else {}
        tool = FunctionSchema(
            name=CONSULT_TOOL_NAME,
            description=(
                "Ask the active Kassette client to reason about and answer the user's request. "
                "Use this for every substantive user request."
            ),
            properties={
                "request": {
                    "type": "string",
                    "description": "The user's request, preserved accurately and concisely.",
                }
            },
            required=["request"],
            handler=self._delegate,
        )
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            api_key=api_key,
            tools=[tool],
            settings=GeminiLiveLLMService.Settings(
                model=model,
                thinking=thinking,
                enable_affective_dialog=False,
            ),
            system_instruction=DELEGATED_GEMINI_LIVE_INSTRUCTIONS,
            inference_on_context_initialization=False,
            name=name,
        )
        # This provider has no LLM context aggregator to auto-register the schema handler.
        self.register_function(
            CONSULT_TOOL_NAME,
            self._delegate,
            cancel_on_interruption=not self._uses_extended_thinking,
        )
        self._context = LLMContext(messages=[], tools=[tool])

    @property
    def _uses_extended_thinking(self) -> bool:
        return self._settings.model == "gemini-3.8-live-extended-thinking"

    @property
    def _supports_non_blocking_tools(self) -> bool:
        # Extended Live accepts NON_BLOCKING function declarations. Pipecat 1.8
        # predates the model and conservatively disables them for all Gemini 3 models.
        return self._uses_extended_thinking

    async def _handle_session_ready(self, session: Any) -> None:
        # A reconnect reuses this service. Settle the dropped turn so it isn't glued onto the next.
        await self._finish_user_transcript()
        await self._finish_assistant_transcript()
        await super()._handle_session_ready(session)  # pyright: ignore[reportPrivateUsage]
        # Kassette intentionally has no LLM context aggregator. Pipecat otherwise
        # waits forever for one before accepting microphone frames.
        if self._context is None:
            self._ready_for_realtime_input = True

    async def _handle_msg_input_transcription(self, message: LiveServerMessage) -> None:
        # Replaces Pipecat's handler, which logs the user's words at debug level.
        server_content = message.server_content
        transcription = server_content.input_transcription if server_content else None
        if transcription is None or self._closed:
            return
        text = transcription.text or ""
        if self._user_turn_id is None:
            if not text.strip():
                text = ""
            else:
                self._user_turn_number += 1
                self._user_turn_id = f"{self._session_id}:{self._user_turn_number}"
                text = text.lstrip()
        if text and self._user_turn_id is not None:
            turn_id = self._user_turn_id
            # Delivery can suspend past the previous fragment's deadline.
            if self._user_settle_task is not None:
                self._user_settle_task.cancel()
                self._user_settle_task = None
            self._user_transcript = (self._user_transcript + text)[:_MAX_TRANSCRIPT_CHARS]
            await self._emit_transcript(
                SessionEventType.TRANSCRIPT_DELTA,
                TranscriptRole.USER,
                self._user_transcript.rstrip(),
                turn_id=turn_id,
            )
            if self._closed:
                return
            if self._user_turn_id == turn_id:
                self._schedule_user_settle(turn_id)
        if transcription.finished:
            await self._finish_user_transcript()

    async def _handle_msg_output_transcription(self, message: LiveServerMessage) -> None:
        server_content = message.server_content
        transcription = server_content.output_transcription if server_content else None
        if transcription is None:
            return
        text = transcription.text or ""
        if text:
            await self._finish_user_transcript()
            self._assistant_transcript = (self._assistant_transcript + text)[:_MAX_TRANSCRIPT_CHARS]
            await self._emit_transcript(
                SessionEventType.TRANSCRIPT_DELTA,
                TranscriptRole.ASSISTANT,
                self._assistant_transcript,
            )
        if self._publish_client_events:
            await super()._handle_msg_output_transcription(message)  # pyright: ignore[reportPrivateUsage]
        elif text and not self._bot_is_responding:
            # Terminal transcripts go through the event sink, not trace-logged text frames.
            await self._set_bot_is_responding(True)
            await self.push_frame(TTSStartedFrame())
            await self.push_frame(LLMFullResponseStartFrame())
        if transcription.finished:
            await self._finish_assistant_transcript()

    async def _push_user_transcription(
        self, text: str, result: LiveServerMessage | None = None
    ) -> None:
        # Pipecat logs frame strings at TRACE. Preserve RTVI frames without logging speech.
        await self._handle_user_transcription(  # pyright: ignore[reportUnknownMemberType]
            text, True, self._settings.language
        )
        await self.push_frame(
            _PrivateTranscriptionFrame(
                text=text, user_id="", timestamp=time_now_iso8601(), result=result
            ),
            FrameDirection.UPSTREAM,
        )

    async def _push_output_transcription_text_frames(self, text: str) -> None:
        llm_frame = _PrivateLLMTextFrame(text)
        llm_frame.append_to_context = False
        await self.push_frame(llm_frame)
        tts_frame = _PrivateTTSTextFrame(text, aggregated_by=AggregationType.SENTENCE)
        tts_frame.includes_inter_frame_spaces = True
        await self.push_frame(tts_frame)

    async def _handle_msg_tool_call(self, message: LiveServerMessage) -> None:
        await self._finish_user_transcript()
        await super()._handle_msg_tool_call(message)  # pyright: ignore[reportPrivateUsage]

    async def _handle_msg_turn_complete(self, message: LiveServerMessage) -> None:
        await self._finish_user_transcript()
        await self._finish_assistant_transcript()
        server_content = message.server_content
        if server_content and server_content.interaction_status in {
            InteractionStatus.IN_PROGRESS,
            InteractionStatus.REQUIRES_ACTION,
        }:
            return
        await super()._handle_msg_turn_complete(message)  # pyright: ignore[reportPrivateUsage]

    async def handle_client_message(self, message: Any) -> bool:
        """Accept one completed active-client delegation response."""
        if not isinstance(message, dict):
            return False
        record = cast(dict[str, object], message)
        if record.get("label") != "kassette" or record.get("type") != "delegation.complete":
            return False
        data = record.get("data")
        if not isinstance(data, dict):
            return False
        payload = cast(dict[str, object], data)
        delegation_id = payload.get("delegation_id")
        text = payload.get("text")
        if (
            not isinstance(delegation_id, str)
            or not isinstance(text, str)
            or not text.strip()
            or len(text) > _MAX_DELEGATION_TEXT_CHARS
        ):
            return False
        pending = self._pending_delegations.get(delegation_id)
        if pending is None or pending.response.done():
            return False
        pending.response.set_result(text.strip())
        return True

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        if isinstance(frame, InterruptionFrame):
            self._cancel_pending_delegations()
            await self._finish_assistant_transcript()
        await super().process_frame(frame, direction)

    async def broadcast_interruption(self) -> None:
        await self._finish_assistant_transcript()
        await super().broadcast_interruption()

    async def cleanup(self) -> None:
        self._closed = True
        self._cancel_pending_delegations()
        await self._finish_user_transcript()
        await self._finish_assistant_transcript()
        await super().cleanup()

    async def _delegate(self, params: FunctionCallParams) -> None:
        request = params.arguments.get("request")
        text = request.strip() if isinstance(request, str) else ""
        if not text:
            await self._send_delegated_result(
                params.tool_call_id,
                {"error": "The user request was empty."},
            )
            return
        if self._closed:
            await self._send_delegated_result(
                params.tool_call_id,
                {"error": "Voice session is closed."},
            )
            return
        response: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = _PendingDelegation(text=text, response=response)
        self._pending_delegations[params.tool_call_id] = pending
        await self._emit_event(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.SESSION_STATE_CHANGED,
                state=SessionState.THINKING,
            )
        )
        await self._emit_event(
            SessionEvent(
                session_id=self._session_id,
                type=SessionEventType.DELEGATION_REQUESTED,
                text=text,
                metadata={"delegation_id": params.tool_call_id},
            )
        )
        try:
            answer = await response
        except asyncio.CancelledError:
            raise
        finally:
            self._pending_delegations.pop(params.tool_call_id, None)
        await self._send_delegated_result(params.tool_call_id, {"response": answer})

    async def _send_delegated_result(
        self,
        tool_call_id: str,
        result: dict[str, str],
    ) -> None:
        if self._disconnecting or self._session is None:
            return
        response = FunctionResponse(
            name=CONSULT_TOOL_NAME,
            id=tool_call_id,
            response=result,
        )
        await self._session.send_tool_response(function_responses=response)

    async def _emit_event(self, event: SessionEvent) -> None:
        await self._event_sink(event)
        if (
            not self._publish_client_events
            or event.type is not SessionEventType.DELEGATION_REQUESTED
        ):
            return
        self._sequence += 1
        await self.push_frame(
            OutputTransportMessageUrgentFrame(
                message={
                    "label": "kassette",
                    "type": event.type.value,
                    "data": {
                        "session_id": self._session_id,
                        "delegation_id": event.metadata["delegation_id"],
                        "text": event.text,
                        "sequence": self._sequence,
                    },
                }
            )
        )

    async def _emit_transcript(
        self,
        event_type: SessionEventType,
        role: TranscriptRole,
        text: str,
        *,
        turn_id: str | None = None,
    ) -> None:
        await self._event_sink(
            SessionEvent(
                session_id=self._session_id,
                type=event_type,
                role=role,
                text=text,
                metadata={"turn_id": turn_id} if turn_id is not None else {},
            )
        )

    def _schedule_user_settle(self, turn_id: str) -> None:
        if self._user_settle_task is not None:
            self._user_settle_task.cancel()
            self._user_settle_task = None
        if self._closed:
            return
        self._user_settle_task = asyncio.create_task(self._settle_user_turn(turn_id))

    async def _settle_user_turn(self, turn_id: str) -> None:
        await asyncio.sleep(self._user_settle_secs)
        if self._closed or self._user_turn_id != turn_id:
            return
        self._user_settle_task = None
        await self._finish_user_transcript()

    async def _finish_user_transcript(self) -> None:
        if self._user_settle_task is not None:
            self._user_settle_task.cancel()
            self._user_settle_task = None
        text = self._user_transcript.strip()
        turn_id = self._user_turn_id
        self._user_transcript = ""
        self._user_turn_id = None
        if not text or turn_id is None:
            return
        await self._emit_transcript(
            SessionEventType.TRANSCRIPT_FINAL, TranscriptRole.USER, text, turn_id=turn_id
        )
        if self._publish_client_events:
            # The web client's user captions come from this upstream frame.
            await self._push_user_transcription(text)  # pyright: ignore[reportPrivateUsage]

    async def _finish_assistant_transcript(self) -> None:
        text = self._assistant_transcript.strip()
        self._assistant_transcript = ""
        if text:
            await self._emit_transcript(
                SessionEventType.TRANSCRIPT_FINAL, TranscriptRole.ASSISTANT, text
            )

    def _cancel_pending_delegations(self) -> None:
        for pending in self._pending_delegations.values():
            if not pending.response.done():
                pending.response.cancel()
        self._pending_delegations.clear()
