"""The planner: ``plan(request, snapshot, policy, now) -> Decision`` (D102, ADR-0007).

Pure and synchronous: no database, no asyncio, no clock. Stages exactly as
ADR-0007:

1. :func:`~.candidates.candidates` (V0.14 G1: the one generator for every
   request kind) - **Local** (the submitter's own node, ARCHITECTURE §5: a
   baseline candidate, never a separate code path) and **Mode 1** on every
   other node; a *pinned* job (D103) has the candidate set of its one node.
2. feasibility (:mod:`.feasibility`) - static, then live; each rejection is
   ``(node_id, code, text)``.
3. :func:`~.estimator.estimate` for every feasible candidate.
4. :func:`~.policies.rank` by the policy; rank 1 is chosen.

A **split job** (V0.10 G3, ``request.split``) shares stages 1-2; stages 3-4
are :func:`~.split_plan.decide_split`: the generator's split candidates over
the feasible lanes (every lane alone - "don't split" - plus the Mode 3 node
sets), the margin test, and :attr:`~.types.Decision.split` - what the
dispatcher runs.

The :class:`~.types.Decision` lists **every** candidate - chosen, ranked and
rejected - which is what ``plan_decision`` stores and ``compute explain``
prints. ``snapshot_digest`` names exactly what the planner saw;
``reason_key`` names the set of rejections (a waiting job's decision is only
recorded again when it changes, D104).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from dataclasses import asdict, replace
from typing import Any

# V0.14 G2 (D293): calibration factors
from meshforge.controller.planner.calibration import (
    Calibration,
    calibrate_estimate,
    calibration_json,
)
from meshforge.controller.planner.candidates import STORED_MODE, candidates
from meshforge.controller.planner.estimator import estimate
from meshforge.controller.planner.feasibility import (
    choose_slot,
    rejection,
    static_rejection,
)
from meshforge.controller.planner.policies import Scored, rank
from meshforge.controller.planner.retry_exclusion import LIFT_ORDER
from meshforge.controller.planner.split_plan import decide_split
from meshforge.controller.planner.types import (
    CandidateEval,
    ClusterSnapshot,
    Decision,
    Mode,
    NodeSnapshot,
    PlanRequest,
    Slot,
)

__all__ = ["plan", "snapshot_digest"]


def _node_candidates(
    request: PlanRequest, snapshot: ClusterSnapshot
) -> list[tuple[NodeSnapshot, bool]]:
    """The generator's node candidates as ``(node, is_local)``, in its order
    (node-id order; one for a pinned job, none when its node is unknown)."""
    nodes: dict[str, NodeSnapshot] = {}
    for node in snapshot.nodes:
        nodes.setdefault(node.node_id, node)
    return [
        (nodes[c.node_id], c.mode == "local")
        for c in candidates(request, snapshot).candidates
    ]


def _jsonable(value: Any) -> Any:
    if isinstance(value, frozenset | set):
        return sorted(value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dt.datetime):
        return value.isoformat()
    return value


def snapshot_digest(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    calibration: Calibration | None = None,
) -> str:
    """SHA-256 of the canonical (request, snapshot) pair."""
    data: dict[str, Any] = {
        "request": _jsonable(asdict(request)),
        "snapshot": _jsonable(asdict(snapshot)),
    }
    # V0.14 G2 (D293): a calibrated decision saw its factors too (None ->
    # the digest of every decision before G2, unchanged)
    if calibration is not None:
        data["calibration"] = {
            "version": calibration.version,
            **calibration_json(calibration),
        }
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reason_key(evals: list[CandidateEval]) -> str:
    # a Mode 3 set row is keyed by its mode, not its head node (V0.10 G3)
    parts = sorted(
        f"{'split' if c.mode == 'split' else c.node_id}:"
        f"{c.rejection.code.value if c.rejection else 'ok'}"
        for c in evals
    )
    return hashlib.sha256("\n".join(parts).encode("ascii")).hexdigest()


def _usable_now(
    request: PlanRequest, node: NodeSnapshot, *, local: bool, cap: int
) -> bool:
    """``node`` would take ``request`` right now if it were not excluded."""
    lifted = replace(
        request,
        excluded={k: v for k, v in request.excluded.items() if k != node.node_id},
    )
    return rejection(lifted, node, max_attempts_per_node=cap, local=local) is None


def _with_exclusion(request: PlanRequest, snapshot: ClusterSnapshot) -> PlanRequest:
    """D110 retry exclusion, made *soft* by the D129 lift rule (V0.6 G4).

    * A pinned job never excludes (it retries on its node only).
    * While some node **outside** the excluded set could ever run the job
      (passes the static rules), nothing is lifted: the job waits for it.
    * Otherwise (every node that could run it is excluded - a one-laptop
      fabric, or several nodes failing at once) exclusions are lifted **one
      failure-type tier at a time**, in :data:`~.retry_exclusion.LIFT_ORDER`
      (transient ``node_lost``/``timeout`` first, ``vram_oom`` last), and only
      for the nodes of that tier that would take the job **right now** (all
      live rules pass: online, connected, a free slot...). A node still
      failing for its reason - still offline, still busy - is never lifted
      onto. ``disk_full`` is never lifted early (:data:`~.retry_exclusion.
      NEVER_LIFTED`): no signal says the workdir has space again.
    * Nothing usable in any tier -> the exclusions stand and the job waits
      (``QUEUED``) until a node recovers or the earliest exclusion expires,
      instead of burning attempts on a node that would fail it again (chaos
      seed 22: three ``node_lost`` nodes offline + one full disk).

    Deterministic: tiers in a fixed order, nodes in node-id order."""
    if not request.excluded:
        return request
    if request.pinned_node_id is not None or request.node_set:
        return replace(request, excluded={})
    pairs = _node_candidates(request, snapshot)
    elsewhere = any(
        node.node_id not in request.excluded and static_rejection(request, node) is None
        for node, _local in pairs
    )
    if elsewhere:
        return request
    cap = snapshot.config.max_attempts_per_node
    for tier in LIFT_ORDER:
        usable = {
            node.node_id
            for node, local in pairs
            if request.excluded.get(node.node_id) in tier
            and _usable_now(request, node, local=local, cap=cap)
        }
        if usable:
            return replace(
                request,
                excluded={k: v for k, v in request.excluded.items() if k not in usable},
            )
    return request


def plan(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    policy: str,
    now: dt.datetime | None = None,
    *,
    calibration: Calibration | None = None,
) -> Decision:
    """See module docstring. ``now`` judges the age of network-matrix links
    (V0.6 G4, D111); without it no link counts as fresh.

    V0.14 G2 (D293): ``calibration`` scales each candidate's non-``measured``
    terms by its fitted factors (:mod:`.calibration`) before the policy, the
    margin test and the ranking see them; None = uncalibrated (bit-identical
    to the planner before G2)."""
    config = snapshot.config
    request = _with_exclusion(request, snapshot)
    # V0.14 G2: the request's workload family, as the calibration knows it
    family = None if calibration is None else calibration.family_of(request.fingerprint)
    evals: list[CandidateEval] = []
    scored: list[Scored] = []
    feasible: list[tuple[NodeSnapshot, bool, Slot]] = []
    for node, local in _node_candidates(request, snapshot):
        mode: Mode = STORED_MODE["local" if local else "mode1"]
        refused = rejection(
            request,
            node,
            max_attempts_per_node=config.max_attempts_per_node,
            local=local,
        )
        if refused is not None:
            evals.append(
                CandidateEval(node.node_id, mode, feasible=False, rejection=refused)
            )
            continue
        slot = choose_slot(request, node)
        if slot is None:  # pragma: no cover - live_rejection already said slots_full
            raise AssertionError("a feasible candidate without a slot")
        if request.split is not None:
            feasible.append((node, local, slot))
            continue
        guess = estimate(request, node, slot, snapshot, local=local, now=now)
        if calibration is not None:  # V0.14 G2 (D293)
            guess = calibrate_estimate(
                guess, calibration, node_id=node.node_id, family=family
            )
        scored.append(Scored(node=node, slot=slot, local=local, estimate=guess))
        evals.append(
            CandidateEval(node.node_id, mode, feasible=True, slot=slot, estimate=guess)
        )
    if request.split is not None:
        rows, picked, split_plan = decide_split(
            request, snapshot, policy, feasible, now=now, calibration=calibration
        )
        rows = [*rows, *evals]
        rows.sort(key=lambda c: (c.rank is None, c.rank or 0, c.node_id))
        return Decision(
            policy=policy,
            candidates=tuple(rows),
            chosen=picked,
            snapshot_digest=snapshot_digest(request, snapshot, calibration),
            reason_key=_reason_key(rows),
            split=split_plan,
        )
    ranking = {r.node_id: r for r in rank(policy, request, snapshot, scored)}
    final: list[CandidateEval] = []
    chosen: CandidateEval | None = None
    for item in evals:
        placed = ranking.get(item.node_id)
        if placed is not None:
            item = replace(item, score=placed.score, rank=placed.rank)
            if placed.rank == 1:
                chosen = item
        final.append(item)
    final.sort(key=lambda c: (c.rank is None, c.rank or 0, c.node_id))
    return Decision(
        policy=policy,
        candidates=tuple(final),
        chosen=chosen,
        snapshot_digest=snapshot_digest(request, snapshot, calibration),
        reason_key=_reason_key(final),
    )
