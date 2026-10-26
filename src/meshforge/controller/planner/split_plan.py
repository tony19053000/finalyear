"""Stage 3 + 4 for a split job: singles vs Mode 3 sets, the margin test
(V0.10 G3, D265; ADR-0019 decision 5).

Called by :func:`~.plan.plan` with the nodes that passed feasibility, costed
as lanes; the candidates come from the one generator
(:func:`~.candidates.candidates` with ``lanes``, V0.14 G1). They are

* **Local** and **Mode 1 per node** - one lane runs every item: "don't split"
  (one chunk with all items for ``chunk: auto``; the user's chunks, one after
  the other, for ``chunk: {items: N}`` - the entrypoint may rely on them). It is
  the same lane path as a split (D269), so it is costed by the same model
  (:func:`~.split.estimate_split` with one lane) and ranked by the job's
  **policy**, exactly like a job that runs whole;
* **Mode 3** - the fastest-first prefix sets (:func:`~.split.prefix_sets`),
  ranked by predicted makespan (ties: the smaller set, then node ids).

**The margin test.** The best set is chosen only when
``T(set) <= (1 - split_margin) x T(best single)`` - the best single by
*prediction*, whatever the policy would pick, so a weak baseline policy never
makes splitting look good. Otherwise the policy's single node wins. No set is
compared - and a rejected ``split`` row says why - when the job is pinned
(``forced: pinned``, no row), has fewer than ``2 x min_nodes`` items
(``split_too_small``) or fewer than ``min_nodes`` nodes can take a lane now
(``split_too_few_nodes``).

The chosen candidate comes with a :class:`~.types.SplitPlan` - the lanes,
chunk size, chunk count and predicted makespan the dispatcher runs.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Any, Final

# V0.14 G2 (D293): a lane's stage + env are calibrated per node
from meshforge.controller.planner.calibration import (
    Calibration,
    calibrate_lane,
    calibrate_term,
)
from meshforge.controller.planner.candidates import FORCED_SET, Candidate, candidates
from meshforge.controller.planner.estimator import bounded
from meshforge.controller.planner.objectives import (
    COST_MODEL,
    objective_of,
    prefer_local_wins,
    rank_singles,
    set_bytes_moved,
)
from meshforge.controller.planner.policies import Ranked, Scored, rank

# V0.15 G4 (D336): lane terms and the combine corrected by the model
from meshforge.controller.planner.predictor import (
    MlReason,
    MlSession,
    TermContext,
    corrected,
    ml_block,
    ml_term,
    stored_raw,
)
from meshforge.controller.planner.risk import lane_risk, with_risk
from meshforge.controller.planner.split import (
    MIN_SECONDS_PER_ITEM,
    ChunkChoice,
    Lane,
    Rate,
    SetEstimate,
    choose_chunk_items,
    chunk_overhead,
    estimate_split,
    lane,
    set_chunk,
)
from meshforge.controller.planner.types import (
    CandidateEval,
    ClusterSnapshot,
    MarginTest,
    Mode,
    NodeSnapshot,
    Objective,
    PlanRequest,
    Pruned,
    Slot,
    SplitInfo,
    SplitPlan,
)

__all__ = ["FORCED_SET", "MAX_MARGIN", "SplitOutcome", "decide_split"]

MAX_MARGIN: Final = 0.95
"""``split_margin`` is read inside ``[0, MAX_MARGIN]`` (settings validate it;
a hand-built config cannot make splitting impossible by accident)."""
_ROUND: Final = 6


@dataclass(frozen=True, slots=True)
class SplitOutcome:
    """What :func:`decide_split` decided (V0.14 G3: a value, was a tuple)."""

    rows: list[CandidateEval]
    """Ranked + rejected set rows."""
    chosen: CandidateEval | None
    plan: SplitPlan | None
    """What to run."""
    objective_changed: bool = False
    """V0.14 G3: the job's objective changed the pick (vs ``min_time``)."""
    pruned: tuple[Pruned, ...] = ()
    """V0.14 G3 (D294): nodes no Mode 3 set was extended with."""


