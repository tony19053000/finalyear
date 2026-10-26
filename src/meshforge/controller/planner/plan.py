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
from typing import Any, Final

# V0.14 G2 (D293): calibration factors
from meshforge.controller.planner.calibration import (
    Calibration,
    calibrate_estimate,
    calibrate_term,
    calibration_json,
)
from meshforge.controller.planner.candidates import STORED_MODE, candidates
from meshforge.controller.planner.estimator import estimate
from meshforge.controller.planner.feasibility import (
    choose_slot,
    link_rejection,
    rejection,
    static_rejection,
)
from meshforge.controller.planner.objectives import (
    COST_MODEL,
    objective_of,
    rank_singles,
    reason_suffix,
)
from meshforge.controller.planner.policies import Scored, rank

# V0.15 G4 (D336): the optional ML term predictor
from meshforge.controller.planner.predictor import (
    MlInput,
    MlSession,
    PredictorFailed,
    TermContext,
    ml_term,
    stored_raw,
)
from meshforge.controller.planner.retry_exclusion import LIFT_ORDER
from meshforge.controller.planner.risk import risked_singles
from meshforge.controller.planner.split_plan import decide_split
from meshforge.controller.planner.types import (
    CandidateEval,
    ClusterSnapshot,
    Decision,
    Estimate,
    EstimateTerm,
    Mode,
    NodeSnapshot,
    Objective,
    PlannerConfig,
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


# --- V0.14 G3: inputs added by G3 enter the digest only when they are set, so
# a pre-G3 (request, snapshot) - e.g. a stored replay - keeps its digest.
_G3_CONFIG_DEFAULTS: dict[str, Any] = {
    name: _jsonable(asdict(PlannerConfig())).get(name)
    for name in (
        "risk",
        "prefer_local_margin",
        "min_useful_bw_bytes_s",
        "min_useful_bytes",
        "prune_dominated",
    )
}


# --- V1.0 G2 (D418): likewise, an empty disabled_modes never enters the digest
_V10_CONFIG_DEFAULTS: dict[str, Any] = {"disabled_modes": []}


def _without_unset_g3(data: dict[str, Any]) -> dict[str, Any]:
    if data["request"].get("objective") is None:
        data["request"].pop("objective", None)
    snapshot = data["snapshot"]
    for node in snapshot["nodes"]:
        if node.get("reliability") is None:
            node.pop("reliability", None)
    config = snapshot["config"]
    for name, default in (*_G3_CONFIG_DEFAULTS.items(), *_V10_CONFIG_DEFAULTS.items()):
        if config.get(name) == default:
            config.pop(name, None)
    return data


# --- end V0.14 G3 -------------------------------------------------------------


def snapshot_digest(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    calibration: Calibration | None = None,
    ml_version: str | None = None,
) -> str:
    """SHA-256 of the canonical (request, snapshot) pair."""
    data = _without_unset_g3(  # V0.14 G3
        {
            "request": _jsonable(asdict(request)),
            "snapshot": _jsonable(asdict(snapshot)),
        }
    )
    # V0.14 G2 (D293): a calibrated decision saw its factors too (None ->
    # the digest of every decision before G2, unchanged)
    if calibration is not None:
        data["calibration"] = {
            "version": calibration.version,
            **calibration_json(calibration),
        }
    # V0.15 G4 (D336): a decision planned with a model saw its version too
    # (None -> the digest of every decision before G4, unchanged)
    if ml_version is not None:
        data["ml"] = {"version": ml_version}
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _reason_key(evals: list[CandidateEval], objective: Objective | None = None) -> str:
    # a Mode 3 set row is keyed by its mode, not its head node (V0.10 G3)
    parts = sorted(
        f"{'split' if c.mode == 'split' else c.node_id}:"
        f"{c.rejection.code.value if c.rejection else 'ok'}"
        for c in evals
    )
    if objective is not None:  # V0.14 G3 (D295): the objective changed the pick
        parts.append(reason_suffix(objective))
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


# --- V0.15 G4 (D336, D363) ------------------------------------------------------

_WHOLE_TERMS: Final = ("transfer", "env", "compute", "output")
"""A whole job's learnable terms (their planner and dataset names agree)."""


def _ml_estimates(
    scored: list[Scored],
    session: MlSession,
    calibration: Calibration | None,
    family: str | None,
) -> list[Estimate]:
    """Every scored single's estimate with the D336 precedence applied per
    term - one predictor call for all of them (batching)."""
    contexts = [
        TermContext(
            term=name,
            node_id=item.node.node_id,
            raw_s=stored_raw(name, term.seconds),
            source=term.source.value,
            detail=term.detail,
        )
        for item in scored
        for name, term in (
            (name, getattr(item.estimate, name)) for name in _WHOLE_TERMS
        )
    ]
    outcomes = iter(session.resolve(contexts))
    out: list[Estimate] = []
    for item in scored:
        node_id = item.node.node_id
        changed: dict[str, EstimateTerm] = {}
        for name in _WHOLE_TERMS:
            term: EstimateTerm = getattr(item.estimate, name)
            fallback = calibrate_term(
                name, term, calibration, node_id=node_id, family=family
            )
            changed[name] = ml_term(
                term,
                next(outcomes),
                name=name,
                version=session.version,
                fallback=fallback,
            )
        out.append(
            replace(
                item.estimate,
                transfer=changed["transfer"],
                env=changed["env"],
                compute=changed["compute"],
                output=changed["output"],
            )
        )
    return out


def plan(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    policy: str,
    now: dt.datetime | None = None,
    *,
    calibration: Calibration | None = None,
    ml: MlInput | None = None,
) -> Decision:
    """See module docstring. ``now`` judges the age of network-matrix links
    (V0.6 G4, D111); without it no link counts as fresh.

    V0.14 G2 (D293): ``calibration`` scales each candidate's non-``measured``
    terms by its fitted factors (:mod:`.calibration`) before the policy, the
    margin test and the ranking see them; None = uncalibrated (bit-identical
    to the planner before G2).

    V0.15 G4 (D336): ``ml`` corrects each learnable term with the model's
    predicted residual where it is usable (:mod:`.predictor`: precedence ML ->
    calibration -> raw, per term, per candidate); None = no model
    (bit-identical to the planner before G4). D363: a failing predictor never
    fails the plan - the decision is planned again with every learnable term
    on its calibration fallback, and :attr:`~.types.Decision.ml` names the
    error."""
    if ml is None:
        return _plan(request, snapshot, policy, now, calibration, None)
    try:
        return _plan(
            request,
            snapshot,
            policy,
            now,
            calibration,
            MlSession(ml, request, snapshot, now),
        )
    except PredictorFailed as failed:
        session = MlSession(ml, request, snapshot, now, failed=failed.cause)
        return _plan(request, snapshot, policy, now, calibration, session)


# --- end V0.15 G4 ---------------------------------------------------------------


def _plan(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    policy: str,
    now: dt.datetime | None,
    calibration: Calibration | None,
    session: MlSession | None,
) -> Decision:
    """:func:`plan` with the decision's ML session (None = no model)."""
    config = snapshot.config
    ml_use = None if session is None else session.use()  # V0.15 G4
    ml_version = None if ml_use is None else ml_use.version
    objective = objective_of(request.objective)  # V0.14 G3: unknown -> ValueError
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
        # V0.14 G3 (D294): a hopeless fresh link is refused before costing
        hopeless = link_rejection(request, node, snapshot, local=local, now=now)
        if hopeless is not None:
            evals.append(
                CandidateEval(node.node_id, mode, feasible=False, rejection=hopeless)
            )
            continue
        if request.split is not None:
            feasible.append((node, local, slot))
            continue
        guess = estimate(request, node, slot, snapshot, local=local, now=now)
        if calibration is not None and session is None:  # V0.14 G2 (D293)
            guess = calibrate_estimate(
                guess, calibration, node_id=node.node_id, family=family
            )
        scored.append(Scored(node=node, slot=slot, local=local, estimate=guess))
        evals.append(
            CandidateEval(node.node_id, mode, feasible=True, slot=slot, estimate=guess)
        )
    if session is not None and scored:
        # V0.15 G4 (D336): ML -> calibration -> raw per term, one batch call
        corrected = _ml_estimates(scored, session, calibration, family)
        scored = [
            replace(i, estimate=e) for i, e in zip(scored, corrected, strict=True)
        ]
    # V0.14 G3 (D296): the risk term, once every single is costed - from the
    # calibrated terms (V0.14 G2 above), added after them and never scaled
    if scored:
        risked = risked_singles(
            [(item.node, item.estimate) for item in scored], config.risk
        )
        scored = [replace(i, estimate=e) for i, e in zip(scored, risked, strict=True)]
        by_node = {item.node.node_id: item.estimate for item in scored}
        evals = [
            replace(e, estimate=by_node[e.node_id]) if e.feasible else e for e in evals
        ]
    if request.split is not None:
        outcome = decide_split(
            request,
            snapshot,
            policy,
            feasible,
            now=now,
            calibration=calibration,
            ml=session,  # V0.15 G4
        )
        rows = [*outcome.rows, *evals]
        rows.sort(key=lambda c: (c.rank is None, c.rank or 0, c.node_id))
        return Decision(
            policy=policy,
            candidates=tuple(rows),
            chosen=outcome.chosen,
            snapshot_digest=snapshot_digest(request, snapshot, calibration, ml_version),
            reason_key=_reason_key(
                rows, objective if outcome.objective_changed else None
            ),
            split=outcome.plan,
            objective=request.objective,
            pruned=outcome.pruned,
            ml=ml_use,
        )
    # V0.14 G3 (D295): cost_model ranks by the objective (min_time = as before)
    changed = False
    if policy == COST_MODEL and objective != "min_time":
        ranked, changed = rank_singles(
            objective, scored, prefer_local_margin=config.prefer_local_margin
        )
    else:
        ranked = rank(policy, request, snapshot, scored)
    ranking = {r.node_id: r for r in ranked}
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
        snapshot_digest=snapshot_digest(request, snapshot, calibration, ml_version),
        reason_key=_reason_key(final, objective if changed and chosen else None),
        objective=request.objective,
        ml=ml_use,
    )
