"""Bounded, content-free lifecycle diagnostics."""

from __future__ import annotations

import asyncio
import time

from kassette.domain import SessionEvent

type DiagnosticValue = str | int | bool | None
type DiagnosticRecord = dict[str, DiagnosticValue]

_MAX_TRACKED_SESSIONS = 128
_MAX_IDENTIFIER_CHARS = 96
_MAX_SEQUENCE = 1_000_000
_MAX_ELAPSED_MS = 86_400_000


def _bounded_identifier(value: object | None) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return text[:_MAX_IDENTIFIER_CHARS]


class LifecycleDiagnostics:
    """Produce structured timing records without provider or user content."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._started: dict[str, float] = {}
        self._sequences: dict[str, int] = {}

    async def record(self, event: SessionEvent) -> DiagnosticRecord:
        now = time.monotonic()
        async with self._lock:
            if event.session_id not in self._started:
                if len(self._started) >= _MAX_TRACKED_SESSIONS:
                    oldest = next(iter(self._started))
                    self._started.pop(oldest, None)
                    self._sequences.pop(oldest, None)
                self._started[event.session_id] = now
            sequence = min(self._sequences.get(event.session_id, 0) + 1, _MAX_SEQUENCE)
            self._sequences[event.session_id] = sequence
            elapsed_ms = min(
                max(0, round((now - self._started[event.session_id]) * 1000)),
                _MAX_ELAPSED_MS,
            )
            if (event.state is not None and event.state.value == "closed") or (
                event.type.value == "session.error"
            ):
                self._started.pop(event.session_id, None)
                self._sequences.pop(event.session_id, None)

        return {
            "session_id": _bounded_identifier(event.session_id),
            "sequence": sequence,
            "elapsed_ms": elapsed_ms,
            "type": event.type.value,
            "state": event.state.value if event.state is not None else None,
            "role": event.role.value if event.role is not None else None,
            "has_text": event.text is not None,
            "text_chars": min(len(event.text), 1_000_000) if event.text is not None else 0,
            "has_provider_type": event.provider_type is not None,
            "error_code": _bounded_identifier(event.error_code),
            "metadata_key_count": min(len(event.metadata), 64),
        }