def _single_chunk(
    split: SplitInfo, lone: Lane, request: PlanRequest, snapshot: ClusterSnapshot
) -> ChunkChoice:
    if split.chunk_items is None:
        return ChunkChoice(max(1, split.items), 1, ("one_chunk",))
    overhead = chunk_overhead(request, snapshot.config.split)
    return choose_chunk_items(
        split.items,
        fastest_per_item_s=lone.per_item_s,
        lanes=1,
        overhead_s=overhead.seconds,
        config=snapshot.config.split,
        fixed=split.chunk_items,
    )


def _margin(
    candidate: SetEstimate | None,
    best: SetEstimate | None,
    margin: float,
    forced: str | None = None,
) -> MarginTest:
    single_s = None if best is None else round(best.total_s, _ROUND)
    threshold = None if single_s is None else round((1.0 - margin) * single_s, _ROUND)
    set_s = None if candidate is None else round(candidate.total_s, _ROUND)
    return MarginTest(
        set_s=set_s,
        best_single_s=single_s,
        best_single_node_id=None if best is None else best.node_ids[0],
        margin=margin,
        threshold_s=threshold,
        passed=set_s is not None and threshold is not None and set_s <= threshold,
        forced=forced,
    )


def _row(
    item: SetEstimate,
    mode: Mode,
    *,
    score: float | None,
    margin: MarginTest | None = None,
) -> CandidateEval:
    head = item.lanes[0]
    return CandidateEval(
        node_id=head.node_id,
        mode=mode,
        feasible=True,
        slot=head.slot,
        estimate=item.estimate,
        score=score,
        node_set=item.node_ids,
        lanes=item.lane_plans(),
        chunk_items=item.chunk.items,
        chunks=item.chunk.chunks,
        margin=margin,
    )


def _refused_row(candidate: Candidate) -> CandidateEval:
    """A ``mode3`` set the generator refused, as its stored row."""
    return CandidateEval(
        node_id=candidate.node_id,
        mode=candidate.stored_mode,
        feasible=False,
        rejection=candidate.rejection,
        node_set=candidate.node_set,
    )


# --- V0.14 G3 (D296) -----------------------------------------------------------


def _risked(
    item: SetEstimate, request: PlanRequest, snapshot: ClusterSnapshot
) -> SetEstimate:
    """``item`` with its risk term: a lost lane re-runs only the chunk it had
    leased (D267), so each active lane costs ``p_fail x (its chunk time +
    lease release)`` (:func:`~.risk.lane_risk`)."""
    overhead = chunk_overhead(request, snapshot.config.split).seconds
    lanes = [
        (lane_.node, item.chunk.items * lane_.per_item_s + overhead)
        for lane_ in item.lanes
        if lane_.node_id in item.active
    ]
    term = lane_risk(lanes, snapshot.config.risk)
    if term is None:
        return item
    return replace(item, estimate=with_risk(item.estimate, term))


def _objective_set(
    objective: Objective,
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    sets: Sequence[SetEstimate],
    singles: Sequence[SetEstimate],
    by_node: dict[str, SetEstimate],
    ranked: Sequence[Ranked],
    margin: float,
) -> tuple[SetEstimate | None, bool]:
    """D295 for a split job's sets: ``(the set to run if any, use it?)``.
    ``sets`` are in time order, ``ranked`` the singles under the objective
    (rank 1 first)."""
    best_single = min(singles, key=lambda s: (s.total_s, s.node_ids), default=None)
    by_time = sets[0] if sets else None
    time_uses_set = by_time is not None and (
        _margin(by_time, best_single, margin).passed or best_single is None
    )  # V1.0 G2: no single at all (Mode 1 disabled) -> the set is the plan
    if objective == "min_network":
        # a set must pass its own margin test, then move fewer bytes
        eligible = [
            s
            for s in sets
            if best_single is None or _margin(s, best_single, margin).passed
        ]

        def key(item: SetEstimate) -> tuple[float, float]:
            return (set_bytes_moved(item, request), item.total_s)

        if not eligible:
            return by_time, False
        pick = min(eligible, key=lambda s: (*key(s), len(s.lanes), s.node_ids))
        if not ranked:
            return pick, True
        return pick, key(pick) < key(by_node[ranked[0].node_id])
    if objective == "prefer_local" and time_uses_set and by_time is not None:
        local = next((s for s in singles if s.lanes[0].local), None)
        if local is not None and prefer_local_wins(
            local.total_s, by_time.total_s, snapshot.config.prefer_local_margin
        ):
            return by_time, False
    return by_time, time_uses_set


