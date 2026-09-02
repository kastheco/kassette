import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pipecat.frames.frames import Frame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameDirection
from pydantic import ValidationError

from kassette.transcript_grooming import (
    GroomedTranscript,
    NoOpTranscriptGroomer,
    RuleTranscriptGroomer,
    TranscriptGroomingProcessor,
    TranscriptGroomingProfile,
    TranscriptGroomingRequest,
    load_transcript_groomer,
)


class RecordingTranscriptGroomingProcessor(TranscriptGroomingProcessor):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.pushed_frames: list[tuple[Frame, FrameDirection]] = []

    async def push_frame(
        self,
        frame: Frame,
        direction: FrameDirection = FrameDirection.DOWNSTREAM,
    ) -> None:
        self.pushed_frames.append((frame, direction))


class FailingGroomer:
    @property
    def name(self) -> str:
        return "failing"

    async def groom(self, request: TranscriptGroomingRequest) -> GroomedTranscript:
        raise RuntimeError(f"cannot groom final={request.final}")


class SlowGroomer:
    @property
    def name(self) -> str:
        return "slow"

    async def groom(self, request: TranscriptGroomingRequest) -> GroomedTranscript:
        await asyncio.sleep(0.05)
        return GroomedTranscript(text="late", changed=True, adapter=self.name)


async def test_noop_groomer_preserves_provider_text() -> None:
    groomer = NoOpTranscriptGroomer()

    result = await groomer.groom(
        TranscriptGroomingRequest(text="Provider Text", final=False, language="en")
    )

    assert result == GroomedTranscript(text="Provider Text", changed=False, adapter="none")


async def test_rule_groomer_applies_lowercase_and_word_overrides() -> None:
    groomer = RuleTranscriptGroomer(
        TranscriptGroomingProfile(
            lowercase=True,
            preserve_pronoun_i=True,
            word_overrides={
                "cosmos": "kasmos",
                "casmos": "kasmos",
                "chatmoy": "chezmoi",
                "gui": "tui",
            },
        )
    )

    result = await groomer.groom(
        TranscriptGroomingRequest(
            text="I'M using the GUI with Cosmos. During that period, Chatmoy worked.",
            final=True,
        )
    )

    assert result.text == "I'm using the tui with kasmos. during that period, chezmoi worked."
    assert result.changed is True
    assert "period" in result.text


async def test_version_one_profile_does_not_enable_new_transformations() -> None:
    groomer = RuleTranscriptGroomer(TranscriptGroomingProfile(version=1))

    result = await groomer.groom(
        TranscriptGroomingRequest(text="Um, new line. During that period", final=True)
    )

    assert result.text == "Um, new line. During that period"
    assert result.changed is False


async def test_filler_filter_removes_owned_punctuation_and_keeps_sentence_structure() -> None:
    groomer = RuleTranscriptGroomer(
        TranscriptGroomingProfile(
            version=2,
            filter_filler_words=True,
            filler_words=["well", "um"],
        )
    )

    result = await groomer.groom(
        TranscriptGroomingRequest(text="Well, um. okay. (um) Fine", final=True)
    )

    assert result.text == "Okay. Fine"


async def test_symbol_replacements_render_commands_and_preserve_raw_punctuation() -> None:
    groomer = RuleTranscriptGroomer(TranscriptGroomingProfile(version=2, symbol_replacements=True))

    result = await groomer.groom(
        TranscriptGroomingRequest(
            text="hello comma world new line. open paren x close paren...",
            final=True,
        )
    )

    assert result.text == "hello, world\n(x)..."


async def test_word_overrides_respect_unicode_word_boundaries_and_are_idempotent() -> None:
    groomer = RuleTranscriptGroomer(TranscriptGroomingProfile(word_overrides={"sink": "sync"}))
    request = TranscriptGroomingRequest(
        text="sink is not part of sinking or kitchen sinks",
        final=False,
    )

    first = await groomer.groom(request)
    second = await groomer.groom(TranscriptGroomingRequest(text=first.text, final=False))

    assert first.text == "sync is not part of sinking or kitchen sinks"
    assert second.text == first.text
    assert second.changed is False


async def test_processor_grooms_interim_and_final_frames_before_pushing() -> None:
    processor = RecordingTranscriptGroomingProcessor(
        RuleTranscriptGroomer(
            TranscriptGroomingProfile(lowercase=True, word_overrides={"casper": "kaspr"})
        )
    )
    interim = InterimTranscriptionFrame("CASPER IS READY", "owner", "now")
    final = TranscriptionFrame("CASPER IS READY", "owner", "now", finalized=True)

    await processor.process_frame(interim, FrameDirection.DOWNSTREAM)
    await processor.process_frame(final, FrameDirection.DOWNSTREAM)

    assert interim.text == "kaspr is ready"
    assert final.text == "kaspr is ready"
    assert [frame for frame, _direction in processor.pushed_frames] == [interim, final]


@pytest.mark.parametrize(
    "groomer,timeout_secs",
    [(FailingGroomer(), 0.5), (SlowGroomer(), 0.001)],
)
async def test_processor_fails_open_to_provider_text(
    groomer: FailingGroomer | SlowGroomer,
    timeout_secs: float,
) -> None:
    processor = RecordingTranscriptGroomingProcessor(groomer, timeout_secs=timeout_secs)
    frame = TranscriptionFrame("keep the raw transcript", "owner", "now", finalized=True)

    await processor.process_frame(frame, FrameDirection.DOWNSTREAM)

    assert frame.text == "keep the raw transcript"
    assert processor.pushed_frames[0][0] is frame


def test_profile_loader_uses_noop_by_default_and_rules_from_json(tmp_path: Path) -> None:
    assert isinstance(load_transcript_groomer(None), NoOpTranscriptGroomer)
    path = tmp_path / "profile.json"
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "lowercase": True,
                "preserve_pronoun_i": True,
                "word_overrides": {"casmos": "kasmos"},
                "filter_filler_words": True,
                "filler_words": ["um"],
                "symbol_replacements": True,
            }
        ),
        encoding="utf-8",
    )

    groomer = load_transcript_groomer(path)

    assert isinstance(groomer, RuleTranscriptGroomer)


def test_profile_rejects_unknown_versions_and_oversized_overrides(tmp_path: Path) -> None:
    versioned = tmp_path / "versioned.json"
    versioned.write_text('{"version": 3}', encoding="utf-8")
    with pytest.raises(ValidationError):
        load_transcript_groomer(versioned)

    with pytest.raises(ValidationError, match="at most 256 characters"):
        TranscriptGroomingProfile(word_overrides={"x" * 257: "value"})

    with pytest.raises(ValidationError, match="require a version 2 profile"):
        TranscriptGroomingProfile(version=1, symbol_replacements=True)
