"""V0.15 G4 step 3 (D336): Mode 3 plans with the ML-corrected lane costs.

A model that halves ``chunk_rate`` (seconds per item) on node B doubles B's
predicted item rate; chunk sizing, water filling and the margin test all use
it, so B's predicted share of the items grows (and B may join a set it was
left out of). A model that doubles B's ``lane_stage`` delays B's start the
same way the analytical model would. The combine correction moves the set's
total by exactly its own difference.
"""

from __future__ import annotations

import math

import pytest
from mf_ml_stub import Rule, stub_ml
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.planner import plan
from meshforge.controller.planner.replay import project
from meshforge.controller.planner.types import Decision, SplitPlan

pytestmark = pytest.mark.unit

B = "node_002"


def _inputs() -> tuple[object, object, object]:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=4)
    return request, project(request, snapshot), now


def _forced_inputs() -> tuple[object, object, object]:
    import dataclasses

    request, snapshot, now = synthetic_fabric(4, split=True, seed=4)
    request = dataclasses.replace(
        request, node_set=tuple(n.node_id for n in snapshot.nodes)
    )
    return request, project(request, snapshot), now


def _share(plan_: SplitPlan, node_id: str) -> float:
    total = sum(lane.rate_items_s for lane in plan_.lanes if lane.active)
    mine = next(
        (
            lane.rate_items_s
            for lane in plan_.lanes
            if lane.node_id == node_id and lane.active
        ),
        0.0,
    )
    return mine / total


def _decide(rules: dict[tuple[str, str | None], Rule], forced: bool = True) -> Decision:
    request, view, now = _forced_inputs() if forced else _inputs()
    ml, _ = stub_ml(rules)
    return plan(request, view, "cost_model", now, ml=ml)  # type: ignore[arg-type]


def test_halving_b_s_seconds_per_item_moves_chunk_share_to_b() -> None:
    zero = {("chunk_rate", None): Rule(y_hat=0.0)}
    halved = {**zero, ("chunk_rate", B): Rule(y_hat=math.log(0.5))}
    before = _decide(zero).split
    after = _decide(halved).split
    assert before is not None and after is not None and after.split
    rate_before = next(lane.rate_items_s for lane in before.lanes if lane.node_id == B)
    rate_after = next(lane.rate_items_s for lane in after.lanes if lane.node_id == B)
    assert rate_after == pytest.approx(2.0 * rate_before, rel=1e-5)
    assert _share(after, B) > _share(before, B) + 0.05
    assert after.predicted_makespan_s < before.predicted_makespan_s


def test_an_ml_faster_node_changes_the_auto_plan() -> None:
    """Without a forced set: making B ten times faster per item puts B in the
    chosen plan (it leads the fastest-first order)."""
    zero = {("chunk_rate", None): Rule(y_hat=0.0)}
    fast = {**zero, ("chunk_rate", B): Rule(y_hat=math.log(0.1))}
    before = _decide(zero, forced=False)
    after = _decide(fast, forced=False)
    assert after.split is not None and before.split is not None
    assert after.split.lanes[0].node_id == B or B in after.split.node_ids
    assert after.split.predicted_makespan_s < before.split.predicted_makespan_s


def test_a_slower_stage_delays_b_s_start() -> None:
    zero = {("lane_stage", None): Rule(y_hat=0.0)}
    slow = {**zero, ("lane_stage", B): Rule(y_hat=math.log(2.0))}
    before = _decide(zero).split
    after = _decide(slow).split
    assert before is not None and after is not None
    start_before = next(lane.start_s for lane in before.lanes if lane.node_id == B)
    start_after = next(lane.start_s for lane in after.lanes if lane.node_id == B)
    assert start_after > start_before


def test_the_combine_correction_moves_the_total_by_its_own_difference() -> None:
    zero = _decide({("combine", None): Rule(y_hat=0.0)})
    doubled = _decide({("combine", None): Rule(y_hat=math.log(2.0))})
    a, b = zero.chosen, doubled.chosen
    assert a is not None and b is not None and a.estimate and b.estimate
    ca = a.estimate.extra["combine"]
    cb = b.estimate.extra["combine"]
    assert cb.seconds == pytest.approx(2.0 * ca.seconds)
    assert cb.detail["ml"]["used"] is True
    assert cb.detail["ml"]["raw_s"] == round(ca.seconds, 6)
    # the risk term depends on lanes, not on the combine
    assert b.estimate.total_s - a.estimate.total_s == pytest.approx(
        cb.seconds - ca.seconds, rel=1e-9
    )
