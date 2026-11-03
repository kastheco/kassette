#!/usr/bin/env python3
"""License report for an installed `kassette[dictate]` tree. Exits 1 on a disallowed dist.

    uv pip install --target /tmp/dictate --python-platform aarch64-apple-darwin \\
        --python-version 3.12 ".[dictate]"
    python scripts/dictate_license_report.py /tmp/dictate
"""

from __future__ import annotations

import argparse
import re
import sys
from email.parser import HeaderParser
from pathlib import Path

ALLOWED = ("MIT", "MIT-0", "BSD", "Apache-2.0", "PSF")
# The voice stack must never reach a dictate install.
FORBIDDEN = {"pipecat-ai", "aiortc", "av", "google-genai", "openai", "httpx", "typer"}
_ALIASES = {
    "mit license": "MIT",
    "apache license 2.0": "Apache-2.0",
    "apache software license": "Apache-2.0",
    "bsd license": "BSD",
    "python software foundation license": "PSF",
}


def _family(value: str) -> str | None:
    value = value.strip()
    alias = _ALIASES.get(value.casefold())
    if alias is not None:
        return alias
    if value in {"MIT", "MIT-0", "Apache-2.0"}:
        return value
    if re.fullmatch(r"BSD(-\d-Clause)?", value):
        return "BSD"
    if re.fullmatch(r"PSF(-2\.0)?", value):
        return "PSF"
    return None


def _expression_families(expression: str) -> set[str] | None:
    """Allowed families for an SPDX-style expression, or None when no option is allowed.

    Any `OR` alternative may be chosen, and every `AND` part of it must be allowed.
    """
    for alternative in re.split(r"\s+OR\s+", expression.strip().strip("()")):
        parts = [_family(part.strip("() ")) for part in re.split(r"\s+AND\s+", alternative)]
        if parts and all(parts):
            return {part for part in parts if part}
    return None


def licenses(metadata: str) -> tuple[list[str], set[str] | None]:
    """Return the declared license values and the allowed families, if any are allowed."""
    headers = HeaderParser().parsestr(metadata)
    declared: list[str] = []
    for key in ("License-Expression", "License"):
        value = headers.get(key)
        if value and value.strip() and "\n" not in value.strip():
            declared.append(value.strip())
    for classifier in headers.get_all("Classifier") or []:
        if classifier.startswith("License :: OSI Approved :: "):
            declared.append(classifier.removeprefix("License :: OSI Approved :: "))
    # A dist is allowed when any declared value is: classifiers list dual-license options.
    for value in declared:
        families = _expression_families(value)
        if families is not None:
            return declared, families
    return declared, None


def main() -> int:
    parser = argparse.ArgumentParser(description="license report for a kassette[dictate] tree")
    parser.add_argument("site", type=Path, help="install target containing *.dist-info")
    args = parser.parse_args()
    failures = 0
    for dist in sorted(args.site.glob("*.dist-info")):
        metadata = (dist / "METADATA").read_text(encoding="utf-8")
        headers = HeaderParser().parsestr(metadata)
        name = str(headers["Name"]).lower().replace("_", "-")
        if name == "kassette":
            continue
        declared, families = licenses(metadata)
        ok = families is not None and name not in FORBIDDEN
        failures += not ok
        label = ", ".join(sorted(families or ())) or "; ".join(declared) or "unknown"
        print(f"{'ok  ' if ok else 'FAIL'} {name} {headers['Version']}: {label}")
    print(f"allowed: {', '.join(ALLOWED)}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
