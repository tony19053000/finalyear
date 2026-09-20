"""Tester pass (0.2.10 follow-up): adversarial tests for
``spikes._harness.report.n_headline`` / ``render_report`` beyond the
implementer's own ``test_spike_report.py``.

Focus, per the tester brief:
  * garbage ``summary.n`` in a hand-edited result JSON (string / negative /
    huge / null) must never crash the report, and the headline sentence must
    stay true to what actually got counted;
  * ``MIN_VALID_REPS`` monkeypatched to another value must be reflected in the
    headline sentence (the table's ``INVALID (n<K)`` mark already uses the
    live constant; ``TEMPLATE.md``'s own "n < 5" prose is a separate, static
    contract that does *not* track the constant - documented, not "fixed");
  * exactly one row total, valid and invalid, singular vs plural wording;
  * ``**n = `` appears exactly once and the template tail is intact;
  * S1/S2/S3/S7-shaped envelopes (this repo currently has no real raw JSON for
    those spikes) still get a correct headline through the same function that
    renders every spike's report.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

import spikes._harness.report as report_mod
from spikes._harness.envelope import make_envelope
from spikes._harness.report import n_headline, read_template, render_report, result_rows
from spikes._harness.reps import CaseResult

NOW = datetime(2026, 9, 20, 9, 0, tzinfo=UTC)


def _env(
    host: str, values: list[float], *, link: str | None = "ethernet"
) -> dict[str, Any]:
    case = CaseResult(
        case_id="c1",
        label=f"case {host}",
        metrics={"metric_a": "ms"},
        samples=[{"metric_a": v} for v in values],
    )
    return make_envelope(
        spike="S1",
        topic="S1-network",
        link=link,
        params={},
        cases=[case],
        git={"commit": "abc1234", "dirty": False},
        host={"hostname": host, "os": "Linux", "os_release": "6.8"},
        now=NOW,
    )


def _render(envs: list[dict[str, Any]]) -> str:
    return render_report(
        read_template(),
        envs,
        topic="t",
        title="t",
        day="2026-09-20",
        task="0.2.x",
        spike="S1",
    )


def _n_line(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.startswith("Repetitions per data")]
    assert len(lines) == 1, lines
    return lines[0]


# --------------------------------------------------------------- garbage n
@pytest.mark.parametrize("garbage_n", ["five", -1, None])
def test_garbage_summary_n_becomes_invalid_zero_not_a_crash(garbage_n: object) -> None:
    """A hand-edited result file with a nonsensical ``n`` must never crash the
    report and must never be counted as a measurement."""
    env = _env("hostA", [1, 2, 3, 4, 5])
    env["cases"]["c1"]["summary"]["metric_a"]["n"] = garbage_n
    text = _render([env])
    assert "INVALID (n<5)" in text
    assert _n_line(text) == (
        "Repetitions per data point: **n = 0** (no valid rows; 1 row INVALID "
        "(n < 5), marked in the table)"
        " — any row with n < 5 must be marked INVALID with the reason under "
        '"Notes on outliers or failed runs".'
    )


def test_huge_summary_n_is_not_malformed_and_is_counted_as_measured() -> None:
    """``n`` has no declared upper bound: a very large but well-typed integer
    is a valid (if implausible) summary, not a malformed one. This pins the
    current behaviour so a future change is a deliberate decision."""
    env = _env("hostA", [1, 2, 3, 4, 5])
    env["cases"]["c1"]["summary"]["metric_a"]["n"] = 10**9
    text = _render([env])
    assert "**INVALID (n<5)**" not in text
    assert f"**n = {10**9}**" in _n_line(text)


def test_garbage_n_mixed_with_one_healthy_row_keeps_headline_true() -> None:
    """One row poisoned by a garbage ``n`` must not drag a healthy row's
    headline down (the exact defect class this function was rewritten for)."""
    good = _env("hostA", [1, 2, 3, 4, 5, 6])
    bad = _env("hostB", [1, 2, 3, 4, 5])
    bad["cases"]["c1"]["summary"]["metric_a"]["n"] = "garbage"
    text = _render([good, bad])
    assert _n_line(text) == (
        "Repetitions per data point: **n = 6** across the 1 valid row; "
        "1 row INVALID (n < 5), marked in the table"
        " — any row with n < 5 must be marked INVALID with the reason under "
        '"Notes on outliers or failed runs".'
    )


# ------------------------------------------------------- MIN_VALID_REPS
def test_min_valid_reps_monkeypatched_changes_the_headline_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline sentence must follow ``MIN_VALID_REPS`` (imported directly
    in ``report.py``, not a literal 5), while the template's own static "n < 5"
    prose is a separate contract that does not move with it."""
    monkeypatch.setattr(report_mod, "MIN_VALID_REPS", 3)
    env = _env("hostA", [1, 2, 3])
    text = _render([env])
    assert "**n = 3** (every row)" in text
    assert "INVALID (n<3)" not in text
    # the static template tail is untouched by the monkeypatch - by design.
    assert "any row with n < 5 must be marked INVALID" in text

    monkeypatch.setattr(report_mod, "MIN_VALID_REPS", 10)
    env2 = _env("hostA", [1, 2, 3, 4, 5])
    text2 = _render([env2])
    assert (
        "**n = 0** (no valid rows; 1 row INVALID (n < 10), marked in the table)"
    ) in text2
    assert "INVALID (n<10)" in text2


