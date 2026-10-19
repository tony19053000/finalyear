"""V0.9 G5c tester r1: adversarial tests of the pure quality core, derived from
the brief section 5g "try to break" list and the 0.9.8 definitions - not from
the implementation. Self-contained (own row builder)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import math
import random
from typing import Any

import pytest

from meshforge.bench.quality import (
    CRIT1_MAX_INFRA_RATE,
    ROW_FIELDS,
    FailureCounts,
    QualityInput,
    build_quality,
    infra_failure_rate,
)
from meshforge.bench.records import latest_per_repetition, metric_value
from meshforge.bench.stats import median, summarize

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 19, tzinfo=dt.UTC)
_seq = itertools.count(1)


@dataclasses.dataclass(frozen=True)
class Run:
    group_id: str
    config_id: str
    repetition: int = 0
    state: str = "done"
    family: str = "cpu.sort"
    condition: str = "none"
    strategy: str = "local"
    node_id: str = "node_a"
    unsupported: bool = False


def rec(
    config: str, rep: int, total: Any = 100, *, group: str = "g", **kw: Any
) -> dict[str, Any]:
    n = next(_seq)
    row: dict[str, Any] = dict.fromkeys(ROW_FIELDS)
    row.update(
        attempt_id=f"att_{n:06d}",
        attempt_no=1,
        attempt_created_at=T0 + dt.timedelta(seconds=n),
        group_id=group,
        config_id=config,
        repetition=rep,
        failure_class="ok",
        workload_family="cpu.sort",
        condition="none",
        placement_mode="pinned",
        total_ms=total,
        compute_ns=1000,
        compute_iterations=10,
        bytes_in=1,
        bytes_out=1,
        queue_ms=1,
        run_ms=1,
        node_state="online",
        node_cpu_percent=1.0,
        planner_node_id="node_a",
        label_row=True,
        bench_role=None,
    )
    row.update(kw)
    return row


def counts(rows: list[dict[str, Any]]) -> FailureCounts:
    mine = [r for r in rows if r["bench_role"] is None]
    return FailureCounts(
        records=len(mine),
        infra=sum(
            1
            for r in mine
            if r["failure_class"] == "infrastructure"
            and r["failure_type"] != "owner_preempted"
        ),
        preempted=sum(1 for r in mine if r["failure_type"] == "owner_preempted"),
    )


def build(
    runs: list[Run], rows: list[dict[str, Any]], state: str = "done", **kw: Any
) -> Any:
    data = QualityInput()
    for r in rows:
        data.add(r)
    return build_quality(
        "camp_x",
        campaign_state=state,
        runs=runs,
        data=data,
        failures=counts(rows),
        **kw,
    )


def cfg(q: Any, config: str) -> Any:
    return next(c for c in q.configs if c.config_id == config)


def planned(config: str, n: int, **kw: Any) -> list[Run]:
    return [Run("g", config, i, **kw) for i in range(n)]


# --- empty / all failed / tiny n ------------------------------------------


@pytest.mark.parametrize("state", ["done", "cancelled", "active", "paused"])
def test_empty_campaign_every_state(state: str) -> None:
    q = build([], [], state)
    s = q.summary
    assert q.configs == () and s.records == 0 and s.infra_failure_rate is None
    if state in ("done", "cancelled"):
        assert s.crit1_infra_ok is False and s.crit3_reps_ok is False and not s.pending
    else:
        assert s.crit1_infra_ok is None and s.crit3_reps_ok is None and s.pending


def test_all_runs_failed_is_not_a_crash_and_fails_both_criteria() -> None:
    rows = [
        rec(
            "c",
            i,
            None,
            label_row=False,
            failure_class="infrastructure",
            failure_type="node_lost",
        )
        for i in range(5)
    ]
    q = build(planned("c", 5, state="failed"), rows)
    c = cfg(q, "c")
    assert c.n == 0 and c.total_ms is None and c.reps_flag and c.flagged
    assert q.summary.infra_failure_rate == 1.0
    assert q.summary.crit1_infra_ok is False and q.summary.crit3_reps_ok is False
    assert c.done == 0 and c.planned == 5


def test_all_failures_by_workload_are_not_infra() -> None:
    rows = [
        rec("c", i, None, label_row=False, failure_class="workload") for i in range(4)
    ]
    q = build(planned("c", 4, state="failed"), rows)
    assert q.summary.infra_failures == 0 and q.summary.infra_failure_rate == 0.0
    assert q.summary.workload_failures == {"cpu.sort": 4}
    assert q.summary.crit1_infra_ok is True and q.summary.crit3_reps_ok is False


@pytest.mark.parametrize("n", [1, 2, 3])
def test_small_n_flags_and_ci_do_not_crash(n: int) -> None:
    rows = [rec("c", i, 100 + i) for i in range(n)]
    c = cfg(build(planned("c", n), rows), "c")
    assert c.n == n and c.reps_flag is (n < 3)
    assert c.total_ms is not None
    if n == 1:
        assert c.total_ms.cv is None and not c.cv_flag
        assert c.total_ms.ci_low == c.total_ms.ci_high == 100.0


def test_n_counts_distinct_repetitions_not_records() -> None:
    rows = [rec("c", 0, 100), rec("c", 0, 100), rec("c", 0, 100), rec("c", 1, 100)]
    c = cfg(build(planned("c", 3), rows), "c")
    assert c.n == 2 and c.reps_flag


# --- CV / non-finite -----------------------------------------------------


def test_cv_with_mean_zero_is_none_not_inf_and_crit3_not_met() -> None:
    rows = [rec("c", i, 0) for i in range(3)]
    q = build(planned("c", 3), rows)
    c = cfg(q, "c")
    assert c.total_ms is not None and c.total_ms.cv is None
    assert not c.cv_flag
    assert q.summary.crit3_reps_ok is False  # "CV reported" cannot hold


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_values_never_crash_or_poison(bad: float) -> None:
    rows = [rec("c", 0, 100), rec("c", 1, 102), rec("c", 2, bad), rec("c", 3, 101)]
    for r in rows:
        r["compute_ns"] = bad if r["repetition"] == 1 else r["compute_ns"]
    q = build(planned("c", 4), rows)
    c = cfg(q, "c")
    assert c.total_ms is not None
    for v in (
        c.total_ms.median,
        c.total_ms.mean,
        c.total_ms.ci_low,
        c.total_ms.ci_high,
    ):
        assert math.isfinite(v)
    assert c.total_ms.cv is None or math.isfinite(c.total_ms.cv)


def test_non_finite_everywhere_for_a_config() -> None:
    rows = [rec("c", i, math.nan) for i in range(3)]
    q = build(planned("c", 3), rows)
    assert cfg(q, "c").total_ms is None
    assert q.summary.crit3_reps_ok is False


@pytest.mark.parametrize("cv", [0.0001, 0.1, 10.0])
def test_threshold_bounds_and_strictly_greater(cv: float) -> None:
    vals = [100, 110, 120]
    s = summarize(vals)
    assert s is not None and s.cv is not None
    rows = [rec("c", i, v) for i, v in enumerate(vals)]
    # exactly equal is not flagged; just below flags
    assert not cfg(build(planned("c", 3), rows, cv_threshold=s.cv), "c").cv_flag
    assert cfg(build(planned("c", 3), rows, cv_threshold=s.cv * 0.999), "c").cv_flag
    assert build(planned("c", 3), rows, cv_threshold=cv).summary.cv_threshold == cv


# --- fillers ------------------------------------------------------------


def test_filler_with_huge_total_changes_nothing_but_the_filler_count() -> None:
    base = [rec("c", i, 100 + i) for i in range(5)]
    fillers = [
        rec("c", 999, 10**15, bench_role="filler"),
        rec("c", 1, 10**15, bench_role="warmup"),  # same repetition as a real one
        rec(
            "c",
            2,
            None,
            bench_role="filler",
            label_row=False,
            failure_class="infrastructure",
            failure_type="x",
        ),
    ]
    a = build(planned("c", 5), base)
    b = build(planned("c", 5), base + fillers)
    ca, cb = cfg(a, "c"), cfg(b, "c")
    assert ca == cb
    assert b.summary.bench_role_records == {"filler": 2, "warmup": 1}
    assert b.summary.records == a.summary.records
    assert b.summary.infra_failures == 0 and b.summary.infra_failure_rate == 0.0
    assert cb.total_ms is not None and cb.total_ms.median == 102.0


def test_filler_only_campaign_has_no_config_stats() -> None:
    q = build([], [rec("c", 0, 10**12, bench_role="filler")])
    assert q.configs == () and q.summary.records == 0


def test_filler_with_label_row_true_is_still_excluded() -> None:
    r = rec("c", 0, 10**12, bench_role="filler", label_row=True)
    q = build(planned("c", 3), [rec("c", 0, 5), rec("c", 1, 5), rec("c", 2, 5), r])
    assert cfg(q, "c").total_ms.median == 5.0  # type: ignore[union-attr]


# --- retries -----------------------------------------------------------


def test_retries_use_latest_attempt_only_whatever_the_order() -> None:
    old = rec("c", 0, 9999, attempt_no=1, attempt_created_at=T0)
    new = rec("c", 0, 100, attempt_no=2, attempt_created_at=T0 + dt.timedelta(hours=1))
    other = [rec("c", 1, 100), rec("c", 2, 100)]
    for order in itertools.permutations([old, new, *other]):
        q = build(planned("c", 3), list(order))
        c = cfg(q, "c")
        assert c.n == 3 and c.total_ms.median == 100.0 and c.total_ms.mean == 100.0  # type: ignore[union-attr]
        assert c.outlier_attempt_ids == ()


def test_retry_with_identical_timestamps_is_deterministic() -> None:
    a = rec("c", 0, 1, attempt_no=1, attempt_created_at=T0)
    b = rec("c", 0, 2, attempt_no=1, attempt_created_at=T0)
    kept = {
        latest_per_repetition([a, b])[0]["attempt_id"],
        latest_per_repetition([b, a])[0]["attempt_id"],
    }
    assert len(kept) == 1


def test_infra_failure_followed_by_ok_retry_still_counts_the_failure() -> None:
    rows = [
        rec(
            "c",
            0,
            None,
            label_row=False,
            failure_class="infrastructure",
            failure_type="node_lost",
        ),
        rec("c", 0, 100, attempt_no=2),
        rec("c", 1, 100),
        rec("c", 2, 100),
    ]
    q = build(planned("c", 3), rows)
    assert q.summary.infra_failures == 1 and q.summary.records == 4
    assert cfg(q, "c").n == 3


# --- rate helper -------------------------------------------------------


def test_rate_matches_status_helper_and_excludes_preemptions() -> None:
    assert infra_failure_rate(FailureCounts(0, 0, 0)) is None
    assert infra_failure_rate(FailureCounts(5, 0, 5)) is None  # nothing counted
    assert infra_failure_rate(FailureCounts(10, 1, 5)) == 0.2


def test_criterion_one_boundary_is_strictly_below_two_percent() -> None:
    assert CRIT1_MAX_INFRA_RATE == 0.02
    ok_rows = [rec("c", i % 3, 100) for i in range(49)]
    bad = rec(
        "c", 0, None, label_row=False, failure_class="infrastructure", failure_type="x"
    )
    q = build(planned("c", 3), [*ok_rows, bad])  # exactly 1/50 = 2 %
    assert q.summary.infra_failure_rate == 0.02 and q.summary.crit1_infra_ok is False


@pytest.mark.xfail(
    strict=True,
    reason="tester r1 T-1: crit1 is decided on the rate rounded to 4 places "
    "(1.999 % -> 0.02 -> not < 0.02)",
)
def test_criterion_one_is_not_decided_on_a_rounded_rate() -> None:
    """1999 / 100000 = 1.999 % < 2 %: the criterion must hold. The rate is
    rounded to 4 places for display (0.02) - the verdict must not use it."""
    c = FailureCounts(records=100_000, infra=1_999, preempted=0)
    data = QualityInput()
    q = build_quality(
        "camp_x", campaign_state="done", runs=planned("c", 3), data=data, failures=c
    )
    assert CRIT1_MAX_INFRA_RATE > 1_999 / 100_000
    assert q.summary.crit1_infra_ok is True, q.summary.infra_failure_rate


# --- missing fields -------------------------------------------------------


def test_missing_counts_every_field_and_planner_only_when_unpinned() -> None:
    rows = [rec("c", i, 100) for i in range(3)]
    rows[0].update(bytes_out=None, planner_node_id=None)  # pinned: planner null is fine
    rows[1].update(placement_mode="auto", planner_node_id=None, queue_ms=None)
    c = cfg(build(planned("c", 3), rows), "c")
    assert c.missing["bytes_out"] == 1 and c.missing["queue_ms"] == 1
    assert c.missing["planner_node_id"] == 1
    assert c.flagged


def test_missing_field_in_a_superseded_retry_is_still_counted_but_stats_use_latest() -> (
    None
):
    old = rec("c", 0, 100, bytes_out=None, attempt_created_at=T0, attempt_no=1)
    new = rec("c", 0, 100, attempt_created_at=T0 + dt.timedelta(hours=1), attempt_no=2)
    c = cfg(build(planned("c", 3), [old, new, rec("c", 1), rec("c", 2)]), "c")
    assert c.records_checked == 4 and c.missing["bytes_out"] == 1


# --- determinism / structure --------------------------------------------


def test_order_and_shuffle_independence_whole_report() -> None:
    rows = []
    for ci, vals in enumerate([[100, 101, 99, 5000], [10, 10, 10], [7, 8]]):
        for rep, v in enumerate(vals):
            rows.append(rec(f"c{ci}", rep, v, group=f"g{ci % 2}"))
    runs = [Run(f"g{ci % 2}", f"c{ci}", i) for ci in range(3) for i in range(4)]
    ref = build(runs, rows)
    rng = random.Random(7)  # noqa: S311
    for _ in range(20):
        r2, ru2 = rows[:], runs[:]
        rng.shuffle(r2)
        rng.shuffle(ru2)
        assert build(ru2, r2) == ref
    assert [(c.group_id, c.config_id) for c in ref.configs] == sorted(
        (c.group_id, c.config_id) for c in ref.configs
    )


def test_same_config_id_in_two_groups_is_not_merged() -> None:
    rows = [rec("c", i, 100, group="g1") for i in range(3)] + [
        rec("c", i, 900, group="g2") for i in range(3)
    ]
    runs = [Run("g1", "c", i) for i in range(3)] + [Run("g2", "c", i) for i in range(3)]
    q = build(runs, rows)
    assert [(c.group_id, c.total_ms.median) for c in q.configs] == [
        ("g1", 100.0),
        ("g2", 900.0),
    ]  # type: ignore[union-attr]


def test_unsupported_config_is_neither_flagged_nor_in_criterion_three() -> None:
    runs = planned("u", 3, state="skipped", unsupported=True) + planned("c", 3)
    q = build(runs, [rec("c", i, 100) for i in range(3)])
    assert not cfg(q, "u").flagged and q.summary.crit3_reps_ok is True


def test_planned_config_with_no_records_yet_mid_run() -> None:
    runs = planned("c", 3, state="planned")
    q = build(runs, [], state="active")
    assert q.summary.pending and q.summary.runs_by_state["planned"] == 3
    assert cfg(q, "c").reps_flag and q.summary.crit3_reps_ok is None


def test_records_for_configs_not_in_planned_runs_still_reported() -> None:
    q = build([], [rec("ghost", i, 5) for i in range(3)])
    assert cfg(q, "ghost").n == 3 and cfg(q, "ghost").planned == 0


# --- medians == labels' (same code path), and scale -----------------------


def test_report_median_is_the_label_median_over_latest_per_repetition() -> None:
    rows = [rec("c", 0, 50, attempt_no=1, attempt_created_at=T0)]
    rows += [
        rec("c", 0, 100, attempt_no=2, attempt_created_at=T0 + dt.timedelta(seconds=5))
    ]
    rows += [
        rec("c", i, v) for i, v in zip((1, 2, 3, 4), (110, 120, 130, 7000), strict=True)
    ]
    c = cfg(build(planned("c", 5), rows), "c")
    kept = latest_per_repetition(rows)
    expected = median([metric_value(r, "total_ms") for r in kept])  # type: ignore[misc]
    assert c.total_ms.median == expected  # type: ignore[union-attr]
    assert c.compute_per_iter_ns.median == 100.0  # type: ignore[union-attr]


def test_outlier_ids_name_the_attempt() -> None:
    rows = [rec("c", i, v) for i, v in enumerate([100, 101, 99, 100, 102, 100000])]
    big = rows[-1]["attempt_id"]
    c = cfg(build(planned("c", 6), rows), "c")
    assert c.outlier_attempt_ids == (big,) and c.total_ms.outliers == 1  # type: ignore[union-attr]


def test_many_records_across_many_configs_is_fast() -> None:
    import time

    rows = [rec(f"c{i % 200}", i // 200, 100 + i % 7) for i in range(30_000)]
    runs = [Run("g", f"c{i % 200}", i // 200) for i in range(30_000)]
    t = time.monotonic()
    q = build(runs, rows)
    assert time.monotonic() - t < 15
    assert len(q.configs) == 200
