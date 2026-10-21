"""V0.14 G4 (D314): a planner pick -> the campaign configuration, one test per case.

* Local -> ``local`` on the origin; Mode 1 on n -> ``mode1`` on n (same origin);
* Mode 3 over the active lanes -> ``mode3`` with that node set (order-free: the
  campaign stores sets sorted, the plan lists lanes fastest first);
* Mode 3 "don't split" (D269, one lane) -> the lane node's ``local`` / ``mode1``;
* a set with one active lane -> that node's configuration;
* no pick, another origin, an ``unsupported`` configuration, a set that is not
  the campaign's -> None (``unmeasured``).
"""

from __future__ import annotations

import pytest
from mf_eval_world import NOW, ORIGIN, fabric, group

from meshforge.controller.planner import plan
from meshforge.controller.planner.types import (
    CandidateEval,
    Decision,
    LanePlan,
    MarginTest,
    SplitPlan,
    TermSource,
)
from meshforge.controller.planner_eval.mapping import Pick, config_for, pick_of

pytestmark = pytest.mark.unit

MEDIANS = {"node_a": 100.0, "node_b": 50.0, "node_c": 70.0}
SETS = {("node_b", "node_c"): 40.0, ("node_a", "node_b", "node_c"): 45.0}


def _decision(chosen: CandidateEval | None, split: SplitPlan | None = None) -> Decision:
    return Decision(
        policy="cost_model",
        candidates=() if chosen is None else (chosen,),
        chosen=chosen,
        snapshot_digest="d",
        reason_key="r",
        split=split,
    )


def _lane(node: str, *, local: bool = False) -> LanePlan:
    return LanePlan(node, "cpu", local, 1.0, TermSource.HISTORY, 0.0)


def _split(*lanes: LanePlan, split: bool = True) -> SplitPlan:
    margin = MarginTest(None, None, None, 0.1, None, True)
    return SplitPlan(split, tuple(lanes), 10, 4, 1.0, margin)


def test_local_maps_to_the_origins_local_configuration() -> None:
    g = group("g", MEDIANS)
    pick = pick_of(_decision(CandidateEval(ORIGIN, "local", True, rank=1)))
    assert pick == Pick("local", ORIGIN)
    found = config_for(pick, ORIGIN, g.configs)
    assert found is not None and (found.strategy, found.node_id) == ("local", ORIGIN)


def test_mode1_maps_to_that_nodes_mode1_configuration() -> None:
    g = group("g", MEDIANS)
    pick = pick_of(_decision(CandidateEval("node_c", "offload", True, rank=1)))
    assert pick == Pick("mode1", "node_c")
    found = config_for(pick, ORIGIN, g.configs)
    assert found is not None and (found.strategy, found.node_id) == ("mode1", "node_c")


def test_mode3_maps_by_the_set_whatever_the_lane_order() -> None:
    g = group("g", MEDIANS, sets=SETS)
    chosen = CandidateEval(
        "node_c", "split", True, rank=1, node_set=("node_c", "node_b")
    )
    pick = pick_of(_decision(chosen, _split(_lane("node_c"), _lane("node_b"))))
    assert pick == Pick("mode3", "node_c", ("node_b", "node_c"))
    found = config_for(pick, ORIGIN, g.configs)
    assert found is not None and found.strategy == "mode3"
    assert found.node_set == ("node_b", "node_c")
    # a set the campaign never ran is unmeasured, never "the nearest set"
    other = Pick("mode3", "node_a", ("node_a", "node_c"))
    assert config_for(other, ORIGIN, g.configs) is None


def test_dont_split_maps_to_the_single_lane_configuration() -> None:
    """D269: a split job's "don't split" runs one lane (labelled mode1/local)."""
    g = group("g", MEDIANS, sets=SETS)
    one_lane = _split(_lane("node_b"), split=False)
    pick = pick_of(
        _decision(CandidateEval("node_b", "offload", True, rank=1), one_lane)
    )
    assert pick == Pick("mode1", "node_b")
    found = config_for(pick, ORIGIN, g.configs)
    assert found is not None and (found.strategy, found.node_id) == ("mode1", "node_b")
    local = _split(_lane(ORIGIN, local=True), split=False)
    pick = pick_of(_decision(CandidateEval(ORIGIN, "local", True, rank=1), local))
    assert pick == Pick("local", ORIGIN)


def test_a_set_with_one_active_lane_maps_to_that_node() -> None:
    chosen = CandidateEval("node_b", "split", True, rank=1, node_set=("node_b",))
    assert pick_of(_decision(chosen, _split(_lane("node_b")))) == Pick(
        "mode1", "node_b"
    )
    local = CandidateEval(ORIGIN, "split", True, rank=1, node_set=(ORIGIN,))
    assert pick_of(_decision(local, _split(_lane(ORIGIN, local=True)))) == Pick(
        "local", ORIGIN
    )


def test_nothing_chosen_is_unmeasured() -> None:
    g = group("g", MEDIANS)
    assert pick_of(_decision(None)) is None
    assert config_for(None, ORIGIN, g.configs) is None


def test_another_origin_or_an_unsupported_config_never_matches() -> None:
    g = group("g", MEDIANS, unsupported=("node_c",))
    assert config_for(Pick("mode1", "node_b"), "node_b", g.configs) is None
    assert config_for(Pick("mode1", "node_c"), ORIGIN, g.configs) is None
    assert config_for(Pick("mode1", "node_d"), ORIGIN, g.configs) is None
    # Local on a node that is not the origin is not the origin's local config
    assert config_for(Pick("local", "node_b"), ORIGIN, g.configs) is None


def test_real_planner_decisions_map_for_whole_and_split_jobs() -> None:
    request, snapshot = fabric(
        {"node_a": 100, "node_b": 10, "node_c": 50, "node_d": 80}
    )
    decision = plan(request, snapshot, "cost_model", NOW)
    assert pick_of(decision) == Pick("mode1", "node_b")
    split_request, split_snapshot = fabric(
        {"node_a": 100, "node_b": 100, "node_c": 100, "node_d": 100},
        split_items=4000,
    )
    split = plan(split_request, split_snapshot, "cost_model", NOW)
    pick = pick_of(split)
    assert pick is not None
    assert split.split is not None
    if split.split.split and len(split.split.lanes) > 1:
        assert pick.strategy == "mode3"
        assert pick.node_set == tuple(sorted(split.split.node_ids))
    else:
        assert pick.strategy in ("local", "mode1")
