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


def _with_exclusion(request: PlanRequest, snapshot: ClusterSnapshot) -> PlanRequest:
    """D110 retry exclusion, made *soft* (V0.6 G3): a pinned job never
    excludes (it retries on its node only), and when no node outside the
    excluded set could **ever** run the job (all rejected statically) the
    exclusion is lifted - the job retries where it failed instead of waiting
    out ``retry_exclude_s`` for a node that does not exist (a one-laptop
    fabric whose agent restarted). Its attempts stay bounded by
    ``max_attempts``."""
    if not request.excluded:
        return request
    if request.pinned_node_id is not None:
        return replace(request, excluded={})
    elsewhere = any(
        node.node_id not in request.excluded and static_rejection(request, node) is None
        for node, _local in candidates(request, snapshot)
    )
    return request if elsewhere else replace(request, excluded={})


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
