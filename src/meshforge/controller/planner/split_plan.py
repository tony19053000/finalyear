"""Stage 3 + 4 for a split job: singles vs Mode 3 sets, the margin test
(V0.10 G3, D265; ADR-0019 decision 5).

Called by :func:`~.plan.plan` with the nodes that passed feasibility. The
candidates of a split job are

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
from dataclasses import replace
from typing import Final

from meshforge.controller.planner.policies import Scored, rank
from meshforge.controller.planner.split import (
    ChunkChoice,
    Lane,
    SetEstimate,
    choose_chunk_items,
    chunk_overhead,
    estimate_split,
    lane,
    prefix_sets,
)
from meshforge.controller.planner.types import (
    CandidateEval,
    ClusterSnapshot,
    MarginTest,
    Mode,
    NodeSnapshot,
    PlanRequest,
    RejectCode,
    Rejection,
    Slot,
    SplitInfo,
    SplitPlan,
)

__all__ = ["FORCED_SET", "MAX_MARGIN", "SplitOutcome", "decide_split"]

FORCED_SET: Final = "node_set"
"""``MarginTest.forced`` of a campaign's forced ``mode3`` run (V0.10 G8)."""
MAX_MARGIN: Final = 0.95
"""``split_margin`` is read inside ``[0, MAX_MARGIN]`` (settings validate it;
a hand-built config cannot make splitting impossible by accident)."""
_ROUND: Final = 6

type SplitOutcome = tuple[list[CandidateEval], CandidateEval | None, SplitPlan | None]
"""(ranked + rejected set rows, the chosen candidate, what to run)."""


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


def _set_chunk(
    split: SplitInfo,
    group: Sequence[Lane],
    request: PlanRequest,
    snapshot: ClusterSnapshot,
) -> ChunkChoice:
    overhead = chunk_overhead(request, snapshot.config.split)
    return choose_chunk_items(
        split.items,
        fastest_per_item_s=min(item.per_item_s for item in group),
        lanes=len(group),
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


def _forced_row(
    code: RejectCode, text: str, lanes: Sequence[Lane], size: int
) -> CandidateEval | None:
    ordered = sorted(lanes, key=lambda item: (item.per_item_s, item.node_id))
    if not ordered:
        return None
    group = ordered[: max(1, size)]
    return CandidateEval(
        node_id=group[0].node_id,
        mode="split",
        feasible=False,
        rejection=Rejection(code, text),
        node_set=tuple(item.node_id for item in group),
    )


def decide_split(
    request: PlanRequest,
    snapshot: ClusterSnapshot,
    policy: str,
    feasible: Sequence[tuple[NodeSnapshot, bool, Slot]],
    *,
    now: dt.datetime | None = None,
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
    singles = [
        estimate_split(
            request,
            [lone],
            snapshot,
            _single_chunk(split, lone, request, snapshot),
            now=now,
        )
        for lone in lanes
    ]
    forced: str | None = None
    forced_row: CandidateEval | None = None
    sets: list[SetEstimate] = []
    if request.pinned_node_id is not None:
        forced = "pinned"
    elif request.node_set:
        # V0.10 G8 (D271): a campaign's forced mode3 run - every node of its
        # set, or nothing (never a different set, never "don't split")
        forced = FORCED_SET
        wanted = set(request.node_set)
        if {item.node_id for item in lanes} == wanted and len(lanes) >= 2:
            members = sorted(lanes, key=lambda item: (item.per_item_s, item.node_id))
            chunk = _set_chunk(split, members, request, snapshot)
            sets.append(estimate_split(request, members, snapshot, chunk, now=now))
        else:
            ready = len(wanted & {item.node_id for item in lanes})
            forced_row = _forced_row(
                RejectCode.SPLIT_TOO_FEW_NODES,
                f"the forced node set: {ready} of {len(wanted)} node(s) can take a "
                "lane now",
                lanes,
                len(lanes),
            )
    elif split.items < 2 * split.min_nodes:
        forced = RejectCode.SPLIT_TOO_SMALL.value
        forced_row = _forced_row(
            RejectCode.SPLIT_TOO_SMALL,
            f"{split.items} item(s) are fewer than 2 x split.min_nodes "
            f"({split.min_nodes}): the job runs unsplit",
            lanes,
            split.min_nodes,
        )
    elif len(lanes) < split.min_nodes:
        forced = RejectCode.SPLIT_TOO_FEW_NODES.value
        forced_row = _forced_row(
            RejectCode.SPLIT_TOO_FEW_NODES,
            f"only {len(lanes)} node(s) can take a lane now; split.min_nodes is "
            f"{split.min_nodes}",
            lanes,
            len(lanes),
        )
    else:
        for group in prefix_sets(
            lanes,
            items=split.items,
            min_nodes=split.min_nodes,
            max_nodes=split.max_nodes,
        ):
            chunk = _set_chunk(split, group, request, snapshot)
            sets.append(estimate_split(request, group, snapshot, chunk, now=now))

    best_single = min(singles, key=lambda s: (s.total_s, s.node_ids), default=None)
    sets.sort(key=lambda s: (s.total_s, len(s.lanes), s.node_ids))
    best_set = sets[0] if sets else None
    overall = _margin(best_set, best_single, margin, forced)

    by_node = {s.node_ids[0]: s for s in singles}
    ranked = rank(
        policy,
        request,
        snapshot,
        [
            Scored(
                node=s.lanes[0].node,
                slot=s.lanes[0].slot,
                local=s.lanes[0].local,
                estimate=s.estimate,
            )
            for s in singles
        ],
    )
    single_rows = [
        _row(
            by_node[r.node_id],
            "local" if by_node[r.node_id].lanes[0].local else "offload",
            score=r.score,
        )
        for r in ranked
    ]
    set_rows = [
        _row(
            s,
            "split",
            score=round(s.total_s, _ROUND),
            margin=_margin(s, best_single, margin),
        )
        for s in sets
    ]
    use_set = best_set is not None and (overall.passed or forced == FORCED_SET)
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
            best_set if use_set and best_set is not None else by_node[chosen.node_id]
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
    return rows, chosen, plan