def _pick_key(item: SetEstimate | None, is_set: bool) -> tuple[bool, tuple[str, ...]]:
    """What a split decision runs, as a comparable key."""
    return (is_set, () if item is None else item.node_ids)


# --- end V0.14 G3 ---------------------------------------------------------------


# --- V0.15 G4 (D336) -------------------------------------------------------------


def _ml_lanes(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    lanes: Sequence[Lane],
    session: MlSession,
    calibration: Calibration | None,
) -> tuple[list[Lane], dict[str, dict[str, Any]]]:
    """Every lane's ``lane_stage`` / ``lane_env`` / ``chunk_rate`` with the
    D336 precedence (one predictor call), before any set is generated or
    costed - so chunk sizing, water filling and the margin test all see the
    corrected lanes. A lane's terms are described as its one-lane candidate
    (``lanes`` 1, that candidate's chunk): a set's lane count and chunk size
    depend on the corrected rates themselves."""
    split = request.split
    if split is None:  # pragma: no cover - decide_split checked it
        raise ValueError("a split job's lanes need its split")
    family = None if calibration is None else calibration.family_of(request.fingerprint)
    contexts: list[TermContext] = []
    for item in lanes:
        chunk = _single_chunk(split, item, request, snapshot).items
        for name, term in (("lane_stage", item.stage), ("lane_env", item.env)):
            contexts.append(
                TermContext(
                    term=name,
                    node_id=item.node_id,
                    raw_s=stored_raw(name, term.seconds),
                    source=term.source.value,
                    detail=term.detail,
                    chunk_items=chunk,
                )
            )
        contexts.append(
            TermContext(
                term="chunk_rate",
                node_id=item.node_id,
                raw_s=stored_raw("chunk_rate", item.rate.seconds_per_item),
                source=item.rate.source.value,
                chunk_items=chunk,
            )
        )
    outcomes = iter(session.resolve(contexts))
    out: list[Lane] = []
    rate_blocks: dict[str, dict[str, Any]] = {}
    for item in lanes:
        node_id = item.node_id
        stage = ml_term(
            item.stage,
            next(outcomes),
            name="lane_stage",
            version=session.version,
            fallback=calibrate_term(
                "transfer", item.stage, calibration, node_id=node_id, family=family
            ),
        )
        env = ml_term(
            item.env,
            next(outcomes),
            name="lane_env",
            version=session.version,
            fallback=calibrate_term(
                "env", item.env, calibration, node_id=node_id, family=family
            ),
        )
        rate_outcome = next(outcomes)
        spi = item.rate.seconds_per_item
        block = ml_block(rate_outcome, session.version, stored_raw("chunk_rate", spi))
        if rate_outcome.used and rate_outcome.prediction is not None:
            spi = max(
                MIN_SECONDS_PER_ITEM, corrected(spi, rate_outcome.prediction.y_hat)
            )
        # G4 fix r1 (G4-R1-1): the rate's block is in seconds PER ITEM, so it
        # is never put in a term's detail as that term's ``ml`` (a one-lane
        # row's compute detail copies its rate's); _ml_compute gives that
        # compute term its own block, the rate's nested as ``ml.rate``
        rate_blocks[item.node_id] = block
        rate = Rate(spi, item.rate.source, dict(item.rate.detail))
        out.append(replace(item, stage=stage, env=env, rate=rate))
    return out, rate_blocks


def _ml_compute(
    singles: Sequence[SetEstimate],
    rate_blocks: dict[str, dict[str, Any]],
    raw_spi: dict[str, float],
    items: int,
) -> list[SetEstimate]:
    """G4 fix r1 (G4-R1-1): each one-lane candidate's ``compute`` term gets
    a ``detail.ml`` **in its own unit**: what the model did to the lane's
    rate (used / reason / ``y_hat`` / interval), with ``raw_s`` = the
    analytical compute seconds of the lane (``items x`` the raw seconds per
    item - the raw planner's very formula), so ``raw_s x e^y_hat`` is the
    term's seconds and ``raw_prediction`` returns its uncalibrated seconds.
    The rate's own block (seconds per item) is nested as ``ml.rate`` - what
    ``ml/targets`` reads the chunk_rate's raw from."""
    out: list[SetEstimate] = []
    for item in singles:
        compute = item.estimate.compute
        node_id = item.node_ids[0]
        rate_block = rate_blocks.get(node_id)
        if rate_block is None or node_id not in raw_spi:
            out.append(item)
            continue
        block: dict[str, Any] = {k: v for k, v in rate_block.items() if k != "raw_s"}
        if block.get("used") is True:
            pure = 1.0 / raw_spi[node_id]
            block["raw_s"] = stored_raw("compute", bounded(max(1, items) / pure))
        block["rate"] = dict(rate_block)
        fixed = replace(compute, detail={**compute.detail, "ml": block})
        out.append(replace(item, estimate=replace(item.estimate, compute=fixed)))
    return out


