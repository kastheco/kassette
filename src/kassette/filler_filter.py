"""Deterministic removal of configured spoken filler words."""

from __future__ import annotations

import re
from collections.abc import Iterable

FILLER_PUNCTUATION = ".,!?;:\u2026\u3002\uff0c\u3001\uff01\uff1f\uff1a\uff1b"
FILLER_SENTENCE_END = ".!?\u2026\u3002\uff01\uff1f"
WRAPPER_PAIRS = {
    '"': '"',
    "'": "'",
    "\u201c": "\u201d",
    "\u2018": "\u2019",
    "(": ")",
    "[": "]",
    "{": "}",
    "\u300c": "\u300d",
    "\u300e": "\u300f",
    "\uff08": "\uff09",
}
FILLER_OPENERS = "".join(WRAPPER_PAIRS)
FILLER_CLOSERS = "".join(WRAPPER_PAIRS.values())
_CAPITALIZE_MARKER = "\ue002"


def _boundaried_pattern(term: str) -> str:
    """Return a Unicode-aware word pattern matching HyprWhspr's text rules."""
    if len(term) == 1:
        return re.escape(term)
    lead_guard = term[0].isalnum() or term[0] == "_"
    tail_guard = term[-1].isalnum() or term[-1] == "_"
    return (r"(?<!\w)" if lead_guard else "") + re.escape(term) + (r"(?!\w)" if tail_guard else "")


def _trailing_character(chunk: str, previous: str) -> str:
    for character in reversed(chunk):
        if character in "\r\n":
            return ""
        if not character.isspace() and character != _CAPITALIZE_MARKER:
            return character
    return previous


def _filler_pattern(words: list[str]) -> re.Pattern[str]:
    marks = "[" + re.escape(FILLER_PUNCTUATION) + "]*"
    alternatives = "|".join(
        _boundaried_pattern(word) for word in sorted(words, key=len, reverse=True)
    )
    return re.compile(
        "(?P<pre>" + marks + r")(?P<lead>[ \t]*)"
        "(?P<open>[" + re.escape(FILLER_OPENERS) + "])?"
        "(?P<word>(?:" + alternatives + "))"
        "(?P<punct>" + marks + ")"
        "(?(open)"
        + r"(?:(?P<gap>[ \t]*)(?P<close>["
        + re.escape(FILLER_CLOSERS)
        + r"]))?)"
        + r"(?P<trail>[ \t]*)",
        re.IGNORECASE,
    )


def _filler_replacement(match: re.Match[str], preceding: str) -> str:
    pre = match.group("pre")
    lead = match.group("lead")
    punct = match.group("punct")
    trail = match.group("trail")
    opener = match.group("open") or ""
    closer = match.group("close") or ""
    wrapper = bool(opener) and closer == WRAPPER_PAIRS.get(opener)
    kept_open = "" if wrapper else opener
    kept_close = "" if wrapper else (match.group("gap") or "") + closer
    preceding_mark = pre[-1:] or preceding
    opens_sentence = not preceding or preceding_mark in FILLER_SENTENCE_END
    nothing_before = not preceding and not pre
    marker = _CAPITALIZE_MARKER if match.group("word")[:1].isupper() and opens_sentence else ""

    if punct and punct[0] in FILLER_SENTENCE_END and not opener:
        if nothing_before:
            return marker
        if pre and pre[-1] in FILLER_SENTENCE_END:
            return pre + marker + trail
        return punct + marker + trail

    following = match.string[match.end() : match.end() + 1]
    hugs_closer = bool(following) and following in FILLER_CLOSERS
    vacant = nothing_before and not (kept_open or kept_close)
    separator = "" if hugs_closer or vacant else lead
    hugs_following = bool(kept_open) and not kept_close
    closing = "" if hugs_following or vacant else trail
    return pre + separator + kept_open + kept_close + marker + closing


def filter_filler_words(text: str, words: Iterable[str]) -> str:
    """Remove configured filler words and punctuation that belongs to them."""
    filtered_words = [word for word in words if word]
    if not text or not filtered_words:
        return text

    kept: list[str] = []
    cursor = 0
    preceding = ""
    for match in _filler_pattern(filtered_words).finditer(text):
        kept.append(text[cursor : match.start()])
        preceding = _trailing_character(kept[-1], preceding)
        replacement = _filler_replacement(match, preceding)
        kept.append(replacement)
        preceding = _trailing_character(replacement, preceding)
        cursor = match.end()
    kept.append(text[cursor:])

    filtered = re.sub(
        re.escape(_CAPITALIZE_MARKER) + r"([ \t]*)(\w)",
        lambda match: match.group(1) + match.group(2).upper(),
        "".join(kept),
    )
    return re.sub(r" +", " ", filtered.replace(_CAPITALIZE_MARKER, "")).strip()