# --------------------------------------------------------- singular/plural
def test_single_row_invalid_uses_singular_row() -> None:
    text = _render([_env("hostA", [1, 2])])
    assert _n_line(text) == (
        "Repetitions per data point: **n = 0** (no valid rows; 1 row INVALID "
        "(n < 5), marked in the table)"
        " — any row with n < 5 must be marked INVALID with the reason under "
        '"Notes on outliers or failed runs".'
    )
    assert text.count("**INVALID (n<5)**") == 1


def test_single_row_valid_has_no_invalid_count_language() -> None:
    text = _render([_env("hostA", [1, 2, 3, 4, 5])])
    assert _n_line(text) == (
        "Repetitions per data point: **n = 5** (every row)"
        " — any row with n < 5 must be marked INVALID with the reason under "
        '"Notes on outliers or failed runs".'
    )
    # singular/plural wording is only exercised once >=1 row is invalid
    # alongside >=1 valid row; confirm that path explicitly with exactly one
    # of each.
    two = _render([_env("hostA", [1, 2, 3, 4, 5]), _env("hostB", [1, 2])])
    line = _n_line(two)
    assert "1 valid row;" in line and "1 row INVALID" in line
    assert "1 valid rows" not in line and "1 rows INVALID" not in line


# ---------------------------------------------------- headline uniqueness
def test_n_marker_appears_exactly_once_and_tail_is_intact() -> None:
    for envs in (
        [],
        [_env("hostA", [1, 2, 3, 4, 5])],
        [_env("hostA", [1, 2, 3, 4, 5, 6, 7]), _env("hostB", [1, 2, 3])],
    ):
        text = _render(envs)
        assert text.count("**n = ") == 1, envs
        assert text.count("Repetitions per data point:") == 1
        assert _n_line(text).endswith(
            " — any row with n < 5 must be marked INVALID with the reason under "
            '"Notes on outliers or failed runs".'
        )


# --------------------------------------------------- direct n_headline unit
def test_n_headline_pure_function_shapes() -> None:
    from spikes._harness.report import ResultRow
    from spikes._harness.stats import Summary

    def row(n: int) -> ResultRow:
        return ResultRow(
            case="c",
            metric="m",
            unit="",
            summary=Summary(n, None, None, None, None, None, None),
        )

    assert n_headline([]) == "**n = 0** (no rows)"
    assert n_headline([row(0)]) == (
        "**n = 0** (no valid rows; 1 row INVALID (n < 5), marked in the table)"
    )
    assert n_headline([row(0), row(1)]) == (
        "**n = 0** (no valid rows; 2 rows INVALID (n < 5), marked in the table)"
    )
    assert n_headline([row(5)]) == "**n = 5** (every row)"
    assert n_headline([row(5), row(5)]) == "**n = 5** (every row)"
    assert n_headline([row(5), row(7)]) == "**n = 5–7** (smallest: 5)"  # noqa: RUF001 (en dash range)
    assert (
        n_headline([row(5), row(0)])
        == "**n = 5** across the 1 valid row; 1 row INVALID (n < 5), marked in the table"
    )
    assert (
        n_headline([row(5), row(0), row(0)])
        == "**n = 5** across the 1 valid row; 2 rows INVALID (n < 5), marked in the table"
    )


# ------------------------------------------- other spikes' report shapes
_SPIKE_TOPICS = {
    "S1": "S1-network",
    "S2": "S2-reachability",
    "S3": "S3-transfer",
    "S7": "S7-benchmark",
}


@pytest.mark.parametrize("spike", ["S1", "S2", "S3", "S7"])
def test_other_spikes_render_correct_headline_through_the_shared_function(
    spike: str,
) -> None:
    """``n_headline`` renders every spike's report; a realistic envelope shape
    for each (S7 uses ``link=None`` like S6) must produce a truthful headline."""
    link = None if spike == "S7" else "wifi"
    case = CaseResult(
        case_id="k",
        label="k",
        metrics={"x": "ms"},
        samples=[{"x": v} for v in (1.0, 2.0, 3.0, 4.0, 5.0)],
    )
    env = make_envelope(
        spike=spike,
        topic=_SPIKE_TOPICS[spike],
        link=link,
        params={},
        cases=[case],
        git={"commit": "deadbee", "dirty": False},
        host={"hostname": "h1", "os": "Linux", "os_release": "6.8"},
        now=NOW,
    )
    text = render_report(
        read_template(),
        [env],
        topic=_SPIKE_TOPICS[spike],
        title=f"{spike} title",
        day="2026-09-20",
        task="0.2.x",
        spike=spike,
        author="tester",
    )
    assert _n_line(text) == (
        "Repetitions per data point: **n = 5** (every row)"
        " — any row with n < 5 must be marked INVALID with the reason under "
        '"Notes on outliers or failed runs".'
    )
    assert "**INVALID (n<5)**" not in text
    # sanity: result_rows used directly must agree with what got rendered
    rows = result_rows([env])
    assert len(rows) == 1 and rows[0].summary.n == 5