def _ml_combine(items: Sequence[SetEstimate], session: MlSession) -> list[SetEstimate]:
    """Every costed candidate's ``combine`` with the D336 precedence (one
    predictor call): the combine node and lane count are the candidate's
    own. Never calibrated (V0.14: ``no_phase``), so the fallback is raw."""
    contexts = []
    known: list[bool] = []
    for item in items:
        term = item.estimate.extra["combine"]
        node_id = term.detail.get("combine_node_id")
        # V0.15 G5a (G4 reviewer debt): no combine node -> no features; the
        # term keeps its fallback with reason combine_node_unknown (never a
        # silent guess of the first lane's node)
        known.append(isinstance(node_id, str) and bool(node_id))
        if not known[-1]:
            continue
        contexts.append(
            TermContext(
                term="combine",
                node_id=str(node_id),
                raw_s=stored_raw("combine", term.seconds),
                source=term.source.value,
                detail=term.detail,
                lanes=len(item.active),
                chunk_items=item.chunk.items,
            )
        )
    resolved = iter(session.resolve(contexts)) if contexts else iter(())
    outcomes = [
        next(resolved) if ok else session.skip("combine", MlReason.COMBINE_NODE_UNKNOWN)
        for ok in known
    ]
    out: list[SetEstimate] = []
    for item, outcome in zip(items, outcomes, strict=True):
        term = item.estimate.extra["combine"]
        fixed = ml_term(
            term, outcome, name="combine", version=session.version, fallback=term
        )
        extra = {**item.estimate.extra, "combine": fixed}
        out.append(replace(item, estimate=replace(item.estimate, extra=extra)))
    return out


# --- end V0.15 G4 ----------------------------------------------------------------


