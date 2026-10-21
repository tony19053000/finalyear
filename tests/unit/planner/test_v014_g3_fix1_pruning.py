"""V0.14 G3 fix round 1 (tester r1 D1, Lead decision): pruning never changes
the chosen plan.

Pruning may only skip costing a strictly dominated set; every input to chunk
sizing is what the unpruned path uses. So over the tester's random split
fabrics the pruned decision chooses exactly the unpruned plan - the same
workers, chunking and predicted makespan - and every set it costs is a set the
unpruned path costs, with the identical row.
"""

from __future__ import annotations

from typing import Any

import pytest
from test_v014_g3_tester_r1 import NOW, _split_fabric, _with_pruning

from meshforge.controller.planner import plan

pytestmark = pytest.mark.unit

SEEDS = range(6000)
REPROS = (57, 3662)
"""Tester r1: seed 57 - dropping an idle lane changed the auto chunk size
(3 -> 5 items) and raised the predicted makespan 129.76 s -> 134.05 s; 3662 the
Lead's second repro."""


def _plan_key(decision: Any) -> Any:
    """What the dispatcher runs, plus the chosen row."""
    chosen = decision.chosen
    sp = decision.split
    return (
        None if chosen is None else (chosen.mode, chosen.node_id, chosen.node_set),
        None
        if sp is None
        else (
            sp.split,
            sp.lanes,
            sp.chunk_items,
            sp.chunks,
            sp.predicted_makespan_s,
            sp.margin,
        ),
        decision.objective,
    )


def _pair(seed: int) -> tuple[Any, Any]:
    request, snap = _split_fabric(seed)
    plain = plan(request, _with_pruning(snap, False), "cost_model", NOW)
    cut = plan(request, _with_pruning(snap, True), "cost_model", NOW)
    return plain, cut


@pytest.mark.parametrize("seed", REPROS)
def test_the_tester_repros_choose_the_unpruned_plan(seed: int) -> None:
    plain, cut = _pair(seed)
    assert plain.split is not None and cut.split is not None
    assert _plan_key(cut) == _plan_key(plain)
    assert cut.split.predicted_makespan_s == plain.split.predicted_makespan_s


def test_pruned_equals_unpruned_chosen_plan_over_6000_fabrics() -> None:
    pruned_decisions = 0
    skipped_rows = 0
    for seed in SEEDS:
        plain, cut = _pair(seed)
        # the unpruned pick is never itself a skipped set
        if plain.chosen is not None and plain.chosen.mode == "split":
            assert plain.chosen.node_set in {
                c.node_set for c in cut.candidates if c.mode == "split"
            }, seed
        assert _plan_key(cut) == _plan_key(plain), seed
        # pruning only skips: every costed row is the unpruned path's row
        # (ranks shift when a set above it was skipped)
        rows = {(c.mode, c.node_set): c for c in plain.candidates}
        for row in cut.candidates:
            twin = rows[(row.mode, row.node_set)]
            assert row.estimate == twin.estimate, seed
            assert (row.chunk_items, row.chunks) == (twin.chunk_items, twin.chunks)
        skipped_rows += len(plain.candidates) - len(cut.candidates)
        pruned_decisions += bool(cut.pruned)
        assert bool(cut.pruned) == (len(cut.candidates) < len(plain.candidates))
    # not vacuous: pruning still skips sets on a good share of the fabrics
    assert pruned_decisions > 500, pruned_decisions
    assert skipped_rows > 1000, skipped_rows
