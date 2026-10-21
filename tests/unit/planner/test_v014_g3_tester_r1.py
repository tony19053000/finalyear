"""V0.14 G3 tester round 1: independent acceptance and property tests derived
from the brief (section 5c, D294-D296) - not from the implementation.

Criteria covered (test names say which):
risk monotone in failure rate; risk absent when reliability is absent
(risk_window_days = 0); classification covers every FailureType and the SQL
filters partition it; objectives change the pick only as specified and the
reason key moves only with the pick; unknown objective refused; manifest and
request hash unchanged when no objective; link_infeasible never for stale /
unmeasured / pinned / forced; pruning never removes a lane the water filling
would give work to; settings bounds.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import json
import math
import random
from typing import Any

import pytest
from mf_planner_fabrics import NOW, random_case, synthetic_fabric

from meshforge.common.errors import FailureType
from meshforge.common.manifest import OBJECTIVES, POLICIES, ManifestError
from meshforge.common.states import AttemptState
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.objectives import bytes_moved
from meshforge.controller.planner.risk import (
    END_CLASS,
    RISK_TERM,
    EndClass,
    classify_end,
    failure_types_of,
    lane_risk,
    p_fail,
)
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    ControllerLinkStat,
    History,
    LinkStat,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    RejectCode,
    Reliability,
    RiskConfig,
    SplitInfo,
    TreeNeed,
)

pytestmark = pytest.mark.unit

TREE = "sha256:" + "a" * 64
MIB = 1024 * 1024
CFG = RiskConfig()


# ====================================================================== risk


def _node(node_id: str, **kw: Any) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=frozenset({"exec", "exec-subprocess", "exec-split"}),
        cpu_slots=2,
        **kw,
    )


def _whole(
    seconds: dict[str, float],
    reliability: dict[str, Reliability | None],
    *,
    local: str | None = None,
    objective: str | None = None,
) -> tuple[PlanRequest, ClusterSnapshot]:
    request = PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        submitter_node_id=local,
        history=History(by_node_s=seconds, samples_by_node=dict.fromkeys(seconds, 3)),
        objective=objective,  # type: ignore[arg-type]
    )
    snapshot = ClusterSnapshot(
        nodes=tuple(_node(n, reliability=reliability.get(n)) for n in sorted(seconds)),
    )
    return request, snapshot


def _est(decision: Any, node: str) -> Any:
    return next(c for c in decision.candidates if c.node_id == node).estimate


def test_v014_g3_tester_r1_risk_is_monotone_in_the_failure_count() -> None:
    """More node-caused failures out of the same total never lowers p_fail,
    the risk term, or the node's predicted total - and strictly raises them."""
    last_p, last_risk, last_total = -1.0, -1.0, -1.0
    for failures in range(41):
        rel = Reliability(failures, 40, 7.0)
        p = p_fail(rel, CFG)
        request, snap = _whole({"a": 100.0, "b": 100.0}, {"a": rel, "b": None})
        est = _est(plan(request, snap, "cost_model", NOW), "a")
        risk = est.extra[RISK_TERM].seconds
        assert p > last_p and risk > last_risk and est.total_s > last_total
        last_p, last_risk, last_total = p, risk, est.total_s
        assert 0.0 < p < 1.0


def test_v014_g3_tester_r1_risk_falls_with_more_clean_history() -> None:
    last = math.inf
    for total in (0, 1, 5, 50, 500, 50_000):
        p = p_fail(Reliability(0, total, 7.0), CFG)
        assert p < last
        last = p
    # and rises with the prior's a, falls with its b
    base = p_fail(Reliability(2, 10, 7.0), RiskConfig(prior=(1.0, 19.0)))
    assert p_fail(Reliability(2, 10, 7.0), RiskConfig(prior=(5.0, 19.0))) > base
    assert p_fail(Reliability(2, 10, 7.0), RiskConfig(prior=(1.0, 60.0))) < base


def test_v014_g3_tester_r1_risk_is_monotone_for_hostile_counts() -> None:
    """failures > total, negatives, huge numbers: finite, in (0, 1), and
    monotone in failures (clamped), never an exception."""
    seen = [p_fail(Reliability(f, 10, 7.0), CFG) for f in (-5, 0, 3, 10, 11, 10**12)]
    assert all(0.0 < p < 1.0 for p in seen)
    assert seen == sorted(seen)
    big = p_fail(Reliability(10**15, 10**15, 7.0), CFG)
    assert 0.0 < big < 1.0 and math.isfinite(big)


def test_v014_g3_tester_r1_risk_is_off_without_reliability() -> None:
    """risk_window_days = 0 builds Reliability None: no `risk` key anywhere
    and the estimates are exactly those of a node never given the field."""
    secs = {"a": 70.0, "b": 50.0, "c": 90.0}
    request, snap = _whole(secs, {})
    decision = plan(request, snap, "cost_model", NOW)
    for cand in decision.candidates:
        assert cand.estimate is not None and RISK_TERM not in cand.estimate.extra
    assert decision.chosen is not None and decision.chosen.node_id == "b"
    # while reliability present adds the term on every candidate
    rel = {n: Reliability(0, 0, 7.0) for n in secs}
    _, with_risk = _whole(secs, rel)
    on = plan(request, with_risk, "cost_model", NOW)
    assert all(RISK_TERM in c.estimate.extra for c in on.candidates)  # type: ignore[union-attr]
    assert all(
        c.estimate.extra[RISK_TERM].source.value == "assumed"  # type: ignore[union-attr]
        for c in on.candidates
    )


