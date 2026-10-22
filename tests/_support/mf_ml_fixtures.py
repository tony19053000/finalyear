"""V0.15 G1: stored decisions and outcomes for the ML dataset tests.

Decisions are made by the **real planner** on :func:`mf_planner_fabrics.
synthetic_fabric` fabrics and stored as the dispatcher stores them
(:func:`~meshforge.controller.planner.decision_json.candidates_json` +
:func:`~meshforge.controller.planner.replay.to_replay`), so the rows a test
reads are the rows production writes. Outcomes (phases, lanes, chunks,
combine) are hand-made around them.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Mapping
from typing import Any

from mf_planner_fabrics import NOW, synthetic_fabric

from meshforge.controller.ml.targets import (
    ChunkOutcome,
    CombineOutcome,
    LaneOutcome,
    RunInput,
)
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import DecisionOutcome
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import to_replay
from meshforge.controller.planner.types import ClusterSnapshot, PlanRequest

__all__ = [
    "NOW",
    "Stored",
    "chosen",
    "lane_row",
    "set_run",
    "stored",
    "whole_run",
]

T0 = NOW + dt.timedelta(minutes=5)


@dataclasses.dataclass(frozen=True)
class Stored:
    request: PlanRequest
    snapshot: ClusterSnapshot
    now: dt.datetime
    candidates: list[dict[str, Any]]
    replay: dict[str, Any]
    chosen_node_id: str


def stored(
    *,
    split: bool = False,
    forced_set: bool = False,
    n: int = 3,
    seed: int = 0,
    learnable: bool = True,
    pinned: str | None = None,
) -> Stored:
    """A real decision, stored (module docstring). ``forced_set`` = a
    campaign's forced Mode 3 run over every node (the set is chosen).
    ``learnable`` = no peer links (transfers costed over the controller's
    transfer history, source ``history``, not ``measured``), a 30 s assumed
    environment build and a recorded output size - so every whole-job term
    is learnable. ``pinned`` = the job is pinned to that node."""
    request, snapshot, now = synthetic_fabric(n, split=split or forced_set, seed=seed)
    if learnable:
        snapshot = dataclasses.replace(
            snapshot,
            links={},
            config=dataclasses.replace(snapshot.config, default_env_s=30.0),
        )
        request = dataclasses.replace(
            request,
            history=dataclasses.replace(
                request.history, output_bytes=5e6, output_samples=3
            ),
        )
    if forced_set:
        request = dataclasses.replace(
            request, node_set=tuple(node.node_id for node in snapshot.nodes)
        )
    if pinned is not None:
        request = dataclasses.replace(request, pinned_node_id=pinned)
    decision = plan(request, snapshot, "cost_model", now)
    assert decision.chosen is not None
    return Stored(
        request=request,
        snapshot=snapshot,
        now=now,
        candidates=candidates_json(decision),
        replay=to_replay(request, snapshot, now),
        chosen_node_id=decision.chosen.node_id,
    )


def chosen(s: Stored) -> dict[str, Any]:
    return next(
        r for r in s.candidates if r["rank"] == 1 and r["node_id"] == s.chosen_node_id
    )


def lane_row(s: Stored, node_id: str) -> dict[str, Any]:
    return next(
        r
        for r in s.candidates
        if r["mode"] != "split"
        and r["node_id"] == node_id
        and r.get("node_set") == [node_id]
    )


def whole_run(
    s: Stored,
    *,
    phases: Mapping[str, int | None],
    decision_id: int = 1,
    attempt_id: str = "att_1",
    node_id: str | None = None,
    state: str = "done",
    role: str | None = None,
    family: str | None = "cpu.primes",
    fingerprint: str | None = "fp_a",
    finished_at: dt.datetime = T0,
    replay: Mapping[str, Any] | bool | None = True,
    lanes: tuple[LaneOutcome, ...] = (),
    combine: CombineOutcome | None = None,
) -> RunInput:
    node = node_id or s.chosen_node_id
    split = role is None and bool(chosen(s).get("node_set"))
    return RunInput(
        outcome=DecisionOutcome(
            decision_id=decision_id,
            attempt_id=attempt_id,
            chosen_node_id=s.chosen_node_id,
            attempt_node_id=node,
            attempt_state=state,
            attempt_role=role or ("lane" if split else "run"),
            candidates=s.candidates,
            phases_ms=dict(phases),
            finished_at=finished_at,
            family=family,
            fingerprint=fingerprint,
        ),
        job_id="job_a",
        replay=s.replay if replay is True else (None if replay is False else replay),  # type: ignore[arg-type]
        lanes=lanes,
        combine=combine,
    )


def set_run(
    s: Stored,
    *,
    lane_phases: Mapping[str, Mapping[str, int | None]] | None = None,
    chunks: Mapping[str, tuple[ChunkOutcome, ...]] | None = None,
    lane_states: Mapping[str, str] | None = None,
    combine_phases: Mapping[str, int | None] | None = None,
    combine_node: str | None = None,
    decision_id: int = 7,
    fingerprint: str | None = "fp_set",
    family: str | None = "cpu.split",
) -> RunInput:
    """A forced Mode 3 set decision with every lane done and a combine."""
    row = chosen(s)
    assert row["mode"] == "split"
    lanes = tuple(
        LaneOutcome(
            attempt_id=f"lane_{i}",
            node_id=node_id,
            state=(lane_states or {}).get(node_id, "done"),
            phases_ms=dict(
                (lane_phases or {}).get(
                    node_id, {"stage": 2000, "env": 1000, "run": 30_000, "collect": 50}
                )
            ),
            finished_at=T0 + dt.timedelta(seconds=i),
            chunks=(chunks or {}).get(
                node_id,
                (
                    ChunkOutcome(items=100, run_ms=5_000, outcome="committed"),
                    ChunkOutcome(items=100, run_ms=7_000, outcome="committed"),
                    ChunkOutcome(items=100, run_ms=9_999, outcome="lost"),
                ),
            ),
        )
        for i, node_id in enumerate(row["node_set"])
    )
    combine_term = row["estimate"]["terms"]["combine"]
    combine = CombineOutcome(
        attempt_id="combine_1",
        node_id=combine_node or combine_term["detail"]["combine_node_id"],
        state="done",
        phases_ms=dict(
            combine_phases or {"stage": 400, "env": None, "run": 1600, "collect": 30}
        ),
        finished_at=T0 + dt.timedelta(minutes=1),
    )
    return RunInput(
        outcome=DecisionOutcome(
            decision_id=decision_id,
            attempt_id=lanes[0].attempt_id,
            chosen_node_id=s.chosen_node_id,
            attempt_node_id=lanes[0].node_id,
            attempt_state="done",
            attempt_role="lane",
            candidates=s.candidates,
            phases_ms=dict(lanes[0].phases_ms),
            finished_at=T0,
            family=family,
            fingerprint=fingerprint,
        ),
        job_id="job_set",
        replay=s.replay,
        lanes=lanes,
        combine=combine,
    )
