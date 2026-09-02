"""Pluggable transcript grooming for cascaded voice sessions."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Literal, Protocol

from loguru import logger
from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kassette.filler_filter import filter_filler_words

_MAX_PROFILE_BYTES = 256_000
_MAX_OVERRIDE_COUNT = 1_000
_MAX_OVERRIDE_CHARS = 256
_SPOKEN_REPLACEMENTS: tuple[tuple[str, str], ...] = (
    ("period", "."),
    ("comma", ","),
    ("question mark", "?"),
    ("exclamation mark", "!"),
    ("colon", ":"),
    ("semicolon", ";"),
    ("tab", "\t"),
    ("dash", "-"),
    ("underscore", "_"),
    ("open paren", "("),
    ("close paren", ")"),
    ("open bracket", "["),
    ("close bracket", "]"),
    ("open brace", "{"),
    ("close brace", "}"),
    ("at symbol", "@"),
    ("hash", "#"),
    ("dollar sign", "$"),
    ("percent", "%"),
    ("caret", "^"),
    ("ampersand", "&"),
    ("asterisk", "*"),
    ("plus", "+"),
    ("equals", "="),
    ("less than", "<"),
    ("greater than", ">"),
    ("slash", "/"),
    ("backslash", "\\"),
    ("pipe", "|"),
    ("tilde", "~"),
    ("grave", "`"),
    ("quote", '"'),
    ("apostrophe", "'"),
)


@dataclass(frozen=True, slots=True)
class TranscriptGroomingRequest:
    """One provider transcript update presented to a groomer adapter."""

    text: str
    final: bool
    language: str | None = None


@dataclass(frozen=True, slots=True)
class GroomedTranscript:
    """A groomer's replacement text plus privacy-safe change metadata."""

    text: str
    changed: bool
    adapter: str


class TranscriptGroomer(Protocol):
    """Small seam implemented by transcript grooming adapters."""

    @property
    def name(self) -> str: ...

    async def groom(self, request: TranscriptGroomingRequest) -> GroomedTranscript: ...