def test_v014_g3_tester_r1_risk_off_decisions_equal_pre_g3_on_random_cases() -> None:
    """For random cases (no reliability), the risk machinery never alters a
    decision: stripping reliability that is None is a no-op, and no candidate
    carries a risk term."""
    for seed in range(120):
        request, snap, now = random_case(seed)
        decision = plan(request, snap, "cost_model", now)
        for cand in decision.candidates:
            if cand.estimate is not None:
                assert RISK_TERM not in cand.estimate.extra, seed


def test_v014_g3_tester_r1_assumed_vs_history_source() -> None:
    request, snap = _whole(
        {"a": 10.0, "b": 10.0},
        {"a": Reliability(0, 0, 7.0), "b": Reliability(1, 4, 7.0)},
    )
    decision = plan(request, snap, "cost_model", NOW)
    assert _est(decision, "a").extra[RISK_TERM].source.value == "assumed"
    assert _est(decision, "b").extra[RISK_TERM].source.value == "history"
    detail = _est(decision, "b").extra[RISK_TERM].detail
    for key in ("p_fail", "failures", "total", "window_days"):
        assert key in detail


def test_v014_g3_tester_r1_a_flaky_node_loses_a_close_race() -> None:
    request, snap = _whole(
        {"a": 100.0, "b": 101.0},
        {"a": Reliability(30, 30, 7.0), "b": Reliability(0, 30, 7.0)},
    )
    decision = plan(request, snap, "cost_model", NOW)
    assert decision.chosen is not None and decision.chosen.node_id == "b"
    plain, snap_plain = _whole({"a": 100.0, "b": 101.0}, {})
    assert plan(plain, snap_plain, "cost_model", NOW).chosen.node_id == "a"  # type: ignore[union-attr]


def test_v014_g3_tester_r1_single_candidate_has_a_finite_risk() -> None:
    """No other feasible single: retry overhead falls back to T, finite."""
    request, snap = _whole({"only": 50.0}, {"only": Reliability(5, 5, 7.0)})
    est = _est(plan(request, snap, "cost_model", NOW), "only")
    assert math.isfinite(est.total_s)
    assert est.extra[RISK_TERM].seconds == pytest.approx(
        p_fail(Reliability(5, 5, 7.0), CFG)
        * 2
        * (est.total_s - est.extra[RISK_TERM].seconds)
    )


def test_v014_g3_tester_r1_lane_risk_is_the_sum_and_monotone() -> None:
    nodes = [
        _node("a", reliability=Reliability(1, 10, 7.0)),
        _node("b", reliability=Reliability(0, 0, 7.0)),
        _node("c"),  # no reliability: contributes nothing
    ]
    lanes = [(nodes[0], 10.0), (nodes[1], 20.0), (nodes[2], 30.0)]
    cfg = RiskConfig(lease_release_s=7.0)
    term = lane_risk(lanes, cfg)
    assert term is not None
    want = sum(
        p_fail(n.reliability, cfg) * (s + 7.0)  # type: ignore[arg-type]
        for n, s in lanes
        if n.reliability is not None
    )
    assert term.seconds == pytest.approx(want)
    assert lane_risk([(nodes[2], 5.0)], cfg) is None
    assert lane_risk([], cfg) is None
    worse = lane_risk(
        [
            (dataclasses.replace(nodes[0], reliability=Reliability(8, 10, 7.0)), 10.0),
            *lanes[1:],
        ],
        cfg,
    )
    assert worse is not None and worse.seconds > term.seconds
    # longer lease release never lowers it
    longer = lane_risk(lanes, RiskConfig(lease_release_s=70.0))
    assert longer is not None and longer.seconds > term.seconds


def test_v014_g3_tester_r1_risk_hostile_numbers_stay_finite() -> None:
    for chunk in (0.0, float("nan"), float("inf"), -3.0, 1e300):
        term = lane_risk([(_node("a", reliability=Reliability(1, 2, 7.0)), chunk)], CFG)
        assert term is not None and math.isfinite(term.seconds) and term.seconds >= 0


# ====================================================================== classification


def test_v014_g3_tester_r1_every_failure_type_is_explicitly_classified() -> None:
    assert set(END_CLASS) == set(FailureType)
    for failure in FailureType:
        assert isinstance(END_CLASS[failure], EndClass)
        assert classify_end("failed", failure.value) is END_CLASS[failure]


def test_v014_g3_tester_r1_sql_filters_partition_the_table() -> None:
    node = set(failure_types_of(EndClass.NODE))
    clean = set(failure_types_of(EndClass.CLEAN))
    none = set(failure_types_of(EndClass.NOT_COUNTED))
    assert not (node & clean) and not (node & none) and not (clean & none)
    assert node | clean | none == {f.value for f in FailureType}


