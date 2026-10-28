"""V1.0 G5 (D414): the ADR index ``docs/adr/README.md`` is complete and true.

* every ADR file has exactly one row, and the row's status is the status its
  own ``**Status:**`` line states (the file is the source of truth);
* every status is final (Accepted / Superseded / Deprecated / Rejected) or the
  ADR is listed under "Needs team acceptance" - nothing is silently Proposed;
* an index row never claims "Accepted" for an ADR whose file says otherwise
  (agents never accept ADRs; only the team does).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

pytestmark = pytest.mark.unit

ADR_DIR: Final = Path(__file__).resolve().parents[2] / "docs" / "adr"
FINAL: Final = ("Accepted", "Superseded", "Deprecated", "Rejected")
_STATUS: Final = re.compile(
    r"^[>-] \*\*Status:\*\* (?P<status>[A-Z][a-z]+(?: by \[?ADR-\d{4})?)", re.M
)
_ROW: Final = re.compile(
    r"^\| \[(?P<num>\d{4})\]\((?P<file>[^)]+)\) \| [^|]+ \| (?P<status>[^|]+?) \|", re.M
)


def adr_files() -> dict[str, Path]:
    return {p.name[:4]: p for p in sorted(ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))}


def file_status(path: Path) -> str:
    match = _STATUS.search(path.read_text(encoding="utf-8"))
    assert match, f"{path.name}: no **Status:** line"
    return match["status"].replace("[ADR-", "").replace("ADR-", "")


def index() -> tuple[dict[str, tuple[str, str]], str]:
    text = (ADR_DIR / "README.md").read_text(encoding="utf-8")
    main, _, pending = text.partition("## Needs team acceptance")
    rows: dict[str, tuple[str, str]] = {}
    for m in _ROW.finditer(main):
        assert m["num"] not in rows, f"ADR {m['num']} listed twice"
        rows[m["num"]] = (m["file"], m["status"].strip())
    return rows, pending.split("## How to write an ADR")[0]


def test_every_adr_file_has_one_row_pointing_at_it() -> None:
    files = adr_files()
    rows, _ = index()
    assert len(files) >= 23
    assert sorted(rows) == sorted(files)
    for num, (target, _) in rows.items():
        assert target == files[num].name, num


def test_index_status_equals_the_file_status() -> None:
    files = adr_files()
    rows, _ = index()
    for num, path in files.items():
        status = file_status(path)
        listed = rows[num][1]
        if status.startswith("Superseded"):
            assert listed.startswith("Superseded by "), (num, listed)
            assert listed.removeprefix("Superseded by ") in status, (
                num,
                status,
                listed,
            )
        else:
            assert listed == status, (num, status, listed)


def test_every_adr_is_final_or_listed_as_needing_team_acceptance() -> None:
    _, pending = index()
    pending_rows = set(re.findall(r"^\| \[(\d{4})\]\(", pending, re.M))
    for num, path in adr_files().items():
        status = file_status(path)
        if status.startswith(FINAL):
            # a final ADR may appear there only for a Proposed "Updates" section
            assert num not in pending_rows, (
                f"ADR-{num} is {status} but listed as pending"
            )
        else:
            assert status == "Proposed", (num, status)
            assert num in pending_rows, (
                f"ADR-{num} is {status} but not under 'Needs team acceptance'"
            )


def test_the_scope_adrs_are_flagged_for_the_team() -> None:
    """The lead's brief: ADR-0012 (the v1.0 scope) and ADR-0023 (the release
    rule) stay Proposed until the team records acceptance, listed plainly."""
    _, pending = index()
    for num in ("0012", "0023"):
        if file_status(adr_files()[num]) == "Proposed":
            assert f"| [{num}](" in pending, num
