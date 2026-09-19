"""Gemini Live adapter that delegates substantive answers to the active client."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, cast

from google.genai.types import FunctionResponse, InteractionStatus, LiveServerMessage
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.frames.frames import Frame, InterruptionFrame, OutputTransportMessageUrgentFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.google.gemini_live.llm import GeminiLiveLLMService
from pipecat.services.llm_service import FunctionCallParams

from kassette.domain import EventSink, SessionEvent, SessionEventType, SessionState

CONSULT_TOOL_NAME = "kassette_agent_consult"
_MAX_DELEGATION_TEXT_CHARS = 32_000
DELEGATED_GEMINI_LIVE_INSTRUCTIONS = """You are Kassette's realtime voice layer.

For every non-empty user request, call kassette_agent_consult before giving a
substantive answer. The active client owns reasoning, tools, memory, and the
final answer. You may offer one short acknowledgment while the tool is running,
but do not answer the request yourself. When the tool returns, speak its response
naturally without adding facts, advice, or commentary."""


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
        await super()._handle_session_ready(session)  # pyright: ignore[reportPrivateUsage]
        # Kassette intentionally has no LLM context aggregator. Pipecat otherwise
        # waits forever for one before accepting microphone frames.
        if self._context is None:
            self._ready_for_realtime_input = True

    async def _handle_msg_turn_complete(self, message: LiveServerMessage) -> None:
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
        await super().process_frame(frame, direction)

    async def cleanup(self) -> None:
        self._closed = True
        self._cancel_pending_delegations()
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

    def _cancel_pending_delegations(self) -> None:
        for pending in self._pending_delegations.values():
            if not pending.response.done():
                pending.response.cancel()
        self._pending_delegations.clear()
