"""V0.9 G5c (0.9.8, D220): the pure data quality report, ``meshforge.bench.quality``.

Every number on hand-built inputs: per configuration (planned / done / n,
total_ms + compute_per_iter_ns summaries, MAD outlier attempt ids, cv_flag,
reps_flag, missing fields), the summary (runs by state, the shared infra rate,
pre-emptions, condition / workload failures, fillers counted apart), the
criterion verdicts and ``pending``; edge cases from the brief's "try to break"
list; determinism; and the labels' medians for the same configuration.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import itertools
import random
from typing import Any

import pytest

from meshforge.bench.quality import (
    CRIT1_MAX_INFRA_RATE,
    REQUIRED_FIELDS,
    ROW_FIELDS,
    FailureCounts,
    QualityInput,
    build_quality,
    crit1_infra_ok,
    infra_failure_fraction,
    infra_failure_rate,
)
from meshforge.bench.records import latest_per_repetition
from meshforge.bench.stats import median

pytestmark = pytest.mark.unit

T0 = dt.datetime(2026, 10, 19, 12, 0, tzinfo=dt.UTC)


@dataclasses.dataclass(frozen=True)
class Run:
    group_id: str
    config_id: str
    repetition: int
    state: str = "done"
    family: str = "cpu.sort"
    condition: str = "none"
    strategy: str = "local"
    node_id: str = "node_a"
    detail: str | None = None

    @property
    def unsupported(self) -> bool:
        return self.state == "skipped" and (self.detail or "").startswith(
            "unsupported: "
        )


_seq = itertools.count(1)


def rec(
    config: str,
    rep: int,
    total: int | None = 100,
    *,
    group: str = "g1",
    ok: bool = True,
    role: str | None = None,
    failure_class: str = "ok",
    failure_type: str | None = None,
    attempt_no: int = 1,
    created: dt.datetime | None = None,
    **fields: Any,
) -> dict[str, Any]:
    n = next(_seq)
    row: dict[str, Any] = dict.fromkeys(ROW_FIELDS)
    row.update(
        attempt_id=f"att_{n:04d}",
        attempt_no=attempt_no,
        attempt_created_at=created or (T0 + dt.timedelta(seconds=n)),
        group_id=group,
        config_id=config,
        repetition=rep,
        failure_class=failure_class,
        failure_type=failure_type,
        workload_family="cpu.sort",
        condition="none",
        placement_mode="pinned",
        total_ms=total,
        compute_ns=1_000_000,
        compute_iterations=10,
        bytes_in=10,
        bytes_out=5,
        queue_ms=1,
        run_ms=50,
        node_state="online",
        node_cpu_percent=3.0,
        planner_node_id="node_a",
    )
    row.update(fields)
    row["label_row"] = ok and failure_class == "ok" and role is None
    row["bench_role"] = role
    return row


def feed(rows: list[dict[str, Any]]) -> QualityInput:
    data = QualityInput()
    for row in rows:
        data.add(row)
    return data


def counts_of(rows: list[dict[str, Any]]) -> FailureCounts:
    """What record_failure_counts returns for these rows (no bench_role)."""
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


def report(
    runs: list[Run], rows: list[dict[str, Any]], *, state: str = "done", **kw: Any
) -> Any:
    return build_quality(
        "camp_x",
        campaign_state=state,
        runs=runs,
        data=feed(rows),
        failures=counts_of(rows),
        **kw,
    )


def runs_for(config: str, reps: int, *, group: str = "g1", **kw: Any) -> list[Run]:
    return [Run(group, config, r, **kw) for r in range(reps)]


# ------------------------------------------------------------ fields


def test_required_and_row_fields_are_real_schema_columns() -> None:
    pytest.importorskip("sqlalchemy", reason="the schema lives in the controller")
    from meshforge.controller.telemetry.schema import COLUMN_NAMES

    assert set(REQUIRED_FIELDS) <= set(COLUMN_NAMES), set(REQUIRED_FIELDS) - set(
        COLUMN_NAMES
    )
    assert set(ROW_FIELDS) <= set(COLUMN_NAMES), set(ROW_FIELDS) - set(COLUMN_NAMES)


def test_the_field_check_fails_when_the_schema_drops_one() -> None:
    columns = {"total_ms", "compute_ns"}  # a schema that lost the others
    assert not set(REQUIRED_FIELDS) <= columns


# ------------------------------------------------------------ the rate


def test_infra_rate_excludes_preemptions() -> None:
    assert infra_failure_rate(FailureCounts(records=10, infra=1, preempted=5)) == 0.2
    assert infra_failure_rate(FailureCounts(records=3, infra=0, preempted=3)) is None
    assert infra_failure_rate(FailureCounts(records=0, infra=0, preempted=0)) is None
    assert infra_failure_rate(FailureCounts(records=3, infra=1, preempted=0)) == 0.3333


@pytest.mark.parametrize(
    ("records", "infra", "preempted", "ok"),
    [
        (100_000, 1_999, 0, True),  # 1.999 % - displayed 0.02, still < 2 %
        (50, 1, 0, False),  # exactly 2 %
        (100_000, 2_001, 0, False),  # 2.001 % - displayed 0.02
        (55, 1, 5, False),  # 1 / 50 after pre-emptions
        (56, 1, 5, True),  # 1 / 51
        (0, 0, 0, False),  # nothing counted: no evidence
        (4, 0, 4, False),
    ],
)
def test_criterion_one_uses_the_exact_fraction_not_the_display_rate(
    records: int, infra: int, preempted: int, ok: bool
) -> None:
    """G5c fix r1 (tester T-1): the verdict compares the exact fraction; the
    rounded rate is display only, from the same helper."""
    counts = FailureCounts(records=records, infra=infra, preempted=preempted)
    assert crit1_infra_ok(counts) is ok
    exact = infra_failure_fraction(counts)
    shown = infra_failure_rate(counts)
    assert (exact is None) is (shown is None)
    if exact is not None:
        assert shown == round(float(exact), 4)


def test_status_uses_the_same_rate_helper() -> None:
    pytest.importorskip("sqlalchemy")
    import inspect

    from meshforge.controller.bench import control

    source = inspect.getsource(control.CampaignControl.status)
    assert "infra_failure_rate(failures)" in source
    assert "record_failure_counts(db, campaign_id)" in source


# ------------------------------------------------------------ one seeded campaign


def _seeded() -> tuple[list[Run], list[dict[str, Any]]]:
    runs = (
        runs_for("clean", 3, node_id="node_a")
        + runs_for("noisy", 3, node_id="node_b", strategy="mode1")
        + runs_for("outlier", 7, node_id="node_c", strategy="mode1")
        + runs_for("short", 3, node_id="node_d")
    )
    rows = [
        rec("clean", 0, 100),
        rec("clean", 1, 101),
        rec("clean", 2, 102),
        rec("noisy", 0, 100),
        rec("noisy", 1, 200),
        rec("noisy", 2, 300),
        *(rec("outlier", r, 100 + r) for r in range(6)),
        rec("outlier", 6, 10_000),
        rec("short", 0, 100, compute_ns=None),  # a missing field
        rec("short", 1, 100, failure_class="infrastructure", failure_type="node_lost"),
        rec(
            "short",
            2,
            None,
            failure_class="infrastructure",
            failure_type="owner_preempted",
        ),
        rec("short", 1, 9_999_999, role="filler"),  # never counted
        rec("clean", 0, 1, role="warmup"),
        rec("short", 2, None, failure_class="workload", failure_type="user_error"),
        rec(
            "short",
            2,
            None,
            failure_class="condition_failed",
            condition="cpu_load:1",
        ),
    ]
    return runs, rows


def test_every_number_of_a_seeded_campaign() -> None:
    runs, rows = _seeded()
    q = report(runs, rows)
    by = {c.config_id: c for c in q.configs}
    assert [c.config_id for c in q.configs] == ["clean", "noisy", "outlier", "short"]

    clean = by["clean"]
    assert (clean.planned, clean.done, clean.n) == (3, 3, 3)
    assert clean.total_ms is not None and clean.total_ms.median == 101.0
    assert clean.total_ms.cv is not None and clean.total_ms.cv < 0.10
    assert not clean.cv_flag and not clean.reps_flag and not clean.flagged
    assert clean.compute_per_iter_ns is not None
    assert clean.compute_per_iter_ns.median == 100_000.0
    assert clean.records_checked == 3 and not any(clean.missing.values())

    noisy = by["noisy"]
    assert noisy.total_ms is not None and noisy.total_ms.cv == pytest.approx(0.5)
    assert noisy.cv_flag and noisy.flagged and not noisy.reps_flag

    outlier = by["outlier"]
    assert outlier.n == 7 and len(outlier.outlier_attempt_ids) == 1
    far = next(r for r in rows if r["total_ms"] == 10_000)
    assert outlier.outlier_attempt_ids == (far["attempt_id"],)
    assert outlier.total_ms is not None and outlier.total_ms.outliers == 1

    short = by["short"]
    assert short.n == 1 and short.reps_flag and short.flagged
    assert short.missing["compute_ns"] == 1 and short.records_checked == 1
    assert short.total_ms is not None and short.total_ms.median == 100.0  # no filler

    s = q.summary
    assert s.records == 18 and s.bench_role_records == {"filler": 1, "warmup": 1}
    assert s.infra_failures == 1 and s.owner_preempted == 1
    assert s.infra_failure_rate == round(1 / 17, 4)  # 18 - 1 pre-empted
    assert s.condition_failures == {"cpu_load:1": 1}
    assert s.workload_failures == {"cpu.sort": 1}
    assert s.runs_by_state == {
        "planned": 0,
        "submitted": 0,
        "done": 16,
        "failed": 0,
        "skipped": 0,
    }
    assert not s.pending
    assert s.crit1_infra_ok is False  # 5.9 % >= 2 %
    assert s.crit3_reps_ok is False  # "short" has n = 1
    assert s.configs == 4 and s.flagged_configs == 3


def test_the_threshold_is_a_setting() -> None:
    runs, rows = _seeded()
    q = report(runs, rows, cv_threshold=0.6)
    assert not {c.config_id: c for c in q.configs}["noisy"].cv_flag
    assert q.summary.cv_threshold == 0.6


def test_determinism_whatever_the_record_order() -> None:
    runs, rows = _seeded()
    first = report(runs, rows)
    for seed in range(5):
        shuffled = rows[:]
        random.Random(seed).shuffle(shuffled)  # noqa: S311 - test order only
        again = report(list(reversed(runs)), shuffled)
        assert again == first


# ------------------------------------------------------------ try to break


def test_empty_campaign() -> None:
    q = report([], [])
    assert q.configs == () and q.summary.records == 0
    assert q.summary.infra_failure_rate is None
    assert q.summary.crit1_infra_ok is False  # nothing proven
    assert q.summary.crit3_reps_ok is False
    open_q = report([], [], state="active")
    assert open_q.summary.pending
    assert (
        open_q.summary.crit1_infra_ok is None and open_q.summary.crit3_reps_ok is None
    )


def test_all_runs_failed() -> None:
    runs = runs_for("c", 3, state="failed")
    rows = [
        rec("c", r, None, failure_class="infrastructure", failure_type="node_lost")
        for r in range(3)
    ]
    q = report(runs, rows)
    c = q.configs[0]
    assert c.n == 0 and c.total_ms is None and c.reps_flag and c.done == 0
    assert c.records_checked == 0
    assert q.summary.infra_failure_rate == 1.0 and q.summary.crit1_infra_ok is False


@pytest.mark.parametrize("reps", [1, 2])
def test_one_or_two_repetitions_flag_reps(reps: int) -> None:
    runs = runs_for("c", reps)
    q = report(runs, [rec("c", r, 100 + r) for r in range(reps)])
    c = q.configs[0]
    assert c.n == reps and c.reps_flag and q.summary.crit3_reps_ok is False
    if reps == 1:
        assert c.total_ms is not None and c.total_ms.cv is None


def test_cv_with_mean_zero_is_null_never_a_flag() -> None:
    q = report(runs_for("c", 3), [rec("c", r, 0) for r in range(3)])
    c = q.configs[0]
    assert c.total_ms is not None and c.total_ms.cv is None and not c.cv_flag
    assert q.summary.crit3_reps_ok is False  # "with CV reported"


def test_non_finite_values_never_crash() -> None:
    rows = [rec("c", 0, 100), rec("c", 1, 101), rec("c", 2, 102)]
    rows[0]["compute_ns"] = float("nan")  # not an int: no compute value
    rows[1]["total_ms"] = float("inf")  # not an int: no total
    q = report(runs_for("c", 3), rows)
    c = q.configs[0]
    assert c.total_ms is not None and c.total_ms.n == 2
    assert c.compute_per_iter_ns is not None and c.compute_per_iter_ns.n == 2


def test_a_huge_filler_moves_no_median() -> None:
    runs = runs_for("c", 3)
    base = [rec("c", r, 100 + r) for r in range(3)]
    q1 = report(runs, base)
    fillers = [rec("c", 1, 10**12, role="filler", failure_class="ok")]
    q2 = report(runs, base + fillers)
    assert q1.configs == q2.configs
    assert q2.summary.bench_role_records == {"filler": 1}
    assert q2.summary.records == q1.summary.records


def test_retries_keep_the_latest_attempt_per_repetition() -> None:
    runs = runs_for("c", 3)
    rows = [
        rec("c", 0, 500, created=T0),
        rec("c", 0, 100, created=T0 + dt.timedelta(minutes=5), attempt_no=2),
        rec("c", 1, 101),
        rec("c", 2, 102),
    ]
    c = report(runs, rows).configs[0]
    assert c.n == 3 and c.total_ms is not None and c.total_ms.median == 101.0
    assert c.total_ms.n == 3 and c.records_checked == 4  # checked before dedup


def test_a_campaign_mid_run_is_pending() -> None:
    runs = [*runs_for("c", 2), Run("g1", "c", 2, state="submitted")]
    q = report(runs, [rec("c", r) for r in range(2)], state="active")
    assert q.summary.pending
    assert q.summary.crit1_infra_ok is None and q.summary.crit3_reps_ok is None
    assert q.summary.runs_by_state["submitted"] == 1


def test_planner_node_is_required_only_where_unpinned() -> None:
    rows = [
        rec("c", 0, planner_node_id=None),  # pinned: may be null
        rec("c", 1, planner_node_id=None, placement_mode="auto"),
        rec("c", 2),
    ]
    c = report(runs_for("c", 3), rows).configs[0]
    assert c.missing["planner_node_id"] == 1


def test_an_unsupported_config_is_not_flagged_nor_in_criterion_3() -> None:
    runs = runs_for("c", 3) + [
        Run("g1", "u", r, state="skipped", detail="unsupported: no tc")
        for r in range(3)
    ]
    q = report(runs, [rec("c", r, 100 + r) for r in range(3)])
    u = {c.config_id: c for c in q.configs}["u"]
    assert u.state == "unsupported" and not u.flagged and u.planned == 3
    assert q.summary.crit3_reps_ok is True


def test_clean_campaign_meets_both_criteria() -> None:
    runs = runs_for("a", 3) + runs_for("b", 3, node_id="node_b")
    rows = [rec("a", r, 100 + r) for r in range(3)] + [
        rec("b", r, 200 + r) for r in range(3)
    ]
    s = report(runs, rows).summary
    assert s.infra_failure_rate == 0.0 and s.infra_failure_rate < CRIT1_MAX_INFRA_RATE
    assert s.crit1_infra_ok is True and s.crit3_reps_ok is True


def test_ten_thousand_records_stay_fast_and_small() -> None:
    import time
    import tracemalloc

    runs = [Run(f"g{g}", f"c{g}", r) for g in range(100) for r in range(100)]
    tracemalloc.start()
    started = time.perf_counter()
    data = QualityInput()
    for g in range(100):
        for r in range(100):
            data.add(rec(f"c{g}", r, 100 + (r % 7), group=f"g{g}"))
    q = build_quality(
        "camp_x",
        campaign_state="done",
        runs=runs,
        data=data,
        failures=FailureCounts(10_000, 0, 0),
    )
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(q.configs) == 100 and all(c.n == 100 for c in q.configs)
    assert elapsed < 10.0, elapsed
    assert peak < 200 * 1024 * 1024, peak  # well under the export's budget


# ------------------------------------------------------------ = the labels


def test_report_medians_equal_the_labels_medians() -> None:
    pytest.importorskip("sqlalchemy")
    from meshforge.controller.telemetry.labels import PlannedRunState, build_labels

    runs, rows = _seeded()
    planned = [
        PlannedRunState(
            group_id=r.group_id,
            config_id=r.config_id,
            repetition=r.repetition,
            family=r.family,
            params={},
            condition=r.condition,
            strategy=r.strategy,
            node_id=r.node_id,
            origin_node_id="node_a",
            state=r.state,
            recorded=True,
        )
        for r in runs
    ]
    label_rows = [r for r in rows if r["label_row"]]
    labels = build_labels("camp_x", planned, label_rows)
    q = report(planned, rows)  # PlannedRunState satisfies the protocol
    by_label = {c.config_id: c for g in labels.groups for c in g.configs}
    for c in q.configs:
        lab = by_label[c.config_id].metrics["total_ms"]
        assert (lab is None) == (c.total_ms is None)
        if lab is not None and c.total_ms is not None:
            assert lab.median == c.total_ms.median
            assert (lab.ci_low, lab.ci_high, lab.cv) == (
                c.total_ms.ci_low,
                c.total_ms.ci_high,
                c.total_ms.cv,
            )
        assert by_label[c.config_id].repetitions == c.n


def test_latest_per_repetition_is_the_labels_function() -> None:
    pytest.importorskip("sqlalchemy")
    from meshforge.controller.telemetry import labels

    assert labels.latest_per_repetition is latest_per_repetition
    assert median([1, 2, 3]) == 2.0


def test_the_threshold_setting_defaults_to_the_report_default() -> None:
    pytest.importorskip("sqlalchemy")
    from meshforge.bench.quality import DEFAULT_CV_THRESHOLD
    from meshforge.controller.settings import BenchSettings

    assert BenchSettings().quality_cv_threshold == DEFAULT_CV_THRESHOLD == 0.10
    with pytest.raises(ValueError, match="quality_cv_threshold"):
        BenchSettings(quality_cv_threshold=0)
