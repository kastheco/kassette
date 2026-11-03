"""`kassette dictate`: macOS toggle dictation. Needs only `kassette[dictate]`."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence
from typing import NoReturn

from loguru import logger
from pydantic import ValidationError

from kassette.settings import load_settings

_PREFIX = "kassette dictate"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PREFIX,
        description=(
            "Toggle macOS dictation with a global hotkey and paste the groomed transcript "
            "at the cursor."
        ),
    )
    parser.add_argument(
        "--hotkey",
        help="toggle hotkey, such as ctrl+option+d. overrides KASSETTE_DICTATE_HOTKEY",
    )
    return parser


def _fail(message: str) -> NoReturn:
    print(f"{_PREFIX}: {message}", file=sys.stderr)
    raise SystemExit(1)


def _notify(message: str) -> None:
    print(f"{_PREFIX}: {message}", file=sys.stderr, flush=True)


def _validation_summary(error: ValidationError) -> str:
    """Name invalid fields and reasons without echoing configured values."""
    return "; ".join(
        f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
    )


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if sys.platform != "darwin":
        _fail("kassette dictate requires macOS")
    if args.hotkey is not None:
        os.environ["KASSETTE_DICTATE_HOTKEY"] = args.hotkey
    try:
        settings = load_settings()
    except ValidationError as error:
        _fail(f"invalid configuration: {_validation_summary(error)}")

    from kassette.dictation import DictationError, run_dictation, validate_dictation_settings

    try:
        groomer = validate_dictation_settings(settings)
        from kassette import macos

        macos.require_microphone()
        macos.require_accessibility()
        platform = macos.MacDictationPlatform()
    except DictationError as error:
        _fail(str(error))

    # Normal logs leave transcript text out. Keep debug output off the terminal.
    logger.remove()
    logger.add(sys.stderr, level="WARNING")

    try:
        asyncio.run(run_dictation(settings, groomer, platform=platform, notify=_notify))
    except KeyboardInterrupt:
        pass
    except DictationError as error:
        _fail(str(error))
