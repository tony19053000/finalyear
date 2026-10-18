"""V0.9 G5 lane A items 3-4: cost metrics per configuration and the group label
(pure :func:`meshforge.controller.telemetry.labels.build_labels`).

best / tie / insufficient / pending, the CI-overlap rule, the latest attempt
per (config, repetition), unsupported configurations, the planner signal and
determinism (any input order -> the same labels).
"""

from __future__ import annotations

import datetime as dt
import random
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.telemetry import labels as lb
from meshforge.controller.telemetry.labels import PlannedRunState, build_labels
from meshforge.controller.telemetry.schema import COLUMN_NAMES

pytestmark = pytest.mark.unit

CAMP = "camp_01J0000000000000000000DDDD"
T0 = dt.datetime(2026, 10, 19, 9, 0, tzinfo=dt.UTC)
_SEQ = iter(range(1, 10**6))


def row(
    config: str,
    rep: int,
    total_ms: int | None,
    *,
    group: str = "g1",
    node: str | None = None,
    planner: str | None = None,
    at_s: int = 0,
    attempt_no: int = 1,
    **extra: Any,
) -> dict[str, Any]:
    n = next(_SEQ)
    out: dict[str, Any] = dict.fromkeys(COLUMN_NAMES)
    out.update(
        campaign_id=CAMP,
        group_id=group,
        config_id=config,
        repetition=rep,
        attempt_id=f"att_{n:06d}",
        attempt_no=attempt_no,
        attempt_created_at=T0 + dt.timedelta(seconds=at_s or n),
        node_id=node or f"node_{config}",
        strategy="mode1",
        total_ms=total_ms,
        planner_node_id=planner,
        failure_class="ok",
        workload_status="ok",
    )
    out.update(extra)
    return out


def runs(
    config: str,
    reps: int = 3,
    *,
    group: str = "g1",
    state: str = "done",
    recorded: bool = True,
    detail: str | None = None,
    node: str | None = None,
) -> list[PlannedRunState]:
    return [
        PlannedRunState(
            group_id=group,
            config_id=config,
            repetition=r,
            family="cpu.sort",
            params={"size": 1000},
            condition="none",
            strategy="mode1",
            node_id=node or f"node_{config}",
            origin_node_id="node_o",
            state=state,
            detail=detail,
            recorded=recorded,
        )
        for r in range(1, reps + 1)
    ]


def series(config: str, totals: Sequence[int], **kw: Any) -> list[dict[str, Any]]:
    return [row(config, i + 1, t, **kw) for i, t in enumerate(totals)]


def one(plan: list[PlannedRunState], rows: list[dict[str, Any]]) -> lb.GroupLabel:
    out = build_labels(CAMP, plan, rows)
    assert len(out.groups) == 1
    return out.groups[0]


def test_clear_winner_is_best_with_margin() -> None:
    plan = runs("a") + runs("b") + runs("c")
    rows = (
        series("a", [100, 102, 101])
        + series("b", [200, 205, 199])
        + series("c", [300, 301, 302])
    )
    g = one(plan, rows)
    assert g.label == "best"
    assert (g.best_config_id, g.best_node_id, g.best_strategy) == (
        "a",
        "node_a",
        "mode1",
    )
    assert g.margin_pct == pytest.approx((200 - 101) / 101 * 100)
    assert g.tied_config_ids == () and g.reason is None
    assert [c.config_id for c in g.configs] == ["a", "b", "c"]
    total = g.configs[0].metrics["total_ms"]
    assert total is not None and (total.n, total.median) == (3, 101.0)


def test_overlapping_cis_are_a_tie_with_the_tied_set() -> None:
    plan = runs("a") + runs("b") + runs("c")
    rows = (
        series("a", [100, 150, 200])
        + series("b", [140, 160, 180])  # inside a's [100, 200]
        + series("c", [500, 510, 520])  # clearly worse
    )
    g = one(plan, rows)
    assert g.label == "tie"
    assert g.tied_config_ids == ("a", "b")
    assert g.best_config_id is None and g.planner_agrees is None
    assert g.margin_pct == pytest.approx(10 / 150 * 100)  # b 160 vs a 150


def test_touching_cis_are_a_tie_strictly_below_is_best() -> None:
    plan = runs("a") + runs("b")
    touching = series("a", [90, 95, 100]) + series("b", [100, 110, 120])
    assert one(plan, touching).label == "tie"  # 100 is not < 100
    apart = series("a", [90, 95, 99]) + series("b", [100, 110, 120])
    assert one(plan, apart).label == "best"


