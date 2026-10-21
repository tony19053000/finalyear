"""Stage 1: the **one** candidate generator (V0.14 G1, D290/D291; ADR-0007).

Every way the planner may run a request comes from :func:`candidates`; the
later stages (feasibility, costing, the policy, the margin test) only judge
what it generated. A :class:`Candidate` is ``(mode, node_set, plan_params)``:

* ``local`` - the submitter's own node (ARCHITECTURE §5: a baseline
  candidate, never a separate path);
* ``mode1`` - Mode 1 job offloading on one other node. For a split job this
  is the one-lane "don't split" plan (the same lane path as a split, D269);
* ``mode3`` - Mode 3 task splitting over an ordered node set (fastest first).

``mode2`` / ``mode4`` do not exist in this build (ADR-0012): an
:class:`~.types.AcceleratorRequest` gets **no** candidate and the typed
refusal :attr:`~.types.RejectCode.NOT_SUPPORTED_IN_BUILD`, so V0.11+ add them
here without touching a caller.

**By request kind** (:func:`request_kind`):

* :attr:`~.types.RequestKind.JOB` (and the first pass of a split job) - one
  candidate per node in node-id order: ``local`` for the node named by
  ``submitter_node_id``, ``mode1`` for every other. A *pinned* job (D103) has
  its one node; a campaign's forced ``mode3`` run (V0.10 G8) exactly the nodes
  of its set.
* :attr:`~.types.RequestKind.SPLIT` with ``lanes`` - the nodes that passed
  feasibility, costed as lanes (:func:`~.split.lane`): every lane as a
  one-lane ``local`` / ``mode1`` candidate, then the ``mode3`` sets:

  - pinned -> none (``forced: pinned``);
  - a forced set (``node_set``) -> that set, once every node of it can take a
    lane (``plan_params: {"forced": "node_set"}``), else a rejected set
    (``split_too_few_nodes``);
  - fewer than ``2 x min_nodes`` items -> a rejected set (``split_too_small``);
  - fewer than ``min_nodes`` lanes -> a rejected set (``split_too_few_nodes``);
  - otherwise the fastest-first prefix sets (:func:`~.split.prefix_sets`,
    O(N): the V0.10 G3 pruning) - over the lanes left after **dominance
    pruning** (V0.14 G3, D294, :func:`dominated_lanes`).

**Dominance pruning** (``PlannerConfig.prune_dominated``). Lane ``B`` is not a
set extender when an already-included lane ``A`` (earlier in rate order, not
pruned itself) dominates it - no worse on both lane terms (start =
stage + env, seconds per item = ``1/r + f``) and better on one - **and** has
the capacity to take ``B``'s share: ``A`` alone would finish every item before
``B`` could even start (``start_A + items x per_item_A + chunks x t_o <=
start_B``, with the most chunks the chunk rules allow). ``B`` would then get no
chunk in any set it is in (the water filling of :func:`~.split.estimate_split`
marks it inactive), so a set with ``B`` is the same work as the set without it
plus an idle lane: every such set is strictly dominated. Equal lanes never
dominate each other, ``B`` stays a one-lane candidate, and a forced or pinned
job is never pruned. The decision records each pruned node with the one that
dominates it (:attr:`~.types.Decision.pruned`).

  Mode 3 sets need the lanes' rates to be ordered, which is why a split job's
  generator runs on the feasible, costed lanes: the sets are a function of the
  nodes, never of the policy.

Stored values do not change: :attr:`Candidate.stored_mode` maps ``mode1`` to
the ``offload`` and ``mode3`` to the ``split`` that ``plan_decision`` rows,
run records and ``compute explain`` have always shown (D291).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from meshforge.controller.planner.split import Lane, chunk_overhead, prefix_sets
from meshforge.controller.planner.types import (
    AcceleratorRequest,
    ClusterSnapshot,
    Mode,
    PlanRequest,
    Pruned,
    RejectCode,
    Rejection,
    RequestKind,
    SplitConfig,
)

__all__ = [
    "FORCED_SET",
    "STORED_MODE",
    "Candidate",
    "CandidateMode",
    "CandidateSet",
    "candidates",
    "dominated_lanes",
    "request_kind",
]

type CandidateMode = Literal["local", "mode1", "mode3"]

STORED_MODE: Final[Mapping[CandidateMode, Mode]] = {
    "local": "local",
    "mode1": "offload",
    "mode3": "split",
}
"""A candidate's mode as stored (``plan_decision.candidates[].mode``)."""

FORCED_SET: Final = "node_set"
"""``plan_params["forced"]`` / ``MarginTest.forced`` of a campaign's forced
``mode3`` run (V0.10 G8)."""

_PINNED: Final = "pinned"


