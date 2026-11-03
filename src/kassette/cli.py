"""kassette entry point. Dispatches without importing either extra's dependencies.

`kassette dictate` needs `kassette[dictate]`. Every other command needs `kassette[voice]`.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

_VOICE_MODULES = {"typer", "pipecat", "aiortc", "av", "aiohttp", "google", "openai"}


def main(argv: Sequence[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["dictate"]:
        from kassette.dictate_cli import main as dictate

        dictate(args[1:])
        return
    try:
        from kassette.voice_cli import app
    except ModuleNotFoundError as error:
        if (error.name or "").split(".")[0] not in _VOICE_MODULES:
            raise
        print(
            "kassette: this command needs the voice extra. install kassette[voice], "
            "or run `kassette dictate` for macOS dictation",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    app(args=args, prog_name="kassette")
