"""V0.14 G3 (D296, roadmap 0.14.4): the risk term.

* every failure type is classified explicitly (a new one fails here);
* the maths: prior only, many failures, counts out of range, a bad prior;
* Local / Mode 1: ``p_fail x (T + retry_overhead)`` with the best other
  single's queue + transfer + env; Mode 3: the sum over active lanes of
  ``p_fail x (chunk time + lease release)``;
* source ``history`` vs ``assumed``; no reliability inputs = no risk term
  (D307) - the estimate is exactly the pre-G3 one;
* the margin test compares risk-adjusted totals: an unreliable set loses.
"""

from __future__ import annotations

import dataclasses
import math

import pytest
from mf_planner_fabrics import synthetic_fabric

from meshforge.common.errors import FailureType
from meshforge.common.states import AttemptState
from meshforge.controller.planner import plan
from meshforge.controller.planner.risk import (
    END_CLASS,
    RISK_TERM,
    EndClass,
    classify_end,
    failure_types_of,
    lane_risk,
    p_fail,
    risked_singles,
    single_risk,
)
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Estimate,
    EstimateTerm,
    NodeSnapshot,
    Reliability,
    RiskConfig,
    TermSource,
)

pytestmark = pytest.mark.unit

CFG = RiskConfig()

# ---------------------------------------------------------------- the table

NODE_CAUSED = {
    FailureType.NODE_LOST,
    FailureType.GPU_SESSION_LOST,
    FailureType.OWNER_PREEMPTED,
    FailureType.DISK_FULL,
    FailureType.ENV_ERROR,
    FailureType.TRANSFER_ERROR,
}
WORKLOAD = {
    FailureType.USER_ERROR,
    FailureType.OOM,
    FailureType.VRAM_OOM,
    FailureType.TIMEOUT,
    FailureType.DEADLINE_EXCEEDED,
    FailureType.OUTPUT_ERROR,
    FailureType.COMBINE_ERROR,
    FailureType.INVALID_INPUT,
}


def test_every_failure_type_is_classified_explicitly() -> None:
    assert set(END_CLASS) == set(FailureType), "classify the new failure type"


@pytest.mark.parametrize("failure", list(FailureType), ids=lambda f: f.value)
def test_each_failure_type_has_the_intended_class(failure: FailureType) -> None:
    if failure in NODE_CAUSED:
        want = EndClass.NODE
    elif failure in WORKLOAD:
        want = EndClass.CLEAN  # the node did its part: never blamed
    else:
        want = EndClass.NOT_COUNTED
    assert END_CLASS[failure] is want
    assert classify_end(AttemptState.FAILED.value, failure.value) is want


def test_states_classify_without_a_failure_type() -> None:
    assert classify_end("done", None) is EndClass.CLEAN
    assert classify_end("lost", None) is EndClass.NODE
    assert classify_end("lost", "user_error") is EndClass.NODE  # lost is lost
    assert classify_end("failed", None) is EndClass.NOT_COUNTED
    assert classify_end("failed", "no_such_type") is EndClass.NOT_COUNTED
    assert classify_end("fenced", "fenced") is EndClass.NOT_COUNTED
    for live in ("assigned", "staging", "env_ready", "running", "uploading"):
        assert classify_end(live, None) is EndClass.NOT_COUNTED


def test_the_sql_filters_are_the_table() -> None:
    assert set(failure_types_of(EndClass.NODE)) == {f.value for f in NODE_CAUSED}
    assert set(failure_types_of(EndClass.CLEAN)) == {f.value for f in WORKLOAD}
    assert list(failure_types_of(EndClass.NODE)) == sorted(
        failure_types_of(EndClass.NODE)
    )


# ---------------------------------------------------------------- maths


def test_prior_only_is_five_percent() -> None:
    assert p_fail(Reliability(0, 0, 7.0), CFG) == pytest.approx(0.05)


def test_many_failures_and_many_successes() -> None:
    assert p_fail(Reliability(18, 20, 7.0), CFG) == pytest.approx(19 / 40)
    assert p_fail(Reliability(0, 980, 7.0), CFG) == pytest.approx(1 / 1000)
    assert p_fail(Reliability(100, 100, 7.0), CFG) == pytest.approx(101 / 120)
    custom = RiskConfig(prior=(2.0, 2.0))
    assert p_fail(Reliability(0, 0, 7.0), custom) == pytest.approx(0.5)