def decide_split(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    policy: str,
    feasible: Sequence[tuple[NodeSnapshot, bool, Slot]],
    *,
    now: dt.datetime | None = None,
    calibration: Calibration | None = None,
    ml: MlSession | None = None,
) -> SplitOutcome:
    """The split job's feasible rows (singles + sets) ranked, its rejected
    ``split`` row if any, the chosen candidate and its plan (module
    docstring). ``feasible`` = ``(node, is_local, slot)`` in node-id order."""
    split = request.split
    if split is None:
        raise ValueError("decide_split needs a split job")
    config = snapshot.config.split
    margin = min(max(config.split_margin, 0.0), MAX_MARGIN)
    lanes = [
        lane(request, node, slot, snapshot, local=local, now=now)
        for node, local, slot in feasible
    ]
    # V0.14 G2 (D293): each lane's stage + env x its node's factors, before
    # the sets are generated and costed (start times, water filling, margin)
    raw_spi = {item.node_id: item.rate.seconds_per_item for item in lanes}
    rate_blocks: dict[str, dict[str, Any]] = {}
    if ml is not None:  # V0.15 G4 (D336): ML -> calibration -> raw per term
        lanes, rate_blocks = _ml_lanes(request, snapshot, lanes, ml, calibration)
    elif calibration is not None:
        family = calibration.family_of(request.fingerprint)
        lanes = [calibrate_lane(item, calibration, family=family) for item in lanes]
    generated = candidates(request, snapshot, lanes=lanes)
    by_lane = {item.node_id: item for item in lanes}
    singles = [
        estimate_split(
            request,
            [lone],
            snapshot,
            _single_chunk(split, lone, request, snapshot),
            now=now,
        )
        for lone in (by_lane[c.node_id] for c in generated.singles)
    ]
    forced = generated.forced
    sets: list[SetEstimate] = []
    for candidate in generated.sets:
        members = [by_lane[node_id] for node_id in candidate.node_set]
        chunk = set_chunk(request, members, snapshot)  # V0.14 G3: shared rule
        sets.append(estimate_split(request, members, snapshot, chunk, now=now))
    forced_row = next((_refused_row(c) for c in generated.refused_sets), None)
    if ml is not None:  # V0.15 G4 (D336): the combine of every candidate
        singles = _ml_compute(singles, rate_blocks, raw_spi, split.items)
        both = _ml_combine([*singles, *sets], ml)
        singles, sets = both[: len(singles)], both[len(singles) :]
    # V0.14 G3 (D296): the risk term - the margin test compares risked totals
    singles = [_risked(s, request, snapshot) for s in singles]
    sets = [_risked(s, request, snapshot) for s in sets]

    best_single = min(singles, key=lambda s: (s.total_s, s.node_ids), default=None)
    sets.sort(key=lambda s: (s.total_s, len(s.lanes), s.node_ids))
    best_set = sets[0] if sets else None
    overall = _margin(best_set, best_single, margin, forced)

    by_node = {s.node_ids[0]: s for s in singles}
    scored = [
        Scored(
            node=s.lanes[0].node,
            slot=s.lanes[0].slot,
            local=s.lanes[0].local,
            estimate=s.estimate,
        )
        for s in singles
    ]
    # V0.14 G3 (D295): cost_model ranks by the job's objective
    objective = objective_of(request.objective)
    reads_objective = (
        policy == COST_MODEL and objective != "min_time" and forced != FORCED_SET
    )
    changed = False
    if reads_objective:
        ranked, _ = rank_singles(
            objective,
            scored,
            prefer_local_margin=snapshot.config.prefer_local_margin,
            bytes_of=lambda item: set_bytes_moved(by_node[item.node.node_id], request),
        )
    else:
        ranked = rank(policy, request, snapshot, scored)
    modes = {c.node_id: c.stored_mode for c in generated.singles}
    single_rows = [
        _row(by_node[r.node_id], modes[r.node_id], score=r.score) for r in ranked
    ]
    # V1.0 G2 (D418 A6): with Mode 1 disabled and the Local lane unusable
    # there is no single to compare with - a set is then the only plan (never
    # reached otherwise: every lane of a set is also a single)
    use_set = best_set is not None and (
        overall.passed or forced == FORCED_SET or best_single is None
    )
    chosen_set = best_set
    if reads_objective:
        time_pick = _pick_key(best_set if use_set else best_single, use_set)
        chosen_set, use_set = _objective_set(
            objective, request, snapshot, sets, singles, by_node, ranked, margin
        )
        final = (
            chosen_set
            if use_set
            else by_node.get(ranked[0].node_id)
            if ranked
            else None
        )
        changed = _pick_key(final, use_set) != time_pick
        if chosen_set is not None and use_set:
            overall = _margin(chosen_set, best_single, margin, forced)
            sets = [chosen_set, *(s for s in sets if s is not chosen_set)]
    set_rows = [
        _row(
            s,
            "split",
            score=round(s.total_s, _ROUND),
            margin=_margin(s, best_single, margin),
        )
        for s in sets
    ]
    ordered = [*set_rows, *single_rows] if use_set else [*single_rows, *set_rows]
    if forced == FORCED_SET and not use_set:
        ordered = []  # the forced set cannot run now: wait, never something else
    rows = [replace(row, rank=index + 1) for index, row in enumerate(ordered)]
    if forced == FORCED_SET and not use_set:
        rows = [*set_rows, *single_rows]  # shown, unranked
    if forced_row is not None:
        rows.append(forced_row)
    chosen = rows[0] if ordered else None
    plan: SplitPlan | None = None
    if chosen is not None:
        picked = (
            chosen_set
            if use_set and chosen_set is not None
            else by_node[chosen.node_id]
        )
        plan = SplitPlan(
            split=chosen.mode == "split",
            # a forced set runs on every node of it, even one predicted idle
            lanes=tuple(
                p
                for p in picked.lane_plans()
                if p.active or (forced == FORCED_SET and use_set)
            ),
            chunk_items=picked.chunk.items,
            chunks=picked.chunk.chunks,
            predicted_makespan_s=round(picked.total_s, _ROUND),
            margin=overall,
        )
    return SplitOutcome(
        rows,
        chosen,
        plan,
        objective_changed=changed and chosen is not None,
        pruned=generated.pruned,
    )