def test_v014_g3_tester_r1_workload_failures_never_blame_the_node() -> None:
    """Brief: never crash / the job's own oom / timeout / cancelled / fenced
    as node-caused."""
    for failure in (
        FailureType.USER_ERROR,
        FailureType.OOM,
        FailureType.TIMEOUT,
        FailureType.CANCELLED,
        FailureType.FENCED,
    ):
        assert classify_end("failed", failure.value) is not EndClass.NODE
    for failure in (
        FailureType.NODE_LOST,
        FailureType.GPU_SESSION_LOST,
        FailureType.OWNER_PREEMPTED,
        FailureType.DISK_FULL,
        FailureType.ENV_ERROR,
        FailureType.TRANSFER_ERROR,
    ):
        assert classify_end("failed", failure.value) is EndClass.NODE
    assert classify_end(AttemptState.LOST.value, None) is EndClass.NODE
    assert classify_end(AttemptState.FENCED.value, None) is EndClass.NOT_COUNTED
    assert classify_end(AttemptState.DONE.value, None) is EndClass.CLEAN


def test_v014_g3_tester_r1_every_attempt_state_is_classified_without_error() -> None:
    for state in AttemptState:
        for ftype in (None, *(f.value for f in FailureType), "bogus", ""):
            assert isinstance(classify_end(state.value, ftype), EndClass)


# ====================================================================== objectives


def _same_pick(a: Any, b: Any) -> bool:
    def key(d: Any) -> Any:
        if d.chosen is None:
            return None
        sp = d.split
        return (
            d.chosen.node_id,
            d.chosen.mode,
            d.chosen.node_set,
            None if sp is None else (sp.split, sp.node_ids),
        )

    return bool(key(a) == key(b))


def _decide(request: PlanRequest, snap: ClusterSnapshot, policy: str, now: Any) -> Any:
    try:
        return plan(request, snap, policy, now)
    except ValueError:  # not reached: objective names are valid here
        raise


@pytest.mark.parametrize("objective", ["min_network", "prefer_local"])
def test_v014_g3_tester_r1_reason_key_moves_only_with_the_pick(
    objective: str,
) -> None:
    """Over 250 random fabrics (whole + split): reason_key differs from the
    min_time decision's iff the pick differs; the candidates' feasibility
    never changes; a decision that cannot choose stays that way."""
    changed = unchanged = 0
    for seed in range(250):
        request, snap, now = random_case(seed)
        base = plan(request, snap, "cost_model", now)
        got = plan(
            dataclasses.replace(request, objective=objective),  # type: ignore[arg-type]
            snap,
            "cost_model",
            now,
        )
        assert sorted(
            (c.node_id, c.mode, c.feasible) for c in got.candidates
        ) == sorted((c.node_id, c.mode, c.feasible) for c in base.candidates), seed
        assert (got.chosen is None) == (base.chosen is None), seed
        same = _same_pick(base, got)
        assert (got.reason_key == base.reason_key) == same, (seed, objective)
        if base.chosen is not None:
            changed += not same
            unchanged += same
    assert changed > 0 and unchanged > 0, "the property never exercised both sides"


def test_v014_g3_tester_r1_min_time_and_absent_objective_are_the_same_decision() -> (
    None
):
    for seed in range(250):
        request, snap, now = random_case(seed)
        a = plan(request, snap, "cost_model", now)
        b = plan(
            dataclasses.replace(request, objective="min_time"), snap, "cost_model", now
        )
        assert candidates_json(a) == candidates_json(b), seed
        assert a.reason_key == b.reason_key
        assert _same_pick(a, b)


@pytest.mark.parametrize("policy", [p for p in POLICIES if p != "cost_model"])
def test_v014_g3_tester_r1_rule_policies_ignore_every_objective(policy: str) -> None:
    for seed in range(120):
        request, snap, now = random_case(seed)
        base = plan(request, snap, policy, now)
        for objective in OBJECTIVES:
            got = plan(
                dataclasses.replace(request, objective=objective),  # type: ignore[arg-type]
                snap,
                policy,
                now,
            )
            assert _same_pick(base, got), (seed, objective)
            assert got.reason_key == base.reason_key, (seed, objective)
            assert candidates_json(got) == candidates_json(base)
            assert got.objective == objective  # still recorded


def test_v014_g3_tester_r1_min_network_picks_minimal_bytes_for_whole_jobs() -> None:
    seen = 0
    for seed in range(300):
        request, snap, now = random_case(seed)
        if request.split is not None or request.pinned_node_id is not None:
            continue
        got = plan(
            dataclasses.replace(request, objective="min_network"),
            snap,
            "cost_model",
            now,
        )
        feasible = [c for c in got.candidates if c.feasible and c.estimate is not None]
        if got.chosen is None or not feasible:
            continue
        seen += 1
        moved = {c.node_id: bytes_moved(c.estimate) for c in feasible}  # type: ignore[arg-type]
        assert moved[got.chosen.node_id] == min(moved.values()), seed
        # the ranked order is non-decreasing in (bytes, time, id)
        ranked = sorted((c for c in feasible), key=lambda c: c.rank or 0)
        keys = [(moved[c.node_id], c.estimate.total_s, c.node_id) for c in ranked]  # type: ignore[union-attr]
        assert keys == sorted(keys), seed
    assert seen > 20


