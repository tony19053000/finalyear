"""The planner: ``plan(request, snapshot, policy, now) -> Decision`` (D102, ADR-0007).

Pure and synchronous: no database, no asyncio, no clock. Stages exactly as
ADR-0007:

1. :func:`candidates` - **Local** (the submitter's own node, ARCHITECTURE §5:
   a baseline candidate, never a separate code path) and **Mode 1** on every
   other node; a *pinned* job (D103) has the candidate set of its one node.
2. feasibility (:mod:`.feasibility`) - static, then live; each rejection is
   ``(node_id, code, text)``.
3. :func:`~.estimator.estimate` for every feasible candidate.
4. :func:`~.policies.rank` by the policy; rank 1 is chosen.

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

from meshforge.controller.planner.estimator import estimate
from meshforge.controller.planner.feasibility import (
    choose_slot,
    rejection,
    static_rejection,
)
from meshforge.controller.planner.policies import Scored, rank
from meshforge.controller.planner.retry_exclusion import LIFT_ORDER
from meshforge.controller.planner.types import (
    CandidateEval,
    ClusterSnapshot,
    Decision,
    Mode,
    NodeSnapshot,
    PlanRequest,
)

__all__ = ["candidates", "plan", "snapshot_digest"]


def candidates(
    request: PlanRequest, snapshot: ClusterSnapshot
) -> list[tuple[NodeSnapshot, bool]]:
    """``(node, is_local)`` in node-id order; one entry for a pinned job (none
    when the pinned node is not in the snapshot)."""
    nodes = sorted(snapshot.nodes, key=lambda n: n.node_id)
    if request.pinned_node_id is not None:
        nodes = [n for n in nodes if n.node_id == request.pinned_node_id]
    return [(n, n.node_id == request.submitter_node_id) for n in nodes]


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


def snapshot_digest(request: PlanRequest, snapshot: ClusterSnapshot) -> str:
    """SHA-256 of the canonical (request, snapshot) pair."""
    data = {
        "request": _jsonable(asdict(request)),
        "snapshot": _jsonable(asdict(snapshot)),
    }
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reason_key(evals: list[CandidateEval]) -> str:
    parts = sorted(
        f"{c.node_id}:{c.rejection.code.value if c.rejection else 'ok'}" for c in evals
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
    if request.pinned_node_id is not None:
        return replace(request, excluded={})
    pairs = candidates(request, snapshot)
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
) -> Decision:
    """See module docstring. ``now`` is accepted for the D102 signature and
    future staleness rules; v0 needs no time."""
    del now
    config = snapshot.config
    request = _with_exclusion(request, snapshot)
    evals: list[CandidateEval] = []
    scored: list[Scored] = []
    for node, local in candidates(request, snapshot):
        mode: Mode = "local" if local else "offload"
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
        guess = estimate(request, node, slot, snapshot, local=local)
        scored.append(Scored(node=node, slot=slot, local=local, estimate=guess))
        evals.append(
            CandidateEval(node.node_id, mode, feasible=True, slot=slot, estimate=guess)
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
        snapshot_digest=snapshot_digest(request, snapshot),
        reason_key=_reason_key(final),
    )