class TranscriptGroomingProfile(BaseModel):
    """Versioned deterministic grooming profile loaded outside the repository."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1, 2] = 1
    lowercase: bool = False
    preserve_pronoun_i: bool = True
    collapse_whitespace: bool = True
    word_overrides: dict[str, str] = Field(default_factory=dict)
    filter_filler_words: bool = False
    filler_words: list[str] = Field(default_factory=list)
    symbol_replacements: bool = False

    @field_validator("word_overrides")
    @classmethod
    def validate_word_overrides(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > _MAX_OVERRIDE_COUNT:
            raise ValueError(f"word_overrides may contain at most {_MAX_OVERRIDE_COUNT} entries")
        normalized: dict[str, str] = {}
        for source, replacement in value.items():
            source = source.strip()
            replacement = replacement.strip()
            if not source:
                raise ValueError("word override sources cannot be empty")
            if len(source) > _MAX_OVERRIDE_CHARS or len(replacement) > _MAX_OVERRIDE_CHARS:
                raise ValueError(
                    f"word override sources and replacements may contain at most "
                    f"{_MAX_OVERRIDE_CHARS} characters"
                )
            normalized[source] = replacement
        return normalized

    @field_validator("filler_words")
    @classmethod
    def validate_filler_words(cls, value: list[str]) -> list[str]:
        if len(value) > _MAX_OVERRIDE_COUNT:
            raise ValueError(f"filler_words may contain at most {_MAX_OVERRIDE_COUNT} entries")
        normalized: list[str] = []
        for word in value:
            word = word.strip()
            if not word:
                raise ValueError("filler words cannot be empty")
            if len(word) > _MAX_OVERRIDE_CHARS:
                raise ValueError(
                    f"filler words may contain at most {_MAX_OVERRIDE_CHARS} characters"
                )
            normalized.append(word)
        return normalized

    @model_validator(mode="after")
    def require_version_two_for_extended_rules(self) -> TranscriptGroomingProfile:
        if self.version == 1 and (
            self.filter_filler_words or self.filler_words or self.symbol_replacements
        ):
            raise ValueError("filler filtering and symbol replacements require a version 2 profile")
        return self


class NoOpTranscriptGroomer:
    """Default adapter that preserves provider transcripts exactly."""

    @property
    def name(self) -> str:
        return "none"

    async def groom(self, request: TranscriptGroomingRequest) -> GroomedTranscript:
        return GroomedTranscript(text=request.text, changed=False, adapter=self.name)


class RuleTranscriptGroomer:
    """Fast, idempotent style and recognition corrections for live and final text."""

    def __init__(self, profile: TranscriptGroomingProfile) -> None:
        self._profile = profile
        self._overrides = [
            (
                re.compile(rf"(?<!\w){re.escape(source)}(?!\w)", flags=re.IGNORECASE),
                replacement,
            )
            for source, replacement in sorted(
                profile.word_overrides.items(),
                key=lambda item: len(item[0]),
                reverse=True,
            )
        ]

    @property
    def name(self) -> str:
        return "rules"

    async def groom(self, request: TranscriptGroomingRequest) -> GroomedTranscript:
        text = request.text
        uses_extended_rules = self._profile.filter_filler_words or self._profile.symbol_replacements
        if uses_extended_rules:
            text = text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
        if self._profile.collapse_whitespace:
            text = re.sub(r"\s+", " ", text).strip()
        for pattern, replacement in self._overrides:
            text = pattern.sub(replacement, text)
        if self._profile.filter_filler_words:
            text = filter_filler_words(text, self._profile.filler_words)
        if self._profile.symbol_replacements:
            text = _replace_spoken_symbols(text)
        if self._profile.lowercase:
            text = text.lower()
            if self._profile.preserve_pronoun_i:
                text = re.sub(r"\bi(?=\b|['\u2019])", "I", text)
        return GroomedTranscript(
            text=text,
            changed=text != request.text,
            adapter=self.name,
        )


def _replace_spoken_symbols(text: str) -> str:
    """Render explicit HyprWhspr spoken symbol commands without touching raw punctuation."""
    text = re.sub(
        r"\bnew[ \t]+line\b(?:[ \t]*[.!?]+)?",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    markers: dict[str, str] = {}
    for index, (command, symbol) in enumerate(_SPOKEN_REPLACEMENTS):
        marker = f"\ue000{index}\ue001"
        markers[marker] = symbol
        escaped_symbol = re.escape(symbol)
        escaped_command = re.escape(command)
        single_symbol = rf"{escaped_symbol}(?!{escaped_symbol})"
        pattern = (
            rf"(?:{escaped_symbol}+[ \t]+\b{escaped_command}\b{single_symbol}"
            rf"|{escaped_symbol}+\b{escaped_command}\b"
            rf"|\b{escaped_command}\b{single_symbol}"
            rf"|\b{escaped_command}\b)"
        )
        text = re.sub(pattern, marker, text, flags=re.IGNORECASE)

    for marker, symbol in markers.items():
        if symbol in ".,?!:;":
            text = re.sub(rf"[ \t]*{re.escape(marker)}[ \t]*", marker, text)
            text = re.sub(
                rf"{re.escape(marker)}(?=[^\s\ue000)\]}}>,.?!:;\u3002\uFF0C\u3001\uFF01\uFF1F\uFF1A\uFF1B])",
                symbol + " ",
                text,
            )
            text = text.replace(marker, symbol)
        elif symbol in "([{":
            text = re.sub(rf"{re.escape(marker)}[ \t]*", marker, text)
            text = text.replace(marker, symbol)
        elif symbol in ")]}":
            text = re.sub(rf"[ \t]*{re.escape(marker)}", marker, text)
            text = text.replace(marker, symbol)
        else:
            text = text.replace(marker, symbol)

    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def load_transcript_groomer(profile_path: Path | None) -> TranscriptGroomer:
    """Load the configured deterministic adapter, or the default no-op adapter."""

    if profile_path is None:
        return NoOpTranscriptGroomer()
    raw = profile_path.read_bytes()
    if len(raw) > _MAX_PROFILE_BYTES:
        raise ValueError(f"transcript grooming profile exceeds {_MAX_PROFILE_BYTES} bytes")
    profile = TranscriptGroomingProfile.model_validate_json(raw)
    return RuleTranscriptGroomer(profile)


class TranscriptGroomingProcessor(FrameProcessor):
    """Apply a groomer after STT and before normalized transcript events."""

    def __init__(
        self,
        groomer: TranscriptGroomer,
        *,
        timeout_secs: float = 0.5,
        name: str | None = None,
        enable_direct_mode: bool = False,
    ) -> None:
        if timeout_secs <= 0:
            raise ValueError("timeout_secs must be positive")
        super().__init__(  # pyright: ignore[reportUnknownMemberType]
            name=name,
            enable_direct_mode=enable_direct_mode,
        )
        self._groomer = groomer
        self._timeout_secs = timeout_secs

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        if direction is FrameDirection.DOWNSTREAM and isinstance(
            frame,
            (InterimTranscriptionFrame, TranscriptionFrame),
        ):
            await self._groom_frame(frame)
        await self.push_frame(frame, direction)

    async def _groom_frame(
        self,
        frame: InterimTranscriptionFrame | TranscriptionFrame,
    ) -> None:
        started = perf_counter()
        final = isinstance(frame, TranscriptionFrame)
        request = TranscriptGroomingRequest(
            text=frame.text,
            final=final,
            language=str(frame.language) if frame.language is not None else None,
        )
        try:
            async with asyncio.timeout(self._timeout_secs):
                result = await self._groomer.groom(request)
        except TimeoutError:
            logger.warning(
                "transcript grooming timed out adapter={} final={} timeout_ms={}",
                self._groomer.name,
                final,
                round(self._timeout_secs * 1_000),
            )
            return
        except Exception as error:
            logger.warning(
                "transcript grooming failed adapter={} final={} error_type={}",
                self._groomer.name,
                final,
                type(error).__name__,
            )
            return
        if frame.text.strip() and not result.text.strip():
            logger.warning(
                "transcript grooming returned empty text; preserving provider transcript "
                "adapter={} final={}",
                result.adapter,
                final,
            )
            return
        frame.text = result.text
        logger.debug(
            "transcript grooming complete adapter={} final={} changed={} elapsed_ms={}",
            result.adapter,
            final,
            result.changed,
            round((perf_counter() - started) * 1_000, 2),
        )


def profile_schema() -> dict[str, Any]:
    """Return the public JSON schema for editor and integration tooling."""

    return TranscriptGroomingProfile.model_json_schema()
