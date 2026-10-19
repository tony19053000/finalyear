"""Where a split job's lanes go and how big its chunks are - **the G3 seam**
(V0.10 G4 placeholder; G3's planner Mode 3 replaces :func:`plan_lanes`).

V0.10 G4 builds the lanes end to end while G3 builds the planner's Mode 3
(D265: Local / Mode 1 per node / Mode 3 prefix sets, rate estimates, chunk
size, the "don't split" margin) in another worktree. Until they merge, this
one small pure function answers the two questions the dispatcher asks, from
the planner's ordinary (Mode 1) :class:`~meshforge.controller.planner.Decision`
of the job:

* **node set** - every candidate that is feasible *now* (it has a free slot),
  that advertises ``exec-split`` (an agent that cannot run a lane never gets
  one), in the planner's rank order, distinct nodes (one lane per node per job,
  D267f), at most ``split.max_nodes``. Fewer than ``split.min_nodes`` -> "don't
  split" (D269): one lane on the best node;
* **chunk size** - ``split.chunk.items`` when given; ``auto`` = about four
  chunks per lane (``ceil(items / (4 * lanes))``; D265's tail-balance rule
  without the rate terms), one chunk of every item for "don't split"; never more
  than ``max_chunks`` chunks.

**At the G3 merge:** replace the body of :func:`plan_lanes` with G3's plan
object (its node set, chunk size and decision JSON); keep the signature's
output (:class:`LanePlan`) - the dispatcher (``jobs/planning.py``) and the
store use nothing else. A chunk size is fixed once, when the job's work queue is
created: a re-plan of the remaining chunks (D267e) only picks new nodes.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

from meshforge.common.capabilities import EXEC_SPLIT_CAPABILITY
from meshforge.common.manifest import SplitSpec
from meshforge.controller.planner import CandidateEval, ClusterSnapshot, Decision

__all__ = ["AUTO_CHUNKS_PER_LANE", "LanePlan", "plan_lanes"]

AUTO_CHUNKS_PER_LANE: Final = 4
"""D265: at least four chunks per lane, so a slow lane is not the tail alone."""


@dataclass(frozen=True, slots=True)
class LanePlan:
    """What the dispatcher creates for a split job."""

    lanes: tuple[CandidateEval, ...]
    """One feasible candidate per lane (its node and slot), best first."""
    chunk_items: int
    """Items per chunk (the last chunk may hold fewer)."""
    split: bool
    """False = "don't split": one lane (D269)."""
    reason: str
    """Why this set (shown in logs; G3 puts its margin test here)."""


def plan_lanes(
    decision: Decision,
    snapshot: ClusterSnapshot,
    *,
    spec: SplitSpec,
    item_count: int,
    max_chunks: int,
) -> LanePlan | None:
    """The lanes of one split job now (see module docstring); ``None`` = no
    node can take a lane now (the caller waits or fails like a Mode 1 job)."""
    capable = {
        n.node_id for n in snapshot.nodes if EXEC_SPLIT_CAPABILITY in n.capabilities
    }
    ranked = sorted(
        (
            c
            for c in decision.candidates
            if c.feasible
            and c.rank is not None
            and c.slot is not None
            and c.node_id in capable
        ),
        key=lambda c: (c.rank or 0, c.node_id),
    )
    seen: set[str] = set()
    usable: list[CandidateEval] = []
    for cand in ranked:
        if cand.node_id not in seen:
            seen.add(cand.node_id)
            usable.append(cand)
    if not usable:
        return None
    limit = spec.max_nodes if spec.max_nodes is not None else len(usable)
    chosen = usable[: max(1, limit)]
    split = len(chosen) >= spec.min_nodes and item_count >= 2
    if not split:
        chosen = chosen[:1]
    floor = math.ceil(item_count / max_chunks)
    if spec.chunk.items is not None:
        chunk_items = spec.chunk.items
    elif not split:
        chunk_items = item_count  # D269: "don't split" = one chunk
    else:
        chunk_items = math.ceil(item_count / (AUTO_CHUNKS_PER_LANE * len(chosen)))
    chunk_items = max(1, floor, min(chunk_items, item_count))
    reason = (
        f"placeholder: {len(chosen)} lane(s) of {len(usable)} feasible split-capable "
        f"node(s); min_nodes={spec.min_nodes}"
    )
    return LanePlan(
        lanes=tuple(chosen), chunk_items=chunk_items, split=split, reason=reason
    )
