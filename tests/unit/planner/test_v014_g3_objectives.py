"""V0.14 G3 (D295, roadmap 0.14.5): objectives rank the same estimates.

* ``min_time`` (and no objective) = the pre-G3 ``cost_model`` exactly;
* ``min_network`` ranks by predicted bytes moved, time breaks ties, then the
  node id; a Mode 3 set must still pass its margin test;
* ``prefer_local``: Local wins unless another candidate is predicted at most
  ``(1 - margin) x`` Local's time - the boundary is exact;
* rule policies ignore the objective; an unknown objective is refused;
* the reason key names the objective only when it changed the pick.
"""

from __future__ import annotations

import dataclasses

import pytest
from mf_planner_fabrics import synthetic_fabric

from meshforge.common.manifest import OBJECTIVES, POLICIES
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.objectives import objective_of
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    TreeNeed,
)
from meshforge.controller.store.models.plan import PLAN_OBJECTIVES

pytestmark = pytest.mark.unit

TREE = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


def _node(node_id: str, cached: frozenset[str] = frozenset()) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=frozenset({"exec", "exec-subprocess", "exec-split"}),
        cpu_slots=2,
        cached=cached,
    )


def _case(
    seconds: dict[str, float],
    *,
    local: str | None = "n_local",
    objective: str | None = None,
    trees: tuple[TreeNeed, ...] = (),
    cached: dict[str, frozenset[str]] | None = None,
    margin: float = 0.25,
) -> tuple[PlanRequest, ClusterSnapshot]:
    cached = cached or {}
    request = PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=trees,
        submitter_node_id=local,
        history=History(by_node_s=seconds, samples_by_node=dict.fromkeys(seconds, 3)),
        objective=objective,  # type: ignore[arg-type]
    )
    snapshot = ClusterSnapshot(
        nodes=tuple(_node(n, cached.get(n, frozenset())) for n in sorted(seconds)),
        config=PlannerConfig(prefer_local_margin=margin),
    )
    return request, snapshot


def test_the_objective_names_agree_everywhere() -> None:
    assert OBJECTIVES == PLAN_OBJECTIVES == ("min_time", "min_network", "prefer_local")
    assert objective_of(None) == "min_time"
    with pytest.raises(ValueError, match="objective"):
        objective_of("max_fun")


def test_an_unknown_objective_is_refused_by_plan_for_every_policy() -> None:
    request, snapshot = _case({"n_local": 10.0, "n_b": 5.0}, objective="fastest")
    for policy in POLICIES:
        with pytest.raises(ValueError, match="objective"):
            plan(request, snapshot, policy)


@pytest.mark.parametrize("split", [False, True])
def test_min_time_is_the_cost_model_of_before(split: bool) -> None:
    for seed in range(12):
        request, snapshot, now = synthetic_fabric(5, split=split, seed=seed)
        before = plan(request, snapshot, "cost_model", now)
        named = plan(
            dataclasses.replace(request, objective="min_time"),
            snapshot,
            "cost_model",
            now,
        )
        assert candidates_json(named) == candidates_json(before)
        assert named.reason_key == before.reason_key
        assert named.chosen == before.chosen and named.split == before.split
        assert named.objective == "min_time" and before.objective is None


# ---------------------------------------------------------------- prefer_local


def test_prefer_local_keeps_local_until_the_other_is_25_percent_faster() -> None:
    # Local 100 s; the other at exactly 75 s wins (<= 0.75 x), 75.01 s does not
    for other, want in ((75.0, "n_b"), (75.01, "n_local"), (90.0, "n_local")):
        request, snapshot = _case(
            {"n_local": 100.0, "n_b": other}, objective="prefer_local"
        )
        decision = plan(request, snapshot, "cost_model")
        assert decision.chosen is not None and decision.chosen.node_id == want, other
    request, snapshot = _case({"n_local": 100.0, "n_b": 60.0}, objective="min_time")
    assert plan(request, snapshot, "cost_model").chosen.node_id == "n_b"  # type: ignore[union-attr]


def test_prefer_local_margin_is_configurable_and_zero_means_min_time() -> None:
    request, snapshot = _case(
        {"n_local": 100.0, "n_b": 99.0}, objective="prefer_local", margin=0.0
    )
    assert plan(request, snapshot, "cost_model").chosen.node_id == "n_b"  # type: ignore[union-attr]
    request, snapshot = _case(
        {"n_local": 100.0, "n_b": 40.0}, objective="prefer_local", margin=0.7
    )
    assert plan(request, snapshot, "cost_model").chosen.node_id == "n_local"  # type: ignore[union-attr]