def test_every_other_config_must_be_separated_not_only_the_runner_up() -> None:
    plan = runs("a") + runs("b") + runs("c")
    rows = (
        series("a", [100, 101, 102])
        + series("b", [149, 150, 151])  # runner-up by median, clearly apart
        + series("c", [50, 200, 350])  # higher median, but its CI reaches a's
    )
    g = one(plan, rows)
    assert g.label == "tie" and g.tied_config_ids == ("a", "c")


def test_equal_medians_are_a_tie() -> None:
    plan = runs("a") + runs("b")
    g = one(plan, series("a", [100, 100, 100]) + series("b", [100, 100, 100]))
    assert g.label == "tie" and g.tied_config_ids == ("a", "b")


def test_one_config_with_two_repetitions_is_insufficient() -> None:
    plan = runs("a") + runs("b") + runs("c")
    rows = (
        series("a", [100, 101, 102])
        + series("b", [200, 201, 202])
        + series("c", [300, 301])  # rep 3 failed (no ok record)
    )
    g = one(plan, rows)
    assert g.label == "insufficient"
    assert g.reason is not None and "c" in g.reason and "fewer than 3" in g.reason
    assert g.best_config_id is None


def test_a_planned_config_without_any_record_is_insufficient() -> None:
    plan = runs("a") + runs("b") + runs("c", state="failed")
    rows = series("a", [100, 101, 102]) + series("b", [200, 201, 202])
    g = one(plan, rows)
    assert g.label == "insufficient"
    c = next(x for x in g.configs if x.config_id == "c")
    assert c.repetitions == 0 and c.metrics["total_ms"] is None


def test_a_null_total_does_not_count_as_a_repetition_value() -> None:
    plan = runs("a") + runs("b")
    rows = series("a", [100, 101, 102]) + series("b", [200, 201]) + [row("b", 3, None)]
    g = one(plan, rows)
    assert g.label == "insufficient"
    b = next(x for x in g.configs if x.config_id == "b")
    assert b.repetitions == 3 and b.metrics["total_ms"].n == 2  # type: ignore[union-attr]


@pytest.mark.parametrize("state", ["planned", "submitted"])
def test_an_unfinished_run_makes_the_group_pending(state: str) -> None:
    plan = runs("a") + runs("b", 2) + runs("b", 3, state=state)[2:]
    rows = series("a", [100, 101, 102]) + series("b", [200, 201])
    g = one(plan, rows)
    assert g.label == "pending" and g.reason is not None


def test_an_ended_run_without_its_record_yet_is_pending() -> None:
    plan = runs("a") + runs("b", 2) + runs("b", 3, recorded=False)[2:]
    g = one(plan, series("a", [100, 101, 102]) + series("b", [200, 201]))
    assert g.label == "pending"


def test_pending_wins_over_insufficient() -> None:
    plan = runs("a", state="submitted")
    assert one(plan, []).label == "pending"


def test_unsupported_configs_are_reported_and_left_out() -> None:
    plan = (
        runs("a")
        + runs("b")
        + runs("c", state="skipped", recorded=False, detail="unsupported: no netem")
    )
    rows = series("a", [100, 101, 102]) + series("b", [200, 201, 202])
    g = one(plan, rows)
    assert g.label == "best" and g.best_config_id == "a"
    c = next(x for x in g.configs if x.config_id == "c")
    assert c.state == "unsupported" and c.repetitions == 0


def test_other_skips_still_count_as_planned() -> None:
    plan = runs("a") + runs("b", state="skipped", recorded=False, detail="cancelled")
    g = one(plan, series("a", [100, 101, 102]))
    assert g.label == "insufficient"


def test_a_group_no_config_can_run_is_insufficient() -> None:
    plan = runs("a", state="skipped", recorded=False, detail="unsupported: x")
    g = one(plan, [])
    assert g.label == "insufficient" and g.reason is not None


def test_a_single_config_group_is_best_without_margin() -> None:
    g = one(runs("a"), series("a", [100, 101, 102]))
    assert g.label == "best" and g.margin_pct is None


def test_latest_attempt_per_repetition_is_kept_whatever_the_order() -> None:
    old = row("a", 1, 999, at_s=10)
    new = row("a", 1, 100, at_s=20)
    rows = [new, old, row("a", 2, 101), row("a", 3, 102)]
    kept = lb.latest_per_repetition(rows)
    assert [r["total_ms"] for r in kept] == [100, 101, 102]
    assert lb.latest_per_repetition(list(reversed(rows))) == kept
    # same creation time -> higher attempt_no, then higher attempt id
    tie_a = row("b", 1, 1, at_s=5, attempt_no=1)
    tie_b = row("b", 1, 2, at_s=5, attempt_no=2)
    assert lb.latest_per_repetition([tie_b, tie_a])[0]["total_ms"] == 2
    assert lb.latest_per_repetition([tie_a, tie_b])[0]["total_ms"] == 2
    tie_c = row("c", 1, 3, at_s=5, attempt_no=1)
    tie_d = row("c", 1, 4, at_s=5, attempt_no=1)  # later attempt id
    assert lb.latest_per_repetition([tie_d, tie_c])[0]["total_ms"] == 4