def test_counts_out_of_range_and_a_bad_prior_stay_in_bounds() -> None:
    assert p_fail(Reliability(-3, -1, 7.0), CFG) == pytest.approx(0.05)
    assert p_fail(Reliability(50, 10, 7.0), CFG) == pytest.approx(11 / 30)
    for prior in ((0.0, 19.0), (1.0, -1.0), (math.nan, 1.0), (math.inf, 1.0)):
        assert p_fail(Reliability(0, 0, 7.0), RiskConfig(prior=prior)) == pytest.approx(
            0.05
        )


def _node(node_id: str, reliability: Reliability | None) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=frozenset({"exec"}),
        reliability=reliability,
    )


def _estimate(queue: float, transfer: float, env: float, compute: float) -> Estimate:
    def term(seconds: float) -> EstimateTerm:
        return EstimateTerm(seconds, TermSource.MEASURED)

    return Estimate(term(queue), term(transfer), term(env), term(compute), term(0.0))


def test_single_risk_is_p_times_total_plus_retry_overhead() -> None:
    node = _node("a", Reliability(1, 10, 7.0))
    term = single_risk(node, 100.0, 30.0, CFG)
    assert term is not None
    assert term.seconds == pytest.approx((2 / 30) * 130.0)
    assert term.source is TermSource.HISTORY
    assert term.detail["p_fail"] == pytest.approx(2 / 30, abs=1e-6)
    assert term.detail["failures"] == 1 and term.detail["total"] == 10
    assert term.detail["window_days"] == 7.0
    assert term.detail["retry_overhead_s"] == 30.0
    prior_only = single_risk(_node("b", Reliability(0, 0, 7.0)), 100.0, 0.0, CFG)
    assert prior_only is not None and prior_only.source is TermSource.ASSUMED
    assert single_risk(_node("c", None), 100.0, 30.0, CFG) is None


def test_retry_overhead_is_the_best_other_singles_staging() -> None:
    a = _node("a", Reliability(0, 0, 7.0))
    b = _node("b", Reliability(0, 0, 7.0))
    c = _node("c", Reliability(0, 0, 7.0))
    fast = _estimate(0.0, 10.0, 5.0, 20.0)  # total 35, overhead 15
    mid = _estimate(0.0, 2.0, 1.0, 60.0)  # total 63, overhead 3
    slow = _estimate(0.0, 50.0, 50.0, 50.0)  # total 150, overhead 100
    out = risked_singles([(a, fast), (b, mid), (c, slow)], CFG)
    risk = [e.extra[RISK_TERM] for e in out]
    # a's retry would go to b (the best other); b's and c's to a
    assert risk[0].detail["retry_overhead_s"] == 3.0
    assert risk[1].detail["retry_overhead_s"] == 15.0
    assert risk[2].detail["retry_overhead_s"] == 15.0
    assert risk[0].seconds == pytest.approx(0.05 * (35.0 + 3.0))
    assert out[0].total_s == pytest.approx(35.0 + 0.05 * 38.0)
    lone = risked_singles([(a, fast)], CFG)[0].extra[RISK_TERM]
    assert lone.detail["retry_overhead_s"] == 35.0  # no other single: T


def test_no_reliability_means_no_risk_term_at_all() -> None:
    a, b = _node("a", None), _node("b", None)
    guess = _estimate(0.0, 1.0, 1.0, 10.0)
    out = risked_singles([(a, guess), (b, guess)], CFG)
    assert out == [guess, guess] and all(RISK_TERM not in e.extra for e in out)


def test_lane_risk_sums_over_lanes_with_the_lease_release() -> None:
    a = _node("a", Reliability(0, 0, 7.0))  # 0.05
    b = _node("b", Reliability(9, 20, 7.0))  # 10/40 = 0.25
    c = _node("c", None)  # unknown: adds nothing
    cfg = RiskConfig(lease_release_s=60.0)
    term = lane_risk([(a, 40.0), (b, 20.0), (c, 1000.0)], cfg)
    assert term is not None
    assert term.seconds == pytest.approx(0.05 * 100.0 + 0.25 * 80.0)
    assert term.source is TermSource.ASSUMED  # the weakest lane's source
    assert [row["node_id"] for row in term.detail["lanes"]] == ["a", "b"]
    assert term.detail["lease_release_s"] == 60.0
    assert lane_risk([(c, 10.0)], cfg) is None
    known = lane_risk([(b, 20.0)], cfg)
    assert known is not None and known.source is TermSource.HISTORY


# ---------------------------------------------------------------- in plan()