def test_v014_g3_tester_r1_prefer_local_obeys_the_margin_on_whole_jobs() -> None:
    other = local_kept = 0
    for seed in range(400):
        rng = random.Random(seed)  # noqa: S311
        secs = {f"n{i}": rng.uniform(5.0, 200.0) for i in range(rng.randint(2, 6))}
        rel = {
            n: Reliability(rng.randint(0, 5), rng.randint(5, 30), 7.0)
            for n in secs
            if rng.random() < 0.5
        }
        request, snap = _whole(secs, rel, local="n0", objective="prefer_local")
        got = plan(request, snap, "cost_model", NOW)
        feasible = {c.node_id: c for c in got.candidates if c.feasible}
        local = feasible["n0"]
        assert got.chosen is not None
        best = min(c.estimate.total_s for c in feasible.values())  # type: ignore[union-attr]
        if got.chosen.node_id == "n0":
            local_kept += 1
            # Local kept the job: nobody is more than 25 % faster
            assert best > 0.75 * local.estimate.total_s - 1e-9, seed  # type: ignore[union-attr]
        else:
            other += 1
            assert (
                got.chosen.estimate.total_s <= 0.75 * local.estimate.total_s + 1e-9
            ), seed  # type: ignore[union-attr]
            # and it is the min_time pick
            assert got.chosen.estimate.total_s == pytest.approx(best), seed  # type: ignore[union-attr]
    assert other > 20 and local_kept > 20


def test_v014_g3_tester_r1_prefer_local_ties_and_boundary_are_exact() -> None:
    for other, want in ((75.0, "b"), (75.0000001, "L"), (0.0001, "b"), (100.0, "L")):
        request, snap = _whole(
            {"L": 100.0, "b": other}, {}, local="L", objective="prefer_local"
        )
        got = plan(request, snap, "cost_model", NOW)
        assert got.chosen is not None and got.chosen.node_id == want, other


def test_v014_g3_tester_r1_prefer_local_uses_the_risk_adjusted_totals() -> None:
    """Local looks equal in raw history but is flaky: risk-adjusted, b is more
    than 25 % faster, so b wins; with no risk Local would keep the job."""
    secs = {"L": 100.0, "b": 90.0}
    request, snap = _whole(
        secs,
        {"L": Reliability(20, 20, 7.0), "b": Reliability(0, 20, 7.0)},
        local="L",
        objective="prefer_local",
    )
    got = plan(request, snap, "cost_model", NOW)
    assert got.chosen is not None and got.chosen.node_id == "b"
    request2, snap2 = _whole(secs, {}, local="L", objective="prefer_local")
    assert plan(request2, snap2, "cost_model", NOW).chosen.node_id == "L"  # type: ignore[union-attr]


def test_v014_g3_tester_r1_pinned_and_forced_jobs_ignore_the_objective() -> None:
    for seed in range(300):
        request, snap, now = random_case(seed)
        if request.pinned_node_id is None and not request.node_set:
            continue
        base = plan(request, snap, "cost_model", now)
        for objective in ("min_network", "prefer_local"):
            got = plan(
                dataclasses.replace(request, objective=objective),  # type: ignore[arg-type]
                snap,
                "cost_model",
                now,
            )
            assert _same_pick(base, got), (seed, objective)
            assert got.reason_key == base.reason_key


def test_v014_g3_tester_r1_unknown_objective_is_refused_for_all_policies() -> None:
    request, snap, now = random_case(3)
    for bad in ("fastest", "", "MIN_TIME", " min_time", "min_time ", "min-time"):
        for policy in POLICIES:
            with pytest.raises(ValueError, match="objective"):
                plan(dataclasses.replace(request, objective=bad), snap, policy, now)  # type: ignore[arg-type]


# ====================================================================== submit / manifest


def _base_manifest() -> dict[str, Any]:
    return {"name": "demo", "entrypoint": "python main.py"}


def test_v014_g3_tester_r1_manifest_canonical_form_unchanged_without_objective() -> (
    None
):
    from meshforge.common.manifest import parse_manifest_data

    plain = parse_manifest_data(_base_manifest())
    assert "objective" not in plain.canonical_dict()
    assert "objective" not in plain.canonical_json()
    explicit_none = parse_manifest_data({**_base_manifest(), "objective": None})
    assert explicit_none.canonical_json() == plain.canonical_json()
    with_it = parse_manifest_data({**_base_manifest(), "objective": "min_network"})
    assert with_it.canonical_dict()["objective"] == "min_network"
    assert with_it.canonical_json() != plain.canonical_json()
    # every other key is exactly what the no-objective form has
    rest = dict(with_it.canonical_dict())
    del rest["objective"]
    assert rest == plain.canonical_dict()


@pytest.mark.parametrize(
    "bad", ["fastest", "", "MIN_TIME", "min_time ", 3, True, ["min_time"], {"a": 1}]
)
def test_v014_g3_tester_r1_manifest_refuses_an_unknown_objective(bad: Any) -> None:
    from meshforge.common.manifest import parse_manifest_data

    with pytest.raises(ManifestError):
        parse_manifest_data({**_base_manifest(), "objective": bad})


@pytest.mark.parametrize("bad", ["fastest", "", "MIN_TIME", 3, True, ["x"]])
def test_v014_g3_tester_r1_request_refuses_an_unknown_objective(bad: Any) -> None:
    from pydantic import ValidationError

    from meshforge.controller.api.job_models import JobSubmission

    body = {
        "manifest": _base_manifest(),
        "bundle_digest": "sha256:" + "a" * 64,
        "placement": {"objective": bad},
    }
    with pytest.raises(ValidationError):
        JobSubmission.model_validate(body)
    body["placement"] = {"objective": "prefer_local"}
    assert JobSubmission.model_validate(body).placement.objective == "prefer_local"


