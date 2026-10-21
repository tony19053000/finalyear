"""V0.14 G1 (D292) - **the key acceptance**: a replay reproduces the decision.

For every input - seeded random fabrics (:mod:`mf_planner_fabrics`: Local,
Mode 1 and Mode 3 jobs; pinned, forced-set, retried, benchmark, waiting and
failing jobs; split jobs too small or with too few nodes; stale and
future-dated links; non-finite inputs) and every recorded decision of the
golden corpus - and for **every** policy:

1. the dispatcher's decision is ``plan(request, project(request, snapshot))``
   (it plans on the projection, so its digest names what the replay holds);
2. ``to_replay`` -> canonical JSON text -> ``json.loads`` -> ``from_replay``
   -> ``plan`` gives that decision again **field for field** - every
   dataclass field, ints stay ints and floats floats (``fields_text``), and the
   stored candidate / split JSON byte for byte;
3. the projection drops nothing the planner reads: the unprojected decision
   differs in ``snapshot_digest`` only.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from mf_planner_fabrics import random_case
from mf_planner_golden import decision_view, fields_text

from meshforge.common.manifest import POLICIES
from meshforge.controller.planner import ClusterSnapshot, PlanRequest, plan
from meshforge.controller.planner.replay import (
    canonical_json,
    from_replay,
    project,
    to_replay,
)

pytestmark = pytest.mark.unit

SEEDS = range(400)
CORPUS = Path(__file__).parent / "data" / "v014_g1_golden.json.gz"


def _sans_digest(decision: Any) -> dict[str, Any]:
    view = decision_view(decision)
    del view["snapshot_digest"], view["fields_sha256"]
    return view


def _check(request: PlanRequest, snapshot: ClusterSnapshot, now: Any) -> int:
    """Asserts 1-3 for every policy; returns how many decisions chose."""
    chose = 0
    replay = json.loads(canonical_json(to_replay(request, snapshot, now)))
    again = from_replay(replay)
    cut = project(request, snapshot)
    for policy in POLICIES:
        original = plan(request, cut, policy, now)
        replayed = plan(*again[:2], policy, again[2])
        assert fields_text(replayed) == fields_text(original), policy
        assert decision_view(replayed) == decision_view(original), policy
        full = plan(request, snapshot, policy, now)
        assert _sans_digest(full) == _sans_digest(original), policy
        chose += original.chosen is not None
    return chose


@pytest.mark.parametrize("block", range(8))
def test_random_fabrics_replay_exactly_for_every_policy(block: int) -> None:
    kinds = {"split": 0, "whole": 0, "pinned": 0, "forced": 0, "chosen": 0}
    for seed in SEEDS[block::8]:
        request, snapshot, now = random_case(seed)
        kinds["split" if request.split is not None else "whole"] += 1
        kinds["pinned"] += request.pinned_node_id is not None
        kinds["forced"] += bool(request.node_set)
        kinds["chosen"] += _check(request, snapshot, now)
    # the draw really covers the shapes the brief names
    assert kinds["split"] >= 10 and kinds["whole"] >= 10, kinds
    assert kinds["chosen"] > 0, kinds


def test_the_random_draw_covers_waiting_failing_and_split_shapes() -> None:
    reasons: set[str] = set()
    modes: set[str] = set()
    for seed in SEEDS:
        request, snapshot, now = random_case(seed)
        decision = plan(request, project(request, snapshot), "cost_model", now)
        for c in decision.candidates:
            modes.add(c.mode)
            if c.rejection is not None:
                reasons.add(c.rejection.code.value)
        if decision.chosen is None:
            reasons.add("static" if decision.all_static_rejections else "waiting")
    assert {"local", "offload", "split"} <= modes
    assert {
        "waiting",
        "static",
        "split_too_small",
        "split_too_few_nodes",
        "not_online",
        "slots_full",
        "retry_excluded",
    } <= reasons, reasons


def _corpus() -> list[dict[str, Any]]:
    with gzip.open(CORPUS, "rt", encoding="utf-8") as fh:
        entries: list[dict[str, Any]] = json.load(fh)["entries"]
    return [e for e in entries if e["error"] is None]


@pytest.mark.parametrize("block", range(4))
def test_recorded_decisions_replay_exactly_for_every_policy(block: int) -> None:
    for entry in _corpus()[block::4]:
        request, snapshot, now = from_replay(entry["replay"])
        _check(request, snapshot, now)