def test_build_labels_applies_the_latest_rule_itself() -> None:
    plan = runs("a") + runs("b")
    rows = series("a", [100, 101, 102]) + series("b", [200, 201, 202])
    rows.append(row("a", 1, 5000, at_s=0))  # older retry (n small -> created first)
    rows[0]["attempt_created_at"] = T0 + dt.timedelta(days=1)
    g = one(plan, rows)
    a = g.configs[0]
    assert a.repetitions == 3 and a.metrics["total_ms"].median == 101.0  # type: ignore[union-attr]


def test_compute_per_iteration_and_bytes() -> None:
    r = row("a", 1, 10, compute_ns=9000, compute_iterations=3, bytes_in=5, bytes_out=7)
    assert lb.metric_value(r, "compute_per_iter_ns") == 3000.0
    assert lb.metric_value(r, "bytes") == 12
    assert (
        lb.metric_value({**r, "compute_iterations": None}, "compute_per_iter_ns")
        is None
    )
    assert lb.metric_value({**r, "compute_ns": None}, "compute_per_iter_ns") is None
    assert (
        lb.metric_value({**r, "compute_iterations": 0}, "compute_per_iter_ns") is None
    )
    assert lb.metric_value({**r, "bytes_out": None}, "bytes") is None
    assert lb.metric_value({**r, "total_ms": True}, "total_ms") is None
    with pytest.raises(ValueError):
        lb.metric_value(r, "nope")


def test_every_metric_is_summarised_per_config() -> None:
    rows = [
        row(
            "a",
            i,
            100 + i,
            compute_ns=1000 * i,
            compute_iterations=2,
            bytes_in=10,
            bytes_out=i,
        )
        for i in (1, 2, 3)
    ]
    g = one(runs("a"), rows)
    m = g.configs[0].metrics
    assert set(m) == set(lb.METRICS)
    assert m["compute_per_iter_ns"].median == 1000.0  # type: ignore[union-attr]
    assert m["bytes"].median == 12.0  # type: ignore[union-attr]
    assert m["total_ms"].cv is not None  # type: ignore[union-attr]


def test_planner_signal() -> None:
    plan = runs("a", node="n1") + runs("b", node="n2")
    agree = series("a", [100, 101, 102], node="n1", planner="n1") + series(
        "b", [200, 201, 202], node="n2", planner="n1"
    )
    g = one(plan, agree)
    assert g.planner_node_id == "n1" and g.planner_agrees is True
    assert [c.planner_node_id for c in g.configs] == ["n1", "n1"]
    disagree = series("a", [100, 101, 102], node="n1", planner="n2") + series(
        "b", [200, 201, 202], node="n2", planner="n2"
    )
    assert one(plan, disagree).planner_agrees is False
    unknown = series("a", [100, 101, 102], node="n1") + series(
        "b", [200, 201, 202], node="n2"
    )
    g = one(plan, unknown)
    assert g.planner_node_id is None and g.planner_agrees is None


def test_planner_mode_ties_break_to_the_smallest_id() -> None:
    rows = [
        row("a", 1, 1, planner="n2"),
        row("a", 2, 1, planner="n1"),
        row("a", 3, 1, planner=None),
    ]
    assert one(runs("a"), rows).planner_node_id == "n1"


def test_groups_are_independent_and_sorted() -> None:
    plan = runs("a", group="g2") + runs("b", group="g2") + runs("x", group="g1")
    rows = (
        series("a", [100, 101, 102], group="g2")
        + series("b", [100, 150, 200], group="g2")
        + series("x", [1, 2], group="g1")
    )
    out = build_labels(CAMP, plan, rows)
    assert [g.group_id for g in out.groups] == ["g1", "g2"]
    assert [g.label for g in out.groups] == ["insufficient", "tie"]
    assert out.counts == {"best": 0, "tie": 1, "insufficient": 1, "pending": 0}
    assert out.rows == 8


def test_any_input_order_gives_the_same_labels() -> None:
    plan = runs("a") + runs("b") + runs("c") + runs("d", group="g2")
    rows = (
        series("a", [100, 102, 101])
        + series("b", [130, 205, 199])
        + series("c", [300, 301, 302])
        + series("d", [1, 2, 3], group="g2")
    )
    first = build_labels(CAMP, plan, rows)
    rng = random.Random(7)  # noqa: S311 - seeded shuffle
    for _ in range(5):
        p, r = plan[:], rows[:]
        rng.shuffle(p)
        rng.shuffle(r)
        assert build_labels(CAMP, p, r) == first
