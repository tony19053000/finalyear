"""V1.0 G5: PROJECT_SUMMARY.md says plainly what v1.0 ships and what is
deferred - and never lists a deferred mode as shipped (V1.0 brief §0: no
slide, demo or README may imply otherwise)."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

DOC: Final = Path(__file__).resolve().parents[2] / "docs" / "PROJECT_SUMMARY.md"


def _section(text: str, heading: str) -> str:
    body = text.split(f"## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def test_ships_and_deferred_are_both_stated() -> None:
    text = DOC.read_text(encoding="utf-8")
    ships = _section(text, "What version 1.0 ships")
    for words in ("Mode 1", "Mode 3", "planner", "Security", "dashboard", "Installers"):
        assert words in ships, words
    assert "Mode 2" not in ships and "Mode 4" not in ships
    deferred = _section(text, "Deferred: designed, not built")
    for words in ("Mode 2", "Mode 4", "not in version 1.0", "ADR-0012"):
        assert words in deferred, words
