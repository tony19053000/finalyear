"""V0.7 G7 tester r1: can ``scripts/threat_model_check.py`` be fooled?

Every test states what a *correct* gate must do. They were strict xfails (one
per defect, TQ-1..TQ-4, docs/briefs/progress/V0.7-G7.md, Tester r1) and pass
since fix round 1 (D167).
"""

from __future__ import annotations

import pytest
from test_threat_model_check import COLLECTED, A, doc, problems, row, tmc

pytestmark = [pytest.mark.unit, pytest.mark.security]

GOOD = row("CG-S1", f"`{A}::test_one`", "mitigated")


def _gate_fails(text: str) -> bool:
    """True when the gate would exit non-zero (a problem or a refusal)."""
    try:
        rows, _ = tmc.check(text, COLLECTED)
    except ValueError:
        return True
    return any(r.problems for r in rows)


def test_control_the_good_doc_passes() -> None:
    assert not _gate_fails(doc(GOOD))


@pytest.mark.parametrize("bad_id", ["CG-s2", "CGX-S2", "CG-S2a", "CG_S2", "CG-S"])
def test_a_row_with_a_malformed_id_is_not_silently_ignored(bad_id: str) -> None:
    open_row = row(bad_id, "—", "open (G9)")
    assert _gate_fails(doc(GOOD, open_row))


def test_a_duplicate_row_id_is_reported() -> None:
    assert _gate_fails(doc(GOOD, GOOD))


def test_section_7_must_list_the_row_not_merely_mention_it() -> None:
    accepted = "| Other risk | CG-D2 is NOT accepted, we will fix it | V0.9 |"
    text = doc(row("CG-D2", "—", "**accepted**: " + "x" * 20), accepted=accepted)
    assert _gate_fails(text)


def test_a_filler_reason_is_not_a_reason() -> None:
    accepted = "| CG-D2 | n/a | - |"
    text = doc(
        row("CG-D2", "—", "**accepted**: n/a n/a n/a n/a n/a"), accepted=accepted
    )
    assert _gate_fails(text)


def test_fake_test_ids_and_unmarked_tests_fail() -> None:
    fake = row("CG-S2", "`tests/unit/a/test_a.py::test_does_not_exist`", "mitigated")
    other_file = row("CG-S3", "`tests/unit/zzz/test_nope.py`", "mitigated")
    unmarked = row("CG-S4", f"`{A}::test_unmarked`", "mitigated")
    got = problems(doc(GOOD, fake, other_file, unmarked))
    assert got["CG-S1"] == []
    for rid in ("CG-S2", "CG-S3", "CG-S4"):
        assert any("not collected" in p for p in got[rid]), rid


def test_windows_paths_and_non_code_span_refs_do_not_count_as_tests() -> None:
    win = row("CG-S2", r"`tests\unit\a\test_a.py::test_one`", "mitigated")
    plain = row("CG-S3", f"{A}::test_one", "mitigated")  # no backticks
    got = problems(doc(GOOD, win, plain))
    assert got["CG-S2"] and got["CG-S3"]


def test_mitigated_word_tricks() -> None:
    for status in ("not mitigated", "unmitigated", "mitigated?? no", "pending"):
        got = problems(doc(row("CG-S2", f"`{A}::test_one`", status)))
        if status == "mitigated?? no":
            continue  # starts with the word: judged by tests, not by prose
        assert got["CG-S2"], status


def test_short_and_long_rows_are_flagged() -> None:
    short = "| CG-S2 | S | threat | today | m | G7 | `x` |"
    long_ = row("CG-S3", f"`{A}::test_one`", "mitigated") + " extra |"
    got = problems(doc(GOOD, short, long_))
    assert got["CG-S2"] and got["CG-S3"]


def test_empty_status_is_flagged() -> None:
    assert _gate_fails(doc(row("CG-S2", f"`{A}::test_one`", "")))