def test_prefer_local_without_a_feasible_local_is_min_time() -> None:
    request, snapshot = _case(
        {"n_a": 30.0, "n_b": 20.0}, local="n_missing", objective="prefer_local"
    )
    decision = plan(request, snapshot, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "n_b"


def test_the_reason_key_names_the_objective_only_when_it_changed_the_pick() -> None:
    keep, snap = _case({"n_local": 100.0, "n_b": 90.0}, objective="prefer_local")
    plain, _ = _case({"n_local": 100.0, "n_b": 90.0}, objective=None)
    changed = plan(keep, snap, "cost_model")
    unchanged = plan(plain, snap, "cost_model")
    assert changed.chosen.node_id == "n_local"  # type: ignore[union-attr]
    assert changed.reason_key != unchanged.reason_key
    same, snap2 = _case({"n_local": 50.0, "n_b": 90.0}, objective="prefer_local")
    already, _ = _case({"n_local": 50.0, "n_b": 90.0}, objective=None)
    assert plan(same, snap2, "cost_model").reason_key == (
        plan(already, snap2, "cost_model").reason_key
    )


# ---------------------------------------------------------------- min_network


def _network_case(objective: str | None) -> tuple[PlanRequest, ClusterSnapshot]:
    # n_fast holds nothing (must fetch 400 MB); n_holder holds the tree
    return _case(
        {"n_fast": 10.0, "n_holder": 60.0, "n_far": 30.0},
        local=None,
        objective=objective,
        trees=(TreeNeed(TREE, 400 * 10**6),),
        cached={"n_holder": frozenset({TREE})},
    )


def test_min_network_picks_the_node_that_moves_the_fewest_bytes() -> None:
    request, snapshot = _network_case("min_network")
    decision = plan(request, snapshot, "cost_model")
    assert decision.chosen is not None and decision.chosen.node_id == "n_holder"
    assert decision.chosen.score == 0.0  # the score is the bytes it moves
    order = [c.node_id for c in decision.candidates]
    assert order[0] == "n_holder"
    # equal bytes (both fetch the tree): the predicted time breaks the tie
    assert order[1:] == sorted(
        order[1:],
        key=lambda n: next(
            c.estimate.total_s for c in decision.candidates if c.node_id == n
        ),
    )
    timed, _ = _network_case("min_time")
    assert plan(timed, snapshot, "cost_model").chosen.node_id != "n_holder"  # type: ignore[union-attr]


def test_min_network_ties_on_bytes_and_time_break_on_the_node_id() -> None:
    request, snapshot = _case(
        {"n_b": 10.0, "n_a": 10.0}, local=None, objective="min_network"
    )
    decision = plan(request, snapshot, "cost_model")
    assert [c.node_id for c in decision.candidates] == ["n_a", "n_b"]


def test_rule_policies_ignore_the_objective() -> None:
    for policy in POLICIES:
        if policy == "cost_model":
            continue
        for objective in OBJECTIVES:
            request, snapshot = _network_case(objective)
            base, _ = _network_case(None)
            got = plan(request, snapshot, policy)
            want = plan(base, snapshot, policy)
            assert candidates_json(got) == candidates_json(want), (policy, objective)
            assert got.reason_key == want.reason_key
            assert got.objective == objective


# ---------------------------------------------------------------- split jobs


def test_a_split_job_under_min_network_never_splits_without_the_margin() -> None:
    for seed in range(20):
        request, snapshot, now = synthetic_fabric(4, split=True, seed=seed)
        net = plan(
            dataclasses.replace(request, objective="min_network"),
            snapshot,
            "cost_model",
            now,
        )
        assert net.split is not None
        if net.split.split:
            assert net.split.margin.passed  # a set still needs its margin test
            chosen = net.chosen
            assert chosen is not None and chosen.margin is not None
            assert chosen.margin.passed


@pytest.mark.parametrize("margin", [0.25, 0.9])
def test_a_split_job_under_prefer_local_keeps_local_unless_clearly_faster(
    margin: float,
) -> None:
    flips = 0
    for seed in range(20):
        request, snapshot, now = synthetic_fabric(4, split=True, seed=seed)
        snapshot = dataclasses.replace(
            snapshot,
            config=dataclasses.replace(snapshot.config, prefer_local_margin=margin),
        )
        timed = plan(request, snapshot, "cost_model", now)
        local = plan(
            dataclasses.replace(request, objective="prefer_local"),
            snapshot,
            "cost_model",
            now,
        )
        assert timed.chosen is not None and local.chosen is not None
        local_row = next(
            c for c in local.candidates if c.mode == "local" and c.estimate
        )
        assert local_row.estimate is not None
        if local.chosen.mode != "local":
            # only a candidate at most 0.75 x Local's time takes it away
            assert local.chosen.estimate is not None
            assert local.chosen.estimate.total_s <= (1 - margin) * (
                local_row.estimate.total_s
            )
            assert local.chosen == timed.chosen
        else:
            flips += timed.chosen.mode != "local"
    if margin == 0.9:  # 4 laptops are never 10x one: Local keeps every job
        assert flips == 20
