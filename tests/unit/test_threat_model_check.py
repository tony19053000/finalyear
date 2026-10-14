"""Unit tests for ``scripts/threat_model_check.py`` (V0.7 G7, D166 (2)).

The script turns V0.7 criterion 5 ("every threat-model row is closed: a test
or an accepted reason") into a check. These tests pin each rule on small
synthetic threat models; the last test runs it on the real
``docs/THREAT_MODEL.md`` against a real ``pytest --collect-only -m security``,
so a stale test reference, an unmarked test or a reopened row fails the unit
tier.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "threat_model_check.py"


def load() -> ModuleType:
    """Import the script by path: ``scripts/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location("mf_threat_model_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up
    spec.loader.exec_module(module)
    return module


tmc = load()

HEAD = (
    "| ID | | Threat | Today | Mitigation | Grp | Test | Status |\n"
    "|---|---|---|---|---|---|---|---|\n"
)
A = "tests/unit/a/test_a.py"
COLLECTED = [
    f"{A}::test_one",
    f"{A}::test_two[x]",
    f"{A}::test_two[y]",
    f"{A}::TestGroup::test_inner",
    "tests/component/b/test_b.py::test_three",
]


def doc(*rows: str, accepted: str = "") -> str:
    return (
        "# T\n\n## 5. STRIDE\n\n### 5.1 B1\n\n"
        + HEAD
        + "\n".join(rows)
        + "\n\n## 6. Rules\n\n## 7. Accepted risks\n\n| Risk | Why | Revisit |\n|---|---|---|\n"
        + accepted
        + "\n"
    )


def row(row_id: str, test: str, status: str, mitigation: str = "m") -> str:
    return f"| {row_id} | S | threat | today | {mitigation} | G7 | {test} | {status} |"


def problems(text: str, collected: list[str] = COLLECTED) -> dict[str, list[str]]:
    rows, _ = tmc.check(text, collected)
    return {r.row_id: r.problems for r in rows}


def test_a_mitigated_row_with_collected_marked_tests_passes() -> None:
    text = doc(row("CG-S1", f"`{A}::test_one`, `::test_two`", "mitigated"))
    assert problems(text) == {"CG-S1": []}


def test_name_refs_inherit_the_previous_file_and_match_any_parametrisation() -> None:
    rows, found = tmc.check(
        doc(
            row(
                "CG-S1", f"`{A}::test_one` (`::test_two`, `::test_two[y]`)", "mitigated"
            )
        ),
        COLLECTED,
    )
    assert rows[0].refs == [f"{A}::test_one", f"{A}::test_two", f"{A}::test_two[y]"]
    assert all(found.values())


def test_whole_file_wildcard_and_class_refs() -> None:
    text = doc(
        row("CG-S1", f"`{A}`", "mitigated"),
        row("CG-S2", f"`{A}::test_t*`", "mitigated"),
        row("CG-S3", f"`{A}::TestGroup`", "mitigated"),
        row("CG-S4", f"`{A}::TestGroup::test_inner`", "mitigated"),
    )
    assert all(p == [] for p in problems(text).values())


def test_a_reference_that_is_not_collected_with_the_marker_fails() -> None:
    text = doc(row("CG-S1", f"`{A}::test_one`, `{A}::test_missing`", "mitigated"))
    assert problems(text)["CG-S1"] == [
        f"not collected with -m security: {A}::test_missing"
    ]
    # Unmarked = absent from the `-m security` collection: same failure.
    text = doc(row("CG-S1", "`tests/unit/z/test_z.py`", "mitigated"))
    assert "not collected" in problems(text)["CG-S1"][0]


def test_mitigated_needs_a_test() -> None:
    text = doc(row("CG-S1", "covered elsewhere", "mitigated"))
    assert problems(text)["CG-S1"] == ["`mitigated` without a test reference"]


@pytest.mark.parametrize(
    ("test", "status", "expected"),
    [
        (f"`{A}::test_one`", "open (G7)", "contains"),
        ("to be written: a test", "mitigated", "contains"),
        (f"`{A}::test_one`", "mitigated / open (G4 node renewal)", "contains"),
        ("—", "decision (G3)", "contains"),
        (f"`{A}::test_one`", "done", "status is neither"),
    ],
)
def test_open_rows_fail(test: str, status: str, expected: str) -> None:
    found = problems(doc(row("CG-S1", test, status)))["CG-S1"]
    assert any(p.startswith(expected) for p in found), found


def test_accepted_needs_a_reason_and_a_section_7_entry() -> None:
    reason = "**accepted**: LAN DoS is out of scope for a LAN fabric"
    assert problems(doc(row("MD-D1", "—", reason)))["MD-D1"] == [
        "`accepted` but MD-D1 is not named in §7"
    ]
    ok = doc(row("MD-D1", "—", reason), accepted="| spoofed mDNS (MD-D1) | x | - |")
    assert problems(ok) == {"MD-D1": []}
    # A bare word is no reason - unless the mitigation cell gives it.
    bare = doc(row("MD-D1", "—", "accepted"), accepted="| (MD-D1) | x | - |")
    assert problems(bare)["MD-D1"] == ["`accepted` without a reason"]
    in_mitigation = doc(
        row(
            "MD-D1", "—", "accepted", mitigation="**accepted**: mDNS is unauthenticated"
        ),
        accepted="| (MD-D1) | x | - |",
    )
    assert problems(in_mitigation) == {"MD-D1": []}


def test_an_accepted_row_may_cite_tests_but_they_must_be_collected() -> None:
    text = doc(
        row("EN-D2", f"`{A}::test_gone`", "**accepted**: a new code costs one command"),
        accepted="| lost reply (EN-D2) | x | - |",
    )
    assert problems(text)["EN-D2"] == [
        f"not collected with -m security: {A}::test_gone"
    ]


def test_mitigated_with_an_accepted_residual_needs_both() -> None:
    status = "mitigated; **accepted**: residual - a stolen key renews until revoked"
    text = doc(row("CG-E5", f"`{A}::test_one`", status))
    assert problems(text)["CG-E5"] == ["`accepted` but CG-E5 is not named in §7"]


def test_escaped_pipes_and_pipes_in_code_do_not_split_cells() -> None:
    text = doc(
        row(
            "LF-T4",
            f"`{A}::test_one` (`O_NOFOLLOW | O_NONBLOCK`)",
            "mitigated",
            mitigation="opens with `O_NOFOLLOW \\| O_NONBLOCK`",
        )
    )
    assert problems(text) == {"LF-T4": []}


def test_a_bare_file_name_must_be_written_as_a_path() -> None:
    text = doc(row("LF-I3", "`test_fabric_ca.py::test_x`", "mitigated"))
    found = problems(text)["LF-I3"]
    assert any("full path" in p for p in found)


def test_rows_outside_section_5_are_ignored_and_none_found_is_an_error() -> None:
    with pytest.raises(ValueError, match="no §5 rows"):
        tmc.check("# T\n\n## 4. Inventory\n\n| CG-S1 | a | b |\n", COLLECTED)


@pytest.mark.xfail(reason="rows still open until G7 deliverables 3-4", strict=True)
def test_the_real_threat_model_has_every_row_closed() -> None:
    """Criterion 5 of V0.7, mechanically: every row of docs/THREAT_MODEL.md is
    mitigated by a collected `security` test or accepted with a §7 reason."""
    assert tmc.main([]) == 0