@dataclass(frozen=True, slots=True)
class Candidate:
    """One way to run the request (module docstring)."""

    mode: CandidateMode
    node_set: tuple[str, ...]
    """The node(s), in order: one for ``local`` / ``mode1``; a ``mode3`` set
    fastest first (its head leads the stored row)."""
    plan_params: Mapping[str, Any] = field(default_factory=dict)
    """What the generator fixed beyond the nodes (``forced``); chunking and
    the lanes' shares are the costing stage's."""
    rejection: Rejection | None = None
    """A ``mode3`` set the generator itself refuses (``split_too_small``,
    ``split_too_few_nodes``) - stored as a rejected ``split`` row. Node
    feasibility is stage 2's, never set here."""

    @property
    def node_id(self) -> str:
        return self.node_set[0]

    @property
    def stored_mode(self) -> Mode:
        return STORED_MODE[self.mode]


@dataclass(frozen=True, slots=True)
class CandidateSet:
    """What :func:`candidates` generated for one request."""

    kind: RequestKind
    candidates: tuple[Candidate, ...] = ()
    refused: Rejection | None = None
    """The request as a whole cannot be planned by this build (no candidate
    at all): :attr:`~.types.RejectCode.NOT_SUPPORTED_IN_BUILD`."""
    forced: str | None = None
    """Split jobs: why the Mode 3 sets are not the free prefix sets -
    ``pinned``, ``node_set``, ``split_too_small`` or ``split_too_few_nodes``
    (``MarginTest.forced``); None = they are."""
    pruned: tuple[Pruned, ...] = ()
    """V0.14 G3 (D294): lanes no Mode 3 set was extended with."""

    @property
    def singles(self) -> tuple[Candidate, ...]:
        """``local`` / ``mode1`` candidates, in generation order."""
        return tuple(c for c in self.candidates if c.mode != "mode3")

    @property
    def sets(self) -> tuple[Candidate, ...]:
        """``mode3`` candidates the generator did not refuse."""
        return tuple(
            c for c in self.candidates if c.mode == "mode3" and c.rejection is None
        )

    @property
    def refused_sets(self) -> tuple[Candidate, ...]:
        return tuple(
            c for c in self.candidates if c.mode == "mode3" and c.rejection is not None
        )


def request_kind(request: PlanRequest | AcceleratorRequest) -> RequestKind:
    if isinstance(request, AcceleratorRequest):
        return RequestKind.ACCELERATOR
    return RequestKind.JOB if request.split is None else RequestKind.SPLIT


def _single(node_id: str, request: PlanRequest) -> Candidate:
    mode: CandidateMode = "local" if node_id == request.submitter_node_id else "mode1"
    return Candidate(mode, (node_id,))


def _nodes(request: PlanRequest, snapshot: ClusterSnapshot) -> tuple[Candidate, ...]:
    nodes = sorted(snapshot.nodes, key=lambda n: n.node_id)
    if request.pinned_node_id is not None:
        nodes = [n for n in nodes if n.node_id == request.pinned_node_id]
    elif request.node_set:  # V0.10 G8: a forced mode3 run - exactly its set
        wanted = set(request.node_set)
        nodes = [n for n in nodes if n.node_id in wanted]
    return tuple(_single(n.node_id, request) for n in nodes)


def _by_rate(lane: Lane) -> tuple[float, str]:
    return (lane.per_item_s, lane.node_id)


def _refused_set(
    code: RejectCode,
    text: str,
    lanes: Sequence[Lane],
    size: int,
    params: Mapping[str, Any] | None = None,
) -> tuple[Candidate, ...]:
    """The set the job would have split over, refused (none without lanes)."""
    ordered = sorted(lanes, key=_by_rate)
    if not ordered:
        return ()
    group = ordered[: max(1, size)]
    return (
        Candidate(
            "mode3",
            tuple(item.node_id for item in group),
            dict(params or {}),
            rejection=Rejection(code, text),
        ),
    )


# --- V0.14 G3 (D294): dominance pruning -------------------------------------


