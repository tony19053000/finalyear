"""V1.0 lab campaign (brief §4, D406-D408): ``lab/checklists/V1.0-campaign.md``
covers every ``lab`` row of ``release/acceptance.yaml``.

* every ``kind: lab`` row id appears in the checklist, and its row in the
  checklist's "Manifest lab rows" table names the same day (L0-L5) as the
  manifest's ``section`` - a new or moved lab row fails here until the
  checklist says which step produces it;
* the table lists no row the manifest does not have as ``lab``;
* every checklist, lab tool and script the campaign tells the team to run
  exists in the repository (a renamed file fails here, not in the lab).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Final

import pytest

REPO: Final = Path(__file__).resolve().parents[3]
if str(REPO / "scripts") not in sys.path:
    sys.path.insert(0, str(REPO / "scripts"))

import release_manifest as rm  # noqa: E402

pytestmark = pytest.mark.unit

CHECKLIST: Final = REPO / "lab" / "checklists" / "V1.0-campaign.md"
TABLE_ROW: Final = re.compile(
    r"^\|\s*`(V\d+\.\d+-c\d+)`\s*\|\s*Day (L\d)\b", re.MULTILINE
)
REPO_PATH: Final = re.compile(
    r"(?<![\w/.-])((?:lab|scripts|tests|docs|release)/[\w./-]+\.(?:md|py|sh|ps1|yaml))"
)


@pytest.fixture(scope="module")
def text() -> str:
    return CHECKLIST.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lab_rows() -> dict[str, rm.Row]:
    return {r.id: r for r in rm.load_manifest().rows if r.kind == "lab"}


def test_the_manifest_has_lab_rows(lab_rows: dict[str, rm.Row]) -> None:
    assert len(lab_rows) >= 20  # a broken loader must not make the next tests vacuous


def test_every_lab_row_id_is_referenced_in_the_campaign(
    text: str, lab_rows: dict[str, rm.Row]
) -> None:
    missing = sorted(i for i in lab_rows if f"`{i}`" not in text)
    assert not missing, f"lab rows with no step in {CHECKLIST.name}: {missing}"


def test_the_row_table_matches_the_manifest_sections(
    text: str, lab_rows: dict[str, rm.Row]
) -> None:
    table = {m.group(1): m.group(2) for m in TABLE_ROW.finditer(text)}
    assert set(table) == set(lab_rows), (
        f"table only: {sorted(set(table) - set(lab_rows))}; "
        f"manifest only: {sorted(set(lab_rows) - set(table))}"
    )
    wrong = {
        i: (day, lab_rows[i].section)
        for i, day in table.items()
        if day != lab_rows[i].section
    }
    assert not wrong, f"checklist day != manifest section: {wrong}"


def test_every_named_repository_file_exists(text: str) -> None:
    named = sorted({m.group(1).rstrip(".") for m in REPO_PATH.finditer(text)})
    assert named, "the pattern found no paths at all"
    missing = [p for p in named if "<" not in p and not (REPO / p).exists()]
    assert not missing, f"{CHECKLIST.name} names files that do not exist: {missing}"
