"""V0.10 G3: ``compute explain`` for a split job (D265) - the stored decision
JSON the real planner produces goes through the API model and is rendered
with the node sets, chunking, item rates with their sources and the margin
test, in one sentence why it was (not) split."""

from __future__ import annotations

import io
from typing import Any

import pytest
from rich.console import Console

from meshforge.cli.explain_commands import decision_tables
from meshforge.controller.api.decision_routes import CandidateOut
from meshforge.controller.planner import (
    ClusterSnapshot,
    History,
    NodeSnapshot,
    PlanRequest,
    SplitInfo,
    TreeNeed,
    plan,
)
from meshforge.controller.planner.decision_json import candidates_json

pytestmark = pytest.mark.unit

EXEC = frozenset(
    {"exec", "exec-subprocess", "exec-split"}
)  # V0.10 G4: lanes need exec-split


def _node(node_id: str, **kw: Any) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=EXEC,
        cpu_slots=1,
        **kw,
    )


def _decision(items: int, rates: dict[str, float], **nodes: Any) -> dict[str, Any]:
    request = PlanRequest(
        job_id="job_01J0000000000000000000000",
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=(TreeNeed("sha256:" + "a" * 64, 1_000_000),),
        submitter_node_id="node_a",
        seeded=True,
        history=History(item_rate_by_node=rates),
        split=SplitInfo(items=items),
    )
    snapshot = ClusterSnapshot(
        nodes=tuple(
            _node(n, **nodes.get(n, {})) for n in ("node_a", "node_b", "node_c")
        )
    )
    decision = plan(request, snapshot, "cost_model")
    candidates = candidates_json(decision)
    for row in candidates:  # what GET /v1/jobs/{id}/decisions answers
        CandidateOut.model_validate(row)
    return {
        "id": 7,
        "attempt_no": 1,
        "attempt_id": None,
        "outcome": "dispatched",
        "policy": "cost_model",
        "chosen_node_id": decision.chosen.node_id if decision.chosen else None,
        "created_at": "2026-10-20T10:00:00Z",
        "snapshot_digest": decision.snapshot_digest,
        "candidates": candidates,
        "actual": None,
    }


def _text(*decisions: dict[str, Any]) -> str:
    out = io.StringIO()
    console = Console(file=out, width=300, color_system=None)
    for table in decision_tables({"decisions": list(decisions)}):
        console.print(table)
    return out.getvalue()


def test_a_split_decision_names_the_set_chunks_rates_and_margin() -> None:
    text = _text(_decision(3000, {"node_a": 10, "node_b": 10, "node_c": 10}))
    assert "split over 3 nodes:" in text
    assert "<= (1 - 0.1) x 303.00s (best single node node_a)" in text
    assert "node_a +2" in text  # the set row in the candidate table
    assert "node_a, node_b, node_c" in text
    assert "12 x 250 items" in text and "1 x 3000 items" in text
    assert "10/s (history)" in text
    assert "pass:" in text
    for term in ("ITEMS_FETCH", "CHUNK_OVERHEAD", "TAIL", "COMBINE"):
        assert term in text
    assert "1.50s (assumed)" in text  # the assumed per-chunk overhead says so


def test_a_dont_split_decision_says_why() -> None:
    text = _text(_decision(3000, {"node_a": 3000, "node_b": 3000, "node_c": 3000}))
    assert "not split: the best set" in text
    assert "is above 3.60s = (1 - 0.1) x 4.00s (best single node node_a)" in text
    assert "fail:" in text


def test_a_forced_dont_split_shows_the_reason() -> None:
    text = _text(_decision(3, {"node_a": 1, "node_b": 1, "node_c": 1}))
    assert "not split: 3 item(s) are fewer than 2 x split.min_nodes (2)" in text
    assert "split_too_small" in text


def test_an_idle_lane_is_marked() -> None:
    decision = _decision(3000, {"node_a": 1, "node_b": 1, "node_c": 1})
    row = next(c for c in decision["candidates"] if c.get("mode") == "split")
    row["inactive"] = ["node_c"]
    assert "node_c (idle)" in _text(decision)


def test_split_rows_tolerate_garbage() -> None:
    junk = {
        "candidates": [
            {"node_id": "x", "mode": "split", "node_set": ["x", 3], "chunks": "a"},
            {"node_id": "y", "mode": "split", "node_set": "nope", "margin_test": 4},
            {"node_id": "z", "mode": "split", "node_set": ["z"], "margin_test": {}},
        ]
    }
    text = _text(junk)
    assert "decision" in text and "\x1b" not in text