def _most_chunks(items: int, chunk_items: int | None, config: SplitConfig) -> int:
    """The most chunks any set's chunking could cut ``items`` into."""
    items = max(1, items)
    if chunk_items is not None:
        return -(-items // max(1, min(chunk_items, items)))
    return max(1, min(items, max(1, config.max_chunks)))


def dominated_lanes(
    lanes: Sequence[Lane],
    *,
    items: int,
    chunk_items: int | None,
    overhead_s: float,
    config: SplitConfig,
) -> tuple[Pruned, ...]:
    """The lanes that are no set extender (module docstring), in rate order."""
    ordered = sorted(lanes, key=_by_rate)
    chunks = _most_chunks(items, chunk_items, config)
    kept: list[tuple[Lane, float]] = []  # (lane, the time it needs alone)
    out: list[Pruned] = []
    for lane_b in ordered:
        start_b, per_b = lane_b.start_s, lane_b.per_item_s
        by: str | None = None
        for lane_a, alone_a in kept:
            start_a, per_a = lane_a.start_s, lane_a.per_item_s
            no_worse = start_a <= start_b and per_a <= per_b
            better = start_a < start_b or per_a < per_b
            if no_worse and better and alone_a <= start_b:
                by = lane_a.node_id
                break
        if by is not None:
            out.append(Pruned(node_id=lane_b.node_id, by=by))
            continue
        alone = start_b + max(1, items) * per_b + chunks * max(0.0, overhead_s)
        kept.append((lane_b, alone))
    return tuple(out)


# --- end V0.14 G3 --------------------------------------------------------------


def _split(
    request: PlanRequest, lanes: Sequence[Lane], snapshot: ClusterSnapshot
) -> CandidateSet:
    split = request.split
    if split is None:  # pragma: no cover - request_kind checked it
        raise ValueError("a split job's candidates need request.split")
    singles = tuple(
        Candidate("local" if item.local else "mode1", (item.node_id,)) for item in lanes
    )
    sets: tuple[Candidate, ...] = ()
    forced: str | None = None
    pruned: tuple[Pruned, ...] = ()
    if request.pinned_node_id is not None:
        forced = _PINNED
    elif request.node_set:
        # V0.10 G8 (D271): every node of the forced set, or nothing (never a
        # different set, never "don't split")
        forced = FORCED_SET
        wanted = set(request.node_set)
        ready = {item.node_id for item in lanes}
        if ready == wanted and len(lanes) >= 2:
            members = sorted(lanes, key=_by_rate)
            sets = (
                Candidate(
                    "mode3",
                    tuple(item.node_id for item in members),
                    {"forced": FORCED_SET},
                ),
            )
        else:
            sets = _refused_set(
                RejectCode.SPLIT_TOO_FEW_NODES,
                f"the forced node set: {len(wanted & ready)} of {len(wanted)} "
                "node(s) can take a lane now",
                lanes,
                len(lanes),
                {"forced": FORCED_SET},
            )
    elif split.items < 2 * split.min_nodes:
        forced = RejectCode.SPLIT_TOO_SMALL.value
        sets = _refused_set(
            RejectCode.SPLIT_TOO_SMALL,
            f"{split.items} item(s) are fewer than 2 x split.min_nodes "
            f"({split.min_nodes}): the job runs unsplit",
            lanes,
            split.min_nodes,
        )
    elif len(lanes) < split.min_nodes:
        forced = RejectCode.SPLIT_TOO_FEW_NODES.value
        sets = _refused_set(
            RejectCode.SPLIT_TOO_FEW_NODES,
            f"only {len(lanes)} node(s) can take a lane now; split.min_nodes is "
            f"{split.min_nodes}",
            lanes,
            len(lanes),
        )
    else:
        extenders = list(lanes)
        if snapshot.config.prune_dominated:  # V0.14 G3 (D294)
            pruned = dominated_lanes(
                lanes,
                items=split.items,
                chunk_items=split.chunk_items,
                overhead_s=chunk_overhead(request, snapshot.config.split).seconds,
                config=snapshot.config.split,
            )
            gone = {p.node_id for p in pruned}
            extenders = [item for item in lanes if item.node_id not in gone]
        sets = tuple(
            Candidate("mode3", tuple(item.node_id for item in group))
            for group in prefix_sets(
                extenders,
                items=split.items,
                min_nodes=split.min_nodes,
                max_nodes=split.max_nodes,
            )
        )
    return CandidateSet(
        RequestKind.SPLIT, (*singles, *sets), forced=forced, pruned=pruned
    )


def candidates(
    request: PlanRequest | AcceleratorRequest,
    snapshot: ClusterSnapshot,
    *,
    lanes: Sequence[Lane] | None = None,
) -> CandidateSet:
    """Every candidate of ``request`` (module docstring). ``lanes`` = a split
    job's feasible nodes as lanes (node-id order): given, the result holds its
    one-lane and Mode 3 candidates; omitted, the node candidates."""
    kind = request_kind(request)
    if isinstance(request, AcceleratorRequest):
        return CandidateSet(
            kind,
            refused=Rejection(
                RejectCode.NOT_SUPPORTED_IN_BUILD,
                "Remote GPU and Combined GPU placement are not part of this build",
            ),
        )
    if lanes is None:
        return CandidateSet(kind, _nodes(request, snapshot))
    if kind is not RequestKind.SPLIT:
        raise ValueError("lanes are only given for a split job")
    return _split(request, lanes, snapshot)