def _with_reliability(
    snapshot: ClusterSnapshot, by_node: dict[str, Reliability | None]
) -> ClusterSnapshot:
    return dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(n, reliability=by_node.get(n.node_id))
            for n in snapshot.nodes
        ),
    )


def test_a_whole_job_decision_carries_the_risk_term_last() -> None:
    request, snapshot, now = synthetic_fabric(4, split=False, seed=4)
    snap = _with_reliability(
        snapshot, {n.node_id: Reliability(0, 0, 7.0) for n in snapshot.nodes}
    )
    decision = plan(request, snap, "cost_model", now)
    for row in decision.candidates:
        assert row.estimate is not None
        assert list(row.estimate.terms())[-1] == RISK_TERM
        assert row.estimate.extra[RISK_TERM].source is TermSource.ASSUMED


def test_an_unreliable_node_loses_where_its_risk_outweighs_its_lead() -> None:
    flips = 0
    for seed in range(40):
        request, snapshot, now = synthetic_fabric(4, split=False, seed=seed)
        plain = plan(request, snapshot, "cost_model", now)
        assert plain.chosen is not None
        best = plain.chosen.node_id
        rel: dict[str, Reliability | None] = {
            n.node_id: Reliability(0, 50, 7.0) for n in snapshot.nodes
        }
        rel[best] = Reliability(50, 50, 7.0)  # lost its last 50 attempts
        risky = plan(request, _with_reliability(snapshot, rel), "cost_model", now)
        assert risky.chosen is not None and risky.chosen.estimate is not None
        # the pick is the lowest risk-adjusted total, whoever that is
        totals = {c.node_id: c.estimate.total_s for c in risky.candidates if c.estimate}
        assert risky.chosen.node_id == min(totals, key=lambda k: (totals[k], k))
        flips += risky.chosen.node_id != best
    assert flips >= 5, f"risk never changed a pick ({flips})"


def test_a_snapshot_without_reliability_decides_exactly_as_before_g3() -> None:
    for split in (False, True):
        request, snapshot, now = synthetic_fabric(5, split=split, seed=9)
        decision = plan(request, snapshot, "cost_model", now)
        assert all(
            RISK_TERM not in c.estimate.extra
            for c in decision.candidates
            if c.estimate is not None
        )


def test_the_margin_test_uses_risk_adjusted_totals() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=2)
    reliable = {n.node_id: Reliability(0, 200, 7.0) for n in snapshot.nodes}
    calm = plan(request, _with_reliability(snapshot, reliable), "cost_model", now)
    assert calm.split is not None and calm.split.split, "the fabric splits when safe"
    head = calm.split.lanes[0].node_id
    shaky = {n.node_id: Reliability(190, 200, 7.0) for n in snapshot.nodes}
    shaky[head] = Reliability(0, 200, 7.0)  # only the fastest lane is reliable
    risky = plan(request, _with_reliability(snapshot, shaky), "cost_model", now)
    assert risky.split is not None
    set_rows = [c for c in risky.candidates if c.mode == "split" and c.estimate]
    for row in set_rows:
        assert row.estimate is not None and RISK_TERM in row.estimate.extra
        assert row.margin is not None
        # the margin test reads the risk-adjusted total
        assert row.margin.set_s == pytest.approx(row.estimate.total_s, abs=1e-6)
    # every split set holds an unreliable lane: the risk outweighs the gain,
    # or the chosen plan is cheaper with its risk than without the other lanes
    if risky.split.split:
        assert set(risky.split.node_ids) != set(calm.split.node_ids) or (
            risky.split.predicted_makespan_s > calm.split.predicted_makespan_s
        )


def test_lane_risk_for_a_set_counts_only_active_lanes_and_their_chunk() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=2)
    rel = {n.node_id: Reliability(4, 40, 7.0) for n in snapshot.nodes}
    decision = plan(request, _with_reliability(snapshot, rel), "cost_model", now)
    for row in decision.candidates:
        if row.mode != "split" or row.estimate is None:
            continue
        risk = row.estimate.extra[RISK_TERM]
        active = {lane.node_id for lane in row.lanes if lane.active}
        assert {r["node_id"] for r in risk.detail["lanes"]} == active
        p = p_fail(Reliability(4, 40, 7.0), CFG)
        expected = sum(p * (r["chunk_s"] + 60.0) for r in risk.detail["lanes"])
        assert risk.seconds == pytest.approx(expected, rel=1e-5)