def test_v014_g3_tester_r1_request_hash_is_unchanged_when_objective_absent() -> None:
    from meshforge.common.manifest import parse_manifest_data
    from meshforge.controller.api.job_models import Placement
    from meshforge.controller.api.job_routes import request_hash

    manifest = parse_manifest_data(_base_manifest()).canonical_dict()
    digest = "sha256:" + "c" * 64
    got = request_hash(manifest, digest, Placement(mode="auto"), tree_id=None)
    # the pre-G3 canonical request, written out by hand
    legacy = json.dumps(
        {
            "manifest": manifest,
            "bundle_digest": digest,
            "placement": {"mode": "auto"},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    assert got == hashlib.sha256(legacy.encode()).hexdigest()
    with_obj = request_hash(
        manifest, digest, Placement(mode="auto", objective="min_network")
    )
    assert with_obj != got
    # a pinned request too
    pinned = Placement(mode="offload", node_id="n_x", policy="cost_model")
    legacy_p = json.dumps(
        {
            "manifest": manifest,
            "bundle_digest": digest,
            "placement": {"mode": "offload", "node_id": "n_x", "policy": "cost_model"},
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    assert (
        request_hash(manifest, digest, pinned)
        == hashlib.sha256(legacy_p.encode()).hexdigest()
    )


# ====================================================================== link_infeasible


def _link_request(**kw: Any) -> PlanRequest:
    return PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=(TreeNeed(TREE, 800 * MIB),),
        **kw,
    )


def _link_snapshot(
    *, age_s: float | None, bw: float = 1e5, samples: int = 4, floor: float = 1e6
) -> ClusterSnapshot:
    links: dict[tuple[str, str], LinkStat] = {}
    if age_s is not None:
        links[("far", "holder")] = LinkStat(
            bw_bytes_s=bw,
            rtt_ms=2.0,
            samples=samples,
            measured_at=NOW - dt.timedelta(seconds=age_s),
        )
    return ClusterSnapshot(
        nodes=(
            _node("far"),
            _node("holder", cached=frozenset({TREE})),
        ),
        links=links,
        config=PlannerConfig(min_useful_bw_bytes_s=floor),
    )


def _codes(decision: Any) -> set[str]:
    return {c.rejection.code.value for c in decision.candidates if c.rejection}


def test_v014_g3_tester_r1_link_infeasible_fresh_vs_stale_vs_unmeasured() -> None:
    fresh = plan(_link_request(), _link_snapshot(age_s=10.0), "cost_model", NOW)
    assert RejectCode.LINK_INFEASIBLE.value in _codes(fresh)
    for label, snap in (
        ("stale", _link_snapshot(age_s=10_000.0)),
        ("unmeasured", _link_snapshot(age_s=None)),
        ("future-dated", _link_snapshot(age_s=-10_000.0)),
        ("floor off", _link_snapshot(age_s=10.0, floor=0.0)),
    ):
        got = plan(_link_request(), snap, "cost_model", NOW)
        assert RejectCode.LINK_INFEASIBLE.value not in _codes(got), label
        far = next(c for c in got.candidates if c.node_id == "far")
        assert far.feasible and far.estimate is not None, label


def test_v014_g3_tester_r1_link_infeasible_no_now_means_nothing_is_fresh() -> None:
    """now=None (no clock): a link's age is unknown, so it cannot be 'fresh'."""
    got = plan(_link_request(), _link_snapshot(age_s=1.0), "cost_model", None)
    assert RejectCode.LINK_INFEASIBLE.value not in _codes(got)


def test_v014_g3_tester_r1_link_infeasible_never_for_pinned_or_forced() -> None:
    snap = _link_snapshot(age_s=10.0)
    pinned = plan(_link_request(pinned_node_id="far"), snap, "cost_model", NOW)
    assert RejectCode.LINK_INFEASIBLE.value not in _codes(pinned)
    assert pinned.chosen is not None and pinned.chosen.node_id == "far"
    split = SplitInfo(items=100, item_bytes=100 * MIB, items_tree=TREE)
    forced = plan(
        _link_request(split=split, node_set=("far", "holder")),
        snap,
        "cost_model",
        NOW,
    )
    assert RejectCode.LINK_INFEASIBLE.value not in _codes(forced)
    # pinned + split too
    pinned_split = plan(
        _link_request(split=split, pinned_node_id="far"), snap, "cost_model", NOW
    )
    assert RejectCode.LINK_INFEASIBLE.value not in _codes(pinned_split)
    assert pinned_split.chosen is not None


def test_v014_g3_tester_r1_link_infeasible_is_live_never_a_permanent_failure() -> None:
    from meshforge.controller.planner.types import STATIC_CODES

    assert RejectCode.LINK_INFEASIBLE not in STATIC_CODES
    snap = _link_snapshot(age_s=10.0)
    holder = dataclasses.replace(snap.nodes[1], state="draining")
    snap = dataclasses.replace(snap, nodes=(snap.nodes[0], holder))
    decision = plan(_link_request(), snap, "cost_model", NOW)
    assert decision.chosen is None
    assert not decision.all_static_rejections  # the job waits; it never fails


def test_v014_g3_tester_r1_link_infeasible_property_on_random_fabrics() -> None:
    """With the floor on and min bytes 0: LINK_INFEASIBLE appears only for
    unpinned unforced jobs; removing every fresh probe link removes it; and
    it never changes which rejection codes are STATIC."""
    appeared = 0
    for seed in range(400):
        request, snap, now = random_case(seed)
        cfg = dataclasses.replace(
            snap.config,
            min_useful_bw_bytes_s=5e8,
            min_useful_bytes=0,
            default_bw_bytes_s=1e3,
        )
        snap = dataclasses.replace(snap, controller_links={})
        armed = dataclasses.replace(snap, config=cfg)
        got = plan(request, armed, "cost_model", now)
        hit = RejectCode.LINK_INFEASIBLE.value in _codes(got)
        appeared += hit
        if request.pinned_node_id is not None or request.node_set:
            assert not hit, seed
        # no links at all -> never
        bare = dataclasses.replace(armed, links={})
        assert RejectCode.LINK_INFEASIBLE.value not in _codes(
            plan(request, bare, "cost_model", now)
        ), seed
        # every link stale -> never
        old = {
            k: dataclasses.replace(v, measured_at=NOW - dt.timedelta(days=30))
            for k, v in snap.links.items()
        }
        stale = dataclasses.replace(armed, links=old)
        assert RejectCode.LINK_INFEASIBLE.value not in _codes(
            plan(request, stale, "cost_model", now)
        ), seed
        # floor off -> byte-identical to the unarmed decision
        off = dataclasses.replace(
            armed, config=dataclasses.replace(cfg, min_useful_bw_bytes_s=0.0)
        )
        unarmed = dataclasses.replace(
            snap, config=dataclasses.replace(snap.config, default_bw_bytes_s=1e3)
        )
        assert candidates_json(
            plan(request, off, "cost_model", now)
        ) == candidates_json(plan(request, unarmed, "cost_model", now)), seed
    assert appeared > 0, "the property never produced a link rejection"


def test_v014_g3_tester_r1_link_infeasible_faster_controller_path_never_rejects() -> (
    None
):
    """The estimator would fetch from the controller (faster) so no rejection."""
    snap = _link_snapshot(age_s=10.0)
    snap = dataclasses.replace(
        snap, controller_links={"far": ControllerLinkStat(bw_bytes_s=5e7, samples=5)}
    )
    got = plan(_link_request(), snap, "cost_model", NOW)
    assert RejectCode.LINK_INFEASIBLE.value not in _codes(got)


def test_v014_g3_tester_r1_link_infeasible_needs_more_than_min_useful_bytes() -> None:
    small = dataclasses.replace(
        _link_request(), trees=(TreeNeed(TREE, 64 * MIB),)
    )  # exactly the default 64 MiB: not "more than"
    got = plan(small, _link_snapshot(age_s=5.0), "cost_model", NOW)
    assert RejectCode.LINK_INFEASIBLE.value not in _codes(got)
    big = dataclasses.replace(small, trees=(TreeNeed(TREE, 64 * MIB + 1),))
    got = plan(big, _link_snapshot(age_s=5.0), "cost_model", NOW)
    assert RejectCode.LINK_INFEASIBLE.value in _codes(got)


# ====================================================================== pruning vs water filling


def _split_fabric(seed: int) -> tuple[PlanRequest, ClusterSnapshot]:
    rng = random.Random(seed)  # noqa: S311
    n = rng.randint(3, 7)
    ids = [f"node_{i:02d}" for i in range(n)]
    items = rng.choice((3, 8, 40, 200, 2000))
    nodes = tuple(
        _node(
            node_id,
            cpu_score=rng.choice((20.0, 100.0, 400.0, 900.0)),
            cached=frozenset({TREE}) if rng.random() < 0.4 else frozenset(),
            cpu_threads=8,
            ram_bytes=16 << 30,
        )
        for node_id in ids
    )
    links = {
        (a, b): LinkStat(
            bw_bytes_s=rng.choice((2e5, 2e6, 2e7, 2e8)),
            rtt_ms=2.0,
            samples=4,
            measured_at=NOW - dt.timedelta(seconds=30),
        )
        for a in ids
        for b in ids
        if a != b
    }
    rates = {
        i: rng.choice((0.2, 1.0, 5.0, 40.0, 300.0)) for i in ids if rng.random() < 0.7
    }
    history = History(
        by_node_s=dict.fromkeys(ids, 10.0),
        samples_by_node=dict.fromkeys(ids, 3),
        item_rate_by_node=rates,
        item_rate_samples_by_node=dict.fromkeys(rates, 4),
        chunk_overhead_s=rng.choice((None, 0.3, 3.0)),
        chunk_overhead_samples=3,
    )
    request = PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=(TreeNeed(TREE, rng.choice((10**6, 3 * 10**8, 3 * 10**9))),),
        submitter_node_id=ids[0],
        seeded=True,
        fingerprint="1" * 64,
        history=history,
        split=SplitInfo(
            items=items,
            item_bytes=rng.choice((None, 10**6, 10**9)),
            items_tree=TREE,
            chunk_items=rng.choice((None, 1, 10)),
        ),
    )
    return request, ClusterSnapshot(nodes=nodes, links=links)


def _with_pruning(snap: ClusterSnapshot, on: bool) -> ClusterSnapshot:
    return dataclasses.replace(
        snap, config=dataclasses.replace(snap.config, prune_dominated=on)
    )


def test_v014_g3_tester_r1_pruning_never_removes_a_lane_that_would_get_work() -> None:
    """Against the unpruned water filling: every node recorded as pruned is
    inactive in every unpruned candidate set that includes it; the chosen
    decision (nodes, split flag, predicted makespan) is unchanged; and the
    pruned run's rows are a subset of the unpruned run's."""
    pruned_seen = 0
    for seed in range(400):
        request, snap = _split_fabric(seed)
        plain = plan(request, _with_pruning(snap, False), "cost_model", NOW)
        cut = plan(request, _with_pruning(snap, True), "cost_model", NOW)
        for p in cut.pruned:
            pruned_seen += 1
            for cand in plain.candidates:
                if cand.mode != "split":
                    continue
                for lane in cand.lanes:
                    if lane.node_id == p.node_id:
                        assert not lane.active, (seed, p, cand.node_set)
        # a pruned node is still a candidate on its own (single row kept)
        for p in cut.pruned:
            assert any(
                c.node_id == p.node_id and c.mode != "split" for c in cut.candidates
            )
        assert p_ok(cut)
    assert pruned_seen > 0, "no seed ever pruned: the property is vacuous"


def _active_pick(d: Any) -> Any:
    sp = d.split
    if sp is None or d.chosen is None:
        return None if d.chosen is None else (d.chosen.mode, d.chosen.node_id)
    return (sp.split, frozenset(ln.node_id for ln in sp.lanes if ln.active))


def test_v014_g3_tester_r1_pruning_keeps_the_same_workers_for_the_chosen_plan() -> None:
    """Pruning may only drop idle lanes: the nodes that work, and whether the
    job splits, never change."""
    seen = 0
    for seed in range(600):
        request, snap = _split_fabric(seed)
        plain = plan(request, _with_pruning(snap, False), "cost_model", NOW)
        cut = plan(request, _with_pruning(snap, True), "cost_model", NOW)
        seen += bool(cut.pruned)
        assert _active_pick(plain) == _active_pick(cut), seed
    assert seen > 50


def test_v014_g3_tester_r1_pruning_does_not_worsen_the_predicted_makespan() -> None:
    """Brief 5c.5: a decision may change only where pruning removed a strictly
    dominated set. A set that has an idle lane and a *lower* prediction is not
    dominated. DEFECT: seed 57 - dropping the idle node_01 changes the auto
    chunk size (3 -> 5 items) and the chosen plan's predicted makespan rises
    129.76 s -> 134.05 s."""
    worse: list[tuple[int, float, float]] = []
    for seed in range(600):
        request, snap = _split_fabric(seed)
        plain = plan(request, _with_pruning(snap, False), "cost_model", NOW)
        cut = plan(request, _with_pruning(snap, True), "cost_model", NOW)
        if plain.split is None or cut.split is None:
            continue
        a, b = plain.split.predicted_makespan_s, cut.split.predicted_makespan_s
        if b > a * (1 + 1e-9):
            worse.append((seed, a, b))
    assert not worse, worse


def p_ok(decision: Any) -> bool:
    """`by` names a node that is not itself pruned; no self-pruning."""
    names = {p.node_id for p in decision.pruned}
    return all(p.by not in names and p.by != p.node_id for p in decision.pruned)


def test_v014_g3_tester_r1_pruning_is_off_by_default_and_deterministic() -> None:
    request, snap = _split_fabric(7)
    a = plan(request, snap, "cost_model", NOW)
    assert a.pruned == ()
    on = _with_pruning(snap, True)
    first = plan(request, on, "cost_model", NOW)
    second = plan(request, on, "cost_model", NOW)
    assert first.pruned == second.pruned
    assert candidates_json(first) == candidates_json(second)
    # permuting node order changes nothing
    shuffled = dataclasses.replace(on, nodes=tuple(reversed(on.nodes)))
    third = plan(request, shuffled, "cost_model", NOW)
    assert third.pruned == first.pruned


def test_v014_g3_tester_r1_identical_nodes_are_never_pruned() -> None:
    ids = [f"n{i}" for i in range(5)]
    nodes = tuple(_node(i, cpu_score=300.0) for i in ids)
    request = PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        history=History(
            by_node_s=dict.fromkeys(ids, 10.0), samples_by_node=dict.fromkeys(ids, 3)
        ),
        split=SplitInfo(items=500),
    )
    snap = _with_pruning(ClusterSnapshot(nodes=nodes), True)
    assert plan(request, snap, "cost_model", NOW).pruned == ()


def test_v014_g3_tester_r1_pruned_decisions_round_trip_through_replay() -> None:
    from meshforge.controller.planner.replay import from_replay, to_replay

    seen = 0
    for seed in range(120):
        request, snap = _split_fabric(seed)
        snap = _with_pruning(snap, True)
        decision = plan(request, snap, "cost_model", NOW)
        replay = to_replay(request, snap, NOW)
        r_request, r_snap, r_now = from_replay(json.loads(json.dumps(replay)))
        again = plan(r_request, r_snap, "cost_model", r_now)
        assert candidates_json(again) == candidates_json(decision), seed
        assert (
            again.pruned == decision.pruned and again.reason_key == decision.reason_key
        )
        seen += bool(decision.pruned)
    assert seen > 0


# ====================================================================== replays / old data


def test_v014_g3_tester_r1_a_reliability_snapshot_round_trips_through_replay() -> None:
    from meshforge.controller.planner.replay import from_replay, to_replay

    for seed in range(60):
        request, snap, now = random_case(seed)
        rng = random.Random(seed)  # noqa: S311
        nodes = tuple(
            dataclasses.replace(
                n, reliability=Reliability(rng.randint(0, 3), rng.randint(3, 30), 7.0)
            )
            if rng.random() < 0.7
            else n
            for n in snap.nodes
        )
        snap = dataclasses.replace(snap, nodes=nodes)
        request = dataclasses.replace(
            request, objective=rng.choice([None, *OBJECTIVES])
        )  # type: ignore[arg-type]
        if now is None:
            now = NOW
        before = plan(request, snap, "cost_model", now)
        replay = to_replay(request, snap, now)
        r_request, r_snap, r_now = from_replay(json.loads(json.dumps(replay)))
        after = plan(r_request, r_snap, "cost_model", r_now)
        assert candidates_json(after) == candidates_json(before), seed
        assert after.reason_key == before.reason_key
        assert after.objective == before.objective


_G3_KEYS = frozenset(
    {
        "reliability",
        "objective",
        "prune_dominated",
        "risk",
        "prefer_local_margin",
        "min_useful_bw_bytes_s",
        "min_useful_bytes",
    }
)


def _strip(node: Any) -> None:
    """Delete every G3 key (what a replay stored before G3 lacks)."""
    if isinstance(node, dict):
        for key in list(node):
            if key in _G3_KEYS:
                del node[key]
            else:
                _strip(node[key])
    elif isinstance(node, list):
        for item in node:
            _strip(item)


def _decode(from_replay: Any, replay: Any) -> Any:
    return from_replay(json.loads(json.dumps(replay)))


def test_v014_g3_tester_r1_replay_without_g3_fields_still_decodes_to_the_old_decision() -> (
    None
):
    """A v1 replay stored before G3 has no reliability / objective / G3 config
    keys; stripping them from a fresh replay must decode and replay to the same
    decision as the pre-G3 defaults (risk, link floor, pruning all off)."""
    from meshforge.controller.planner.replay import from_replay, to_replay

    for seed in range(80):
        request, snap, now = random_case(seed)
        now = now or NOW
        before = plan(request, snap, "cost_model", now)
        replay = json.loads(json.dumps(to_replay(request, snap, now)))
        plain = plan(
            *_decode(from_replay, replay)[:2], "cost_model", NOW if now is NOW else now
        )
        _strip(replay)
        r_request, r_snap, r_now = from_replay(replay)
        after = plan(r_request, r_snap, "cost_model", r_now)
        assert candidates_json(after) == candidates_json(before), seed
        assert after.snapshot_digest == plain.snapshot_digest, seed


def test_v014_g3_tester_r1_snapshot_digest_ignores_unset_g3_inputs() -> None:
    """The digest of a pre-G3-shaped case must not move because G3 added
    fields (old stored digests stay valid)."""
    request, snap, now = synthetic_fabric(4, split=False)
    base = plan(request, snap, "cost_model", now).snapshot_digest
    again = plan(
        dataclasses.replace(request, objective=None), snap, "cost_model", now
    ).snapshot_digest
    assert base == again
    armed = dataclasses.replace(
        snap,
        nodes=tuple(
            dataclasses.replace(n, reliability=Reliability(0, 0, 7.0))
            for n in snap.nodes
        ),
    )
    assert plan(request, armed, "cost_model", now).snapshot_digest != base


@pytest.mark.parametrize("bad", ["fastest", "", "MIN_TIME", 3, None.__class__.__name__])
def test_v014_g3_tester_r1_submit_parse_answers_invalid_input_for_a_bad_objective(
    bad: Any,
) -> None:
    """Brief says 422; the API's convention is the typed ``invalid_input`` error
    (HTTP 400), the same as an unknown policy - the code is what clients read."""
    from meshforge.controller.api.errors import ApiError
    from meshforge.controller.api.job_routes import _parse_submission

    where_placement = json.dumps(
        {
            "manifest": _base_manifest(),
            "bundle_digest": "sha256:" + "a" * 64,
            "placement": {"objective": bad},
        }
    ).encode()
    with pytest.raises(ApiError) as caught:
        _parse_submission(where_placement)
    assert caught.value.failure_type is FailureType.INVALID_INPUT
    assert "objective" in caught.value.message
    # the unknown policy gets the same answer (the convention the deviation cites)
    twin = json.dumps(
        {
            "manifest": _base_manifest(),
            "bundle_digest": "sha256:" + "a" * 64,
            "placement": {"policy": "nope"},
        }
    ).encode()
    with pytest.raises(ApiError) as same:
        _parse_submission(twin)
    assert same.value.http_status == caught.value.http_status
