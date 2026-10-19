"""Mode 3 cost model: item rates, chunk size, predicted makespan of a node set
(V0.10 G3, D265, D275). Pure, like the rest of the planner.

**A lane** is one node of a split job (D259): it stages the project once, builds
the environment once, then pulls chunks; each chunk runs the entrypoint as a
fresh process. Per node ``n``:

* ``r_n`` - items/s of compute (:func:`item_rate`): the fingerprint's **chunk
  history** on ``n`` (``history``), else its whole-run history on ``n`` divided
  over the job's items (``history``), else another node's rate scaled by the
  hardware-score ratio (``scaled``, the D131 least-work rule), else the
  fastest other node's rate as is (``history``, ``scaled: false``), else
  ``default_compute_s`` for the whole job (``assumed``).
* ``f_n`` - seconds per item to fetch the item's bytes (D264: a lane fetches
  only its chunks' items): bytes per item over the best source of the items'
  tree (:func:`~.estimator.tree_bandwidth`); 0 when the node holds the tree.
* ``start_n`` - stage (the job's trees **minus** the items) + env, the
  estimator's own terms.
* ``t_o`` - per-chunk overhead (process start + stage + upload): chunk history
  (``history``), else ``[controller.split] chunk_overhead_s`` (``assumed``).

A chunk of ``c`` items takes ``c x (1/r_n + f_n) + t_o`` on ``n``, so a lane
completes ``r'_n = c / (c x (1/r_n + f_n) + t_o)`` items/s.

**Makespan of a set** (:func:`estimate_split`). Lanes start pulling when their
own stage + env is done, so the work finishes at the ``M`` where
``sum_n max(0, M - start_n) x r'_n = items`` (water filling; a lane whose start
is after ``M`` gets no chunk and is marked inactive). With all starts equal
this is D265's ``max(start) + items / sum r'``; with unequal starts it does not
blame the whole set for one slow environment build. Then::

    T(S) = M + tail + combine + output

* ``tail`` - lanes do not finish together; pulling keeps the gap under one
  chunk: ``tail_frac x`` the set's longest chunk time - and at least until
  every active lane has finished the first chunk it pulled (a slow node's one
  chunk can outlast the fast nodes' whole share). 0 for one lane.
* ``combine`` - the combine attempt (D268): ``t_o`` + the other lanes' share
  of the output moved to the combine node.
* ``output`` - the estimator's output term on the combine node (the submitter
  when it is a lane, else the fastest lane).

``M`` is shown as its parts: ``transfer`` and ``env`` (the lanes' start times
weighted by their rates), ``compute`` (``items / sum r_n``), ``items_fetch``,
``chunk_overhead`` - so the terms add up to the total exactly and each keeps
the **weakest** source behind it (checklist 18).

**Chunk size** (:func:`choose_chunk_items`, ``chunk: auto``): about
``target_chunk_s`` on the fastest lane, raised until the overhead is at most
``max_overhead_frac`` of a chunk there, then lowered until there are at least
``min_chunks_per_node`` chunks per lane (tail balance wins), never more than
``max_chunks`` chunks. ``chunk: {items: N}`` is used as given.

**Candidate sets** (:func:`prefix_sets`): lanes ordered by ``1/r_n + f_n``
(fastest first, ties on node id); the sets are the prefixes of sizes
``min_nodes .. min(max_nodes, N)`` - O(N) sets, each holding the fastest node.
When every lane starts together a larger ``sum r'`` is never worse at equal
size, so the best set of each size is its prefix - the unit tests check it
against all 2^N subsets for N <= 5. Unequal starts (one node must build its
environment) break that, so the prefixes of a second order - by the time the
node alone would need, ``start + items x (1/r_n + f_n)`` - are added (still
O(N)); against brute force the best plan is then within a few per cent (the
residue is chunk sizing around a lane predicted to get no chunk).
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Final

from meshforge.controller.planner.estimator import (
    bounded,
    controller_bandwidth,
    env_term,
    hardware_score,
    output_term,
    transfer_term,
    tree_bandwidth,
    weakest,
)
from meshforge.controller.planner.types import (
    ClusterSnapshot,
    Estimate,
    EstimateTerm,
    LanePlan,
    NodeSnapshot,
    PlanRequest,
    Slot,
    SplitConfig,
    SplitInfo,
    TermSource,
    TreeNeed,
)

__all__ = [
    "MIN_SECONDS_PER_ITEM",
    "ChunkChoice",
    "Lane",
    "Rate",
    "SetEstimate",
    "choose_chunk_items",
    "chunk_overhead",
    "estimate_split",
    "item_rate",
    "lane",
    "prefix_sets",
]

MIN_SECONDS_PER_ITEM: Final = 1e-9
"""No item is predicted faster than this: every rate is finite."""
_ROUND: Final = 6


@dataclass(frozen=True, slots=True)
class Rate:
    """``r_n`` and where it came from."""

    seconds_per_item: float
    source: TermSource
    detail: Mapping[str, Any] = field(default_factory=dict)

    @property
    def items_s(self) -> float:
        return 1.0 / self.seconds_per_item


@dataclass(frozen=True, slots=True)
class Lane:
    """One node as a lane of this job (module docstring)."""

    node: NodeSnapshot
    slot: Slot
    local: bool
    rate: Rate
    fetch: EstimateTerm
    """Seconds **per item** to fetch its bytes (``detail``: where from)."""
    stage: EstimateTerm
    env: EstimateTerm

    @property
    def node_id(self) -> str:
        return self.node.node_id

    @property
    def start_s(self) -> float:
        return bounded(self.stage.seconds + self.env.seconds)

    @property
    def per_item_s(self) -> float:
        """``1/r_n + f_n``: what ordering and chunk sizing use."""
        return self.rate.seconds_per_item + self.fetch.seconds


@dataclass(frozen=True, slots=True)
class ChunkChoice:
    items: int
    """Items per chunk."""
    chunks: int
    rules: tuple[str, ...]
    """Which rules set it, in order: ``fixed`` | ``one_chunk`` | ``target``,
    ``overhead``, ``balance``, ``max_chunks``."""


@dataclass(frozen=True, slots=True)
class SetEstimate:
    """A candidate (one lane or a set) with its chunking and prediction."""

    lanes: tuple[Lane, ...]
    active: frozenset[str]
    """Node ids predicted to run at least one chunk."""
    chunk: ChunkChoice
    estimate: Estimate

    @property
    def total_s(self) -> float:
        return self.estimate.total_s

    @property
    def node_ids(self) -> tuple[str, ...]:
        return tuple(lane.node_id for lane in self.lanes)

    def lane_plans(self) -> tuple[LanePlan, ...]:
        return tuple(
            LanePlan(
                node_id=lane.node_id,
                slot=lane.slot,
                local=lane.local,
                rate_items_s=round(lane.rate.items_s, _ROUND),
                rate_source=lane.rate.source,
                start_s=round(lane.start_s, _ROUND),
                active=lane.node_id in self.active,
            )
            for lane in self.lanes
        )


# ---------------------------------------------------------------------- rates


def _per_item(seconds: float, items: int) -> float | None:
    """``seconds`` for ``items`` as finite seconds per item, None if unusable."""
    if items <= 0 or not math.isfinite(seconds) or seconds <= 0.0:
        return None
    return max(MIN_SECONDS_PER_ITEM, bounded(seconds) / items)


def _known(request: PlanRequest, split: SplitInfo) -> dict[str, tuple[float, str, int]]:
    """node -> (seconds per item, ``chunk_history`` | ``run_history``, samples)."""
    history = request.history
    out: dict[str, tuple[float, str, int]] = {}
    for node_id, rate in sorted(history.item_rate_by_node.items()):
        if math.isfinite(rate) and rate > 0.0:
            spi = max(MIN_SECONDS_PER_ITEM, bounded(1.0 / rate))
            samples = history.item_rate_samples_by_node.get(node_id, 0)
            out[node_id] = (spi, "chunk_history", samples)
    for node_id, seconds in sorted(history.by_node_s.items()):
        if node_id in out:
            continue
        spi_run = _per_item(seconds, split.items)
        if spi_run is not None:
            samples = history.samples_by_node.get(node_id, 0)
            out[node_id] = (spi_run, "run_history", samples)
    return out


def item_rate(
    request: PlanRequest, node: NodeSnapshot, slot: Slot, snapshot: ClusterSnapshot
) -> Rate:
    """``r_n`` of ``node`` (module docstring) - the rules of the estimator's
    ``compute`` term, per item."""
    split = request.split
    if split is None:
        raise ValueError("item_rate needs a split job")
    known = _known(request, split)
    own = known.get(node.node_id)
    if own is not None:
        spi, kind, samples = own
        return Rate(spi, TermSource.HISTORY, {"from": kind, "samples": samples})
    others = sorted(
        (spi, node_id, kind, samples)
        for node_id, (spi, kind, samples) in known.items()
        if node_id != node.node_id
    )
    mine = hardware_score(node, slot)
    if not others:
        assumed = _per_item(snapshot.config.default_compute_s, split.items)
        assumed = assumed or MIN_SECONDS_PER_ITEM
        # No history anywhere: the assumption is for the strongest node; a
        # node with a lower hardware score is assumed proportionally slower
        # (G3 review item 1 - otherwise a first run splits onto slow nodes).
        best = max(
            (
                score
                for other in snapshot.nodes
                if (score := hardware_score(other, slot)) is not None
            ),
            default=None,
        )
        if mine is None or best is None:
            return Rate(assumed, TermSource.ASSUMED, {})
        return Rate(
            max(MIN_SECONDS_PER_ITEM, bounded(assumed * best / mine)),
            TermSource.ASSUMED,
            {"scaled": True, "score_ratio": round(best / mine, 4)},
        )
    scored = sorted(
        (spi * theirs, node_id, kind, samples, theirs)
        for spi, node_id, kind, samples in others
        if (theirs := hardware_score(snapshot.node(node_id), slot)) is not None
    )
    if mine is not None and scored:
        work, other_id, kind, samples, theirs = scored[0]
        return Rate(
            max(MIN_SECONDS_PER_ITEM, bounded(work / mine)),
            TermSource.SCALED,
            {
                "from": kind,
                "from_node_id": other_id,
                "samples": samples,
                "scaled": True,
                "score_ratio": round(theirs / mine, 4),
            },
        )
    spi, other_id, kind, samples = others[0]
    return Rate(
        spi,
        TermSource.HISTORY,
        {"from": kind, "from_node_id": other_id, "samples": samples, "scaled": False},
    )


def chunk_overhead(request: PlanRequest, config: SplitConfig) -> EstimateTerm:
    """``t_o``: measured per-chunk overhead, else the configured assumption."""
    history = request.history
    measured = history.chunk_overhead_s
    if measured is not None and math.isfinite(measured) and measured >= 0.0:
        return EstimateTerm(
            bounded(measured),
            TermSource.HISTORY,
            {"samples": history.chunk_overhead_samples},
        )
    return EstimateTerm(bounded(config.chunk_overhead_s), TermSource.ASSUMED, {})


# ---------------------------------------------------------------------- lanes


def _holds_items(request: PlanRequest, node: NodeSnapshot, *, local: bool) -> bool:
    split = request.split
    if split is None or split.items_tree is None:
        return True
    if split.items_tree in node.cached:
        return True
    # D109: a seeded Local node holds the project tree (the first one)
    return (
        local
        and request.seeded
        and bool(request.trees)
        and request.trees[0].key == split.items_tree
    )


def _without_items(request: PlanRequest, split: SplitInfo) -> PlanRequest:
    """``request`` whose items tree is staged without the items (D264)."""
    if split.items_tree is None or not split.item_bytes:
        return request
    removed = max(0, split.item_bytes)
    return replace(
        request,
        trees=tuple(
            TreeNeed(tree.key, max(0, tree.size - removed))
            if tree.key == split.items_tree
            else tree
            for tree in request.trees
        ),
    )


def _fetch(
    request: PlanRequest,
    split: SplitInfo,
    node: NodeSnapshot,
    snapshot: ClusterSnapshot,
    *,
    local: bool,
    now: dt.datetime | None,
) -> EstimateTerm:
    per_item = split.bytes_per_item
    if per_item <= 0.0 or split.items_tree is None:
        return EstimateTerm(0.0, TermSource.MEASURED, {"bytes_per_item": 0})
    if _holds_items(request, node, local=local):
        return EstimateTerm(
            0.0,
            TermSource.MEASURED,
            {"bytes_per_item": round(per_item, 1), "held": True},
        )
    bw, source, detail = tree_bandwidth(
        TreeNeed(split.items_tree, split.item_bytes or 0), node, snapshot, now=now
    )
    return EstimateTerm(
        bounded(per_item / bw),
        weakest(TermSource.MEASURED, source),
        {"bytes_per_item": round(per_item, 1), "bw_bytes_s": round(bw, 1), **detail},
    )


def lane(
    request: PlanRequest,
    node: NodeSnapshot,
    slot: Slot,
    snapshot: ClusterSnapshot,
    *,
    local: bool,
    now: dt.datetime | None = None,
) -> Lane:
    """``node`` as a lane of the split job ``request``."""
    split = request.split
    if split is None:
        raise ValueError("lane needs a split job")
    staged = _without_items(request, split)
    return Lane(
        node=node,
        slot=slot,
        local=local,
        rate=item_rate(request, node, slot, snapshot),
        fetch=_fetch(request, split, node, snapshot, local=local, now=now),
        stage=transfer_term(staged, node, snapshot, local=local, now=now),
        env=env_term(request, node, snapshot),
    )


def prefix_sets(
    lanes: Sequence[Lane], *, items: int, min_nodes: int, max_nodes: int | None
) -> list[tuple[Lane, ...]]:
    """The candidate sets (module docstring): the prefixes of sizes
    ``min_nodes .. min(max_nodes, len(lanes))`` of two orders - by rate, and
    by the time the node alone would need (``start + items x per item``) - each
    set once, in that order; none when fewer lanes."""
    top = len(lanes) if max_nodes is None else min(max_nodes, len(lanes))

    def by_rate(item: Lane) -> tuple[float, str]:
        return (item.per_item_s, item.node_id)

    orders = (
        sorted(lanes, key=by_rate),
        sorted(
            lanes,
            key=lambda item: (
                item.start_s + items * item.per_item_s,
                item.per_item_s,
                item.node_id,
            ),
        ),
    )
    out: list[tuple[Lane, ...]] = []
    seen: set[frozenset[str]] = set()
    for ordered in orders:
        for size in range(max(min_nodes, 2), top + 1):
            group = tuple(sorted(ordered[:size], key=by_rate))  # fastest first
            key = frozenset(item.node_id for item in group)
            if key not in seen:
                seen.add(key)
                out.append(group)
    return out


# -------------------------------------------------------------------- chunks


def choose_chunk_items(
    items: int,
    *,
    fastest_per_item_s: float,
    lanes: int,
    overhead_s: float,
    config: SplitConfig,
    fixed: int | None = None,
) -> ChunkChoice:
    """Items per chunk (module docstring). ``lanes`` = the set's size."""
    items = max(1, items)
    if fixed is not None:
        size = max(1, min(fixed, items))
        return ChunkChoice(size, math.ceil(items / size), ("fixed",))
    fast = max(MIN_SECONDS_PER_ITEM, fastest_per_item_s)
    if not math.isfinite(fast):
        fast = MIN_SECONDS_PER_ITEM
    rules = ["target"]
    target = config.target_chunk_s / fast
    size = max(1, int(min(float(items), round(target))))
    frac = config.max_overhead_frac
    if 0.0 < frac < 1.0 and overhead_s > 0.0:
        need = math.ceil(min(float(items), overhead_s * (1.0 - frac) / (frac * fast)))
        if need > size:
            size, rules = need, [*rules, "overhead"]
    balance = max(1, items // max(1, config.min_chunks_per_node * max(1, lanes)))
    if size > balance:
        size, rules = balance, [*rules, "balance"]
    least = math.ceil(items / max(1, config.max_chunks))
    if size < least:
        size, rules = least, [*rules, "max_chunks"]
    size = max(1, min(size, items))
    return ChunkChoice(size, math.ceil(items / size), tuple(rules))


# ------------------------------------------------------------------ estimate


def _weighted(
    name: str, terms: Sequence[tuple[EstimateTerm, float, str]]
) -> EstimateTerm:
    """``terms`` (term, rate weight, node id) as one rate-weighted term; one
    lane keeps its own detail (``compute explain`` shows where from)."""
    if len(terms) == 1:
        return terms[0][0]
    total = sum(weight for _, weight, _ in terms)
    seconds = sum(term.seconds * weight for term, weight, _ in terms) / total
    source = weakest(*(term.source for term, _, _ in terms))
    longest = max(terms, key=lambda item: (item[0].seconds, item[2]))
    return EstimateTerm(
        bounded(seconds),
        source,
        {
            "weighted_by": "rate",
            "lanes": len(terms),
            f"max_{name}_s": round(longest[0].seconds, _ROUND),
            "max_node_id": longest[2],
        },
    )


def _water_fill(
    lanes: Sequence[Lane], rates: Mapping[str, float], items: int
) -> tuple[float, list[Lane]]:
    """``(M, active lanes)``: ``sum max(0, M - start) x r' = items``."""
    ordered = sorted(lanes, key=lambda item: (item.start_s, item.node_id))
    weight = 0.0
    weighted_start = 0.0
    active: list[Lane] = []
    finish = math.inf
    for index, item in enumerate(ordered):
        weight += rates[item.node_id]
        weighted_start += item.start_s * rates[item.node_id]
        active.append(item)
        finish = (items + weighted_start) / weight
        following = ordered[index + 1].start_s if index + 1 < len(ordered) else None
        if following is None or finish <= following:
            break
    return finish, active


def estimate_split(
    request: PlanRequest,
    lanes: Sequence[Lane],
    snapshot: ClusterSnapshot,
    chunk: ChunkChoice,
    *,
    now: dt.datetime | None = None,
) -> SetEstimate:
    """``T(S)`` of the set ``lanes`` cut into ``chunk`` (module docstring)."""
    split = request.split
    if split is None or not lanes:
        raise ValueError("estimate_split needs a split job and at least one lane")
    config = snapshot.config.split
    items = max(1, split.items)
    overhead = chunk_overhead(request, config)
    size = chunk.items
    chunk_s = {
        item.node_id: bounded(size * item.per_item_s + overhead.seconds)
        for item in lanes
    }
    effective = {
        node_id: size / max(seconds, MIN_SECONDS_PER_ITEM)
        for node_id, seconds in chunk_s.items()
    }
    finish, active = _water_fill(lanes, effective, items)
    pure = sum(1.0 / item.rate.seconds_per_item for item in active)
    fetched = sum(1.0 / item.per_item_s for item in active)
    paced = sum(effective[item.node_id] for item in active)
    compute_s = bounded(items / pure)
    fetch_s = bounded(max(0.0, items / fetched - items / pure))
    overhead_total = bounded(max(0.0, items / paced - items / fetched))
    rate_source = weakest(*(item.rate.source for item in active))
    compute = EstimateTerm(
        compute_s,
        rate_source,
        {
            "items": items,
            "rate_items_s": round(pure, _ROUND),
            **(dict(active[0].rate.detail) if len(active) == 1 else {}),
        },
    )
    fetch = EstimateTerm(
        fetch_s,
        weakest(TermSource.MEASURED, *(item.fetch.source for item in active)),
        {"bytes_per_item": round(split.bytes_per_item, 1)},
    )
    chunk_term = EstimateTerm(
        overhead_total,
        overhead.source,
        {
            "overhead_s": round(overhead.seconds, _ROUND),
            "chunk_items": size,
            "chunks": chunk.chunks,
            "rules": list(chunk.rules),
        },
    )
    if len(active) > 1:
        longest = max(chunk_s[item.node_id] for item in active)
        # every active lane finishes at least the chunk it pulls first
        first_end = max(item.start_s + chunk_s[item.node_id] for item in active)
        tail = EstimateTerm(
            bounded(max(config.tail_frac * longest, first_end - finish)),
            weakest(rate_source, overhead.source),
            {
                "longest_chunk_s": round(longest, _ROUND),
                "first_chunk_end_s": round(first_end, _ROUND),
            },
        )
    else:
        tail = EstimateTerm(0.0, TermSource.MEASURED, {"lanes": 1})
    combiner = next((item for item in active if item.local), active[0])
    output = output_term(request, combiner.node, snapshot)
    combine = _combine(request, combiner, len(active), snapshot, overhead, now=now)
    weights = [(item, effective[item.node_id]) for item in active]
    estimate = Estimate(
        queue=EstimateTerm(0.0, TermSource.MEASURED, {"lanes": len(active)}),
        transfer=_weighted(
            "stage", [(item.stage, w, item.node_id) for item, w in weights]
        ),
        env=_weighted("env", [(item.env, w, item.node_id) for item, w in weights]),
        compute=compute,
        output=output,
        extra={
            "items_fetch": fetch,
            "chunk_overhead": chunk_term,
            "tail": tail,
            "combine": combine,
        },
    )
    return SetEstimate(
        lanes=tuple(lanes),
        active=frozenset(item.node_id for item in active),
        chunk=chunk,
        estimate=estimate,
    )


def _combine(
    request: PlanRequest,
    combiner: Lane,
    lanes: int,
    snapshot: ClusterSnapshot,
    overhead: EstimateTerm,
    *,
    now: dt.datetime | None,
) -> EstimateTerm:
    """D268: start the combine attempt (``t_o``) + fetch the other lanes'
    share of the chunk results to the combine node."""
    out = request.history.output_bytes
    known = out is not None and math.isfinite(out) and out >= 0.0
    if out is None or not known or out == 0.0 or lanes <= 1:
        return EstimateTerm(
            overhead.seconds,
            overhead.source if known else TermSource.ASSUMED,
            {"parts_bytes": 0, "combine_node_id": combiner.node_id},
        )
    moved = out * (lanes - 1) / lanes
    bw, source, _detail = controller_bandwidth(combiner.node, snapshot, now)
    return EstimateTerm(
        bounded(overhead.seconds + moved / bw),
        weakest(overhead.source, TermSource.HISTORY, source),
        {
            "parts_bytes": round(moved),
            "bw_bytes_s": round(bw, 1),
            "combine_node_id": combiner.node_id,
        },
    )
