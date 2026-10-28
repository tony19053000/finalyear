"""V1.0 G5 (D414): every ARCHITECTURE.md section says whether it is current or
deferred in v1.0, and the deferred ones are exactly the ADR-0012 deferrals."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

DOC: Final = Path(__file__).resolve().parents[2] / "docs" / "ARCHITECTURE.md"
_SECTION: Final = re.compile(r"^## (\d+)\. (.+)\n\n> \*\*v1\.0 status:\*\* (.+)$", re.M)


def sections() -> dict[int, tuple[str, str]]:
    text = DOC.read_text(encoding="utf-8")
    headings = re.findall(r"^## \d+\. ", text, re.M)
    found = {int(n): (title, status) for n, title, status in _SECTION.findall(text)}
    assert len(found) == len(headings), "a numbered section has no v1.0 status line"
    return found


def test_every_section_is_marked_current_or_deferred() -> None:
    found = sections()
    assert sorted(found) == list(range(1, len(found) + 1))
    for n, (title, status) in found.items():
        assert status.startswith(("current", "deferred")), (n, title, status)


def test_the_mode2_and_mode4_flows_are_deferred_and_cite_adr_0012() -> None:
    found = sections()
    deferred = {n for n, (_, status) in found.items() if status.startswith("deferred")}
    titles = {found[n][0] for n in deferred}
    assert titles == {"Mode 2 — Remote GPU flow", "Mode 4 — Combined GPU flow"}
    for n in deferred:
        assert "ADR-0012" in found[n][1]


def test_shipped_flows_are_current() -> None:
    found = sections()
    by_title = dict(found.values())
    for title in ("Mode 1 — Job Offloading flow", "Mode 3 — Task Splitting flow"):
        assert by_title[title].startswith("current"), title
