"""V0.14 G3: ``compute explain`` shows what G3 adds to a decision - the
``risk`` term with its source, the nodes pruned from every Mode 3 set and
why, a set that passed its margin test but lost to the job's objective, and a
``link_infeasible`` rejection with its link, bandwidth and bytes. The stored
decision JSON goes through the API model first (what ``GET /v1/jobs/{id}/
decisions`` answers)."""

from __future__ import annotations

import dataclasses
import io
from typing import Any

import pytest
from mf_planner_fabrics import synthetic_fabric
from rich.console import Console

from meshforge.cli.explain_commands import decision_tables
from meshforge.controller.api.decision_routes import CandidateOut
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.types import Decision, Reliability

pytestmark = pytest.mark.unit


def _payload(decision: Decision) -> dict[str, Any]:
    rows = [
        CandidateOut.model_validate(row).model_dump(mode="json", exclude_none=True)
        for row in candidates_json(decision)
    ]
    return {
        "decisions": [
            {
                "id": 1,
                "attempt_no": 1,
                "attempt_id": None,
                "outcome": "dispatched",
                "policy": decision.policy,
                "chosen_node_id": decision.chosen.node_id if decision.chosen else None,
                "created_at": "2026-10-21T10:00:00Z",
                "snapshot_digest": decision.snapshot_digest,
                "candidates": rows,
                "actual": None,
            }
        ]
    }


def _text(decision: Decision) -> str:
    out = io.StringIO()
    console = Console(file=out, width=400, color_system=None)
    for table in decision_tables(_payload(decision)):
        console.print(table)
    return out.getvalue()


def test_the_risk_term_is_a_column_with_its_source() -> None:
    request, snapshot, now = synthetic_fabric(3, split=False, seed=1)
    snapshot = dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(n, reliability=Reliability(2, 10, 7.0))
            for n in snapshot.nodes
        ),
    )
    text = _text(plan(request, snapshot, "cost_model", now))
    assert "RISK" in text and "(history)" in text
    plain = _text(
        plan(*synthetic_fabric(3, split=False, seed=1)[:2], "cost_model", now)
    )
    assert "RISK" not in plain


def test_pruned_nodes_are_named_with_the_node_that_dominates_them() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=3)
    far = [n.node_id for n in snapshot.nodes if not n.cached][-1]
    slow = dataclasses.replace(
        snapshot,
        nodes=tuple(
            dataclasses.replace(n, cpu_score=1.0) if n.node_id == far else n
            for n in snapshot.nodes
        ),
        links={
            k: dataclasses.replace(v, bw_bytes_s=1024.0) if k[0] == far else v
            for k, v in snapshot.links.items()
        },
        controller_links={
            k: dataclasses.replace(v, bw_bytes_s=1024.0) if k == far else v
            for k, v in snapshot.controller_links.items()
        },
        config=dataclasses.replace(snapshot.config, prune_dominated=True),
    )
    decision = plan(request, slow, "cost_model", now)
    assert decision.pruned
    text = _text(decision)
    assert f"left out of every set: {far} (by {decision.pruned[0].by})" in text


def test_a_set_kept_out_by_the_objective_says_so() -> None:
    request, snapshot, now = synthetic_fabric(4, split=True, seed=0)
    snapshot = dataclasses.replace(
        snapshot, config=dataclasses.replace(snapshot.config, prefer_local_margin=0.9)
    )
    decision = plan(
        dataclasses.replace(request, objective="prefer_local"),
        snapshot,
        "cost_model",
        now,
    )
    assert decision.split is not None and not decision.split.split
    assert "passed the margin test, but the job's objective ranked one node first" in (
        _text(decision)
    )
