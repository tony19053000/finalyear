"""V0.14 G3 tester round 1: ``compute explain`` output for a link_infeasible
rejection (the link, its bandwidth and the bytes), a risk column that never
shows for a risk-free decision, and non-finite numbers never reaching the
table."""

from __future__ import annotations

import dataclasses
import datetime as dt
import io
from typing import Any

import pytest
from rich.console import Console

from meshforge.cli.explain_commands import decision_tables
from meshforge.controller.api.decision_routes import CandidateOut
from meshforge.controller.planner import plan
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    LinkStat,
    NodeSnapshot,
    PlannerConfig,
    PlanRequest,
    Reliability,
    TreeNeed,
)

pytestmark = pytest.mark.unit

TREE = "sha256:" + "a" * 64
NOW = dt.datetime(2026, 10, 21, 12, 0, tzinfo=dt.UTC)
MIB = 1024 * 1024


def _node(node_id: str, **kw: Any) -> NodeSnapshot:
    return NodeSnapshot(
        node_id=node_id,
        state="online",
        connected=True,
        capabilities=frozenset({"exec", "exec-subprocess"}),
        cpu_slots=2,
        **kw,
    )


def _text(decision: Any) -> str:
    rows = [
        CandidateOut.model_validate(row).model_dump(mode="json", exclude_none=True)
        for row in candidates_json(decision)
    ]
    payload = {
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
    out = io.StringIO()
    console = Console(file=out, width=400, color_system=None)
    for table in decision_tables(payload):
        console.print(table)
    return out.getvalue()


def _decision(*, reliability: Reliability | None = None) -> Any:
    request = PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
        trees=(TreeNeed(TREE, 500 * MIB),),
    )
    snap = ClusterSnapshot(
        nodes=(
            _node("n_far", reliability=reliability),
            _node("n_holder", cached=frozenset({TREE}), reliability=reliability),
            _node("n_other", cached=frozenset({TREE}), reliability=reliability),
        ),
        links={
            ("n_far", "n_holder"): LinkStat(
                bw_bytes_s=250e3,
                rtt_ms=4.0,
                samples=3,
                measured_at=NOW - dt.timedelta(seconds=20),
            )
        },
        config=PlannerConfig(min_useful_bw_bytes_s=1e6),
    )
    return plan(request, snap, "cost_model", NOW)


def test_v014_g3_tester_r1_explain_names_the_link_bandwidth_and_bytes() -> None:
    text = _text(_decision())
    assert "link_infeasible" in text
    assert "n_holder" in text and "500 MiB" in text and "0.250 MB/s" in text


def test_v014_g3_tester_r1_explain_has_no_risk_column_without_risk() -> None:
    assert "RISK" not in _text(_decision())
    with_risk = _text(_decision(reliability=Reliability(1, 5, 7.0)))
    assert "RISK" in with_risk and "(history)" in with_risk


def test_v014_g3_tester_r1_explain_survives_hostile_risk_numbers() -> None:
    request = PlanRequest(
        job_id="job_" + "0" * 26,
        attempt_no=1,
        gpu="none",
        vram_bytes=None,
        ram_bytes=None,
        cpus=None,
        base="python-cpu",
        env_key="e" * 64,
    )
    nodes = tuple(
        dataclasses.replace(
            _node(f"n{i}"), reliability=Reliability(10**18, 10**18 + i, 1e300)
        )
        for i in range(3)
    )
    decision = plan(request, ClusterSnapshot(nodes=nodes), "cost_model", NOW)
    text = _text(decision)
    assert "nan" not in text.lower() and "inf" not in text.lower()
