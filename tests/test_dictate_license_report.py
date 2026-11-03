from __future__ import annotations

import runpy
from collections.abc import Callable
from typing import cast

import pytest

licenses = cast(
    "Callable[[str], tuple[list[str], set[str] | None]]",
    runpy.run_path("scripts/dictate_license_report.py")["licenses"],
)


@pytest.mark.parametrize(
    ("expression", "families"),
    [
        ("GPL-3.0-only AND (MIT OR Apache-2.0)", None),
        ("(MIT OR Apache-2.0) AND GPL-3.0-only", None),
        ("MIT OR Apache-2.0 AND GPL-3.0-only", {"MIT"}),
        ("(GPL-3.0-only OR MIT) AND Apache-2.0", {"MIT", "Apache-2.0"}),
        ("MIT AND (BSD-3-Clause OR GPL-3.0-only)", {"MIT", "BSD"}),
        ("((MIT))", {"MIT"}),
        ("MIT AND Apache-2.0", {"MIT", "Apache-2.0"}),
        ("GPL-3.0-only OR Apache-2.0", {"Apache-2.0"}),
        ("MIT OR (Apache-2.0", None),
        ("MIT) OR Apache-2.0", None),
        ("MIT OR", None),
        ("OR MIT", None),
        ("MIT AND ()", None),
        ("MIT WITH unknown-exception", None),
        ("MIT OR Apache-2.0 trailing", None),
        ("", None),
        ("Apache Software License", {"Apache-2.0"}),
    ],
)
def test_license_expression_preserves_required_terms(
    expression: str, families: set[str] | None
) -> None:
    declared, allowed = licenses(f"License-Expression: {expression}\n")

    assert declared == ([expression] if expression else [])
    assert allowed == families


def test_license_expression_overrides_permissive_legacy_metadata() -> None:
    metadata = (
        "License-Expression: MIT AND GPL-3.0-only\n"
        "License: MIT\n"
        "Classifier: License :: OSI Approved :: MIT License\n"
    )

    assert licenses(metadata) == (["MIT AND GPL-3.0-only"], None)


def test_legacy_metadata_allows_any_permissive_option_without_expression() -> None:
    metadata = (
        "License: GPL-3.0-only\n"
        "Classifier: License :: OSI Approved :: GNU General Public License v3 (GPLv3)\n"
        "Classifier: License :: OSI Approved :: BSD License\n"
    )

    assert licenses(metadata)[1] == {"BSD"}
