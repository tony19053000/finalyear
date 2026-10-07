"""Which nodes a retry avoids (V0.6 G3, D110) - pure, like the rest of the planner.

An automatically placed job that failed retryably is tried again **elsewhere**
first, so one sick laptop cannot eat every attempt:

* ``node_lost``, ``transfer_error``, ``integrity_error``, ``timeout``,
  ``disk_full`` (D126) - the node it failed on, for ``retry_exclude_s``
  after that attempt ended;
* ``vram_oom`` - every node whose largest GPU is no larger than the GPU that
  ran out of memory (that node included), for the rest of the job: a retry
  only helps on a bigger GPU;
* anything else (``resource_busy``, ``network_error``, a retryable
  ``output_error``...) - no exclusion.

The planner makes it soft (:func:`~.plan._with_exclusion`), by the **D129
lift rule** (V0.6 G4, replacing G3's all-or-nothing lift):

1. a pinned job never excludes (it retries on its own node only);
2. while some node outside the excluded set could ever run the job (passes
   the static rules), nothing is lifted - the job waits for that node;
3. otherwise exclusions are lifted one tier of :data:`LIFT_ORDER` at a time -
   ``node_lost``/``timeout`` first (transient), then ``transfer_error``/
   ``integrity_error``, ``vram_oom`` last - and only for the tier's nodes that
   would take the job **right now** (online, connected, a free slot; every
   live rule but the exclusion). The first tier with such a node wins;
4. :data:`NEVER_LIFTED` (``disk_full``) is never lifted early: the node's disk
   sample is its largest disk, not its workdir, so nothing says the space is
   back - the window (``retry_exclude_s``) is the signal;
5. no usable node in any tier -> the job stays ``QUEUED`` until a node
   recovers or the earliest exclusion expires. Attempts are not burned on a
   node that would fail the job again for the same reason.

The result maps ``node_id -> failure type`` so ``compute explain`` can say why
a node was skipped. A node that failed the job in more than one way carries
its **strongest** reason - the one lifted last (``disk_full``, then
``vram_oom``, then transfer/integrity, then lost/timeout) - whatever the order
of the attempts: a GPU that was lost once and later ran out of VRAM is a
``vram_oom`` node, lifted in the last tier (tester r1 T-6).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from meshforge.controller.planner.types import NodeSnapshot

__all__ = [
    "EXCLUDE_NODE_FOR",
    "LIFT_ORDER",
    "NEVER_LIFTED",
    "PastFailure",
    "exclusions",
]

EXCLUDE_NODE_FOR: Final = frozenset(
    {"node_lost", "transfer_error", "integrity_error", "timeout", "disk_full"}
)
"""Failure types (values) that exclude the node they happened on (D110)."""
_VRAM_OOM: Final = "vram_oom"

LIFT_ORDER: Final[tuple[frozenset[str], ...]] = (
    frozenset({"node_lost", "timeout"}),
    frozenset({"transfer_error", "integrity_error"}),
    frozenset({_VRAM_OOM}),
)
"""D129: the order exclusions are lifted in when every capable node is
excluded (module docstring, rule 3)."""
NEVER_LIFTED: Final = frozenset({"disk_full"})
"""D129: excluded until the window ends, never lifted early (rule 4)."""


def _strength(failure_type: str) -> int:
    """How late ``failure_type`` is lifted: its tier in :data:`LIFT_ORDER`,
    above them all for :data:`NEVER_LIFTED`."""
    for tier, types in enumerate(LIFT_ORDER):
        if failure_type in types:
            return tier
    return len(LIFT_ORDER)


def _label(out: dict[str, str], node_id: str, failure_type: str) -> None:
    """Record ``failure_type`` for ``node_id`` unless a stronger one is known."""
    known = out.get(node_id)
    if known is None or _strength(failure_type) > _strength(known):
        out[node_id] = failure_type


@dataclass(frozen=True, slots=True)
class PastFailure:
    """One ended attempt of the job (``failed`` or ``lost``)."""

    node_id: str
    failure_type: str
    ended_at: dt.datetime | None
    gpu_indices: tuple[int, ...] = ()
    """The GPUs the attempt was given (its start snapshot)."""


def _largest_vram(node: NodeSnapshot) -> int:
    return max(node.gpu_vram_bytes, default=0)


def _vram_used(failure: PastFailure, node: NodeSnapshot | None) -> int:
    """VRAM of the GPU(s) the failed attempt ran on (0 = unknown)."""
    if node is None or not node.gpu_vram_bytes:
        return 0
    indices = node.gpu_indices or tuple(range(len(node.gpu_vram_bytes)))
    used = [
        vram
        for index, vram in zip(indices, node.gpu_vram_bytes, strict=False)
        if index in failure.gpu_indices
    ]
    return max(used) if used else _largest_vram(node)


def exclusions(
    past: Iterable[PastFailure],
    nodes: Sequence[NodeSnapshot],
    *,
    now: dt.datetime,
    exclude_s: float,
) -> dict[str, str]:
    """``node_id -> failure type`` for this retry (module docstring)."""
    by_id = {n.node_id: n for n in nodes}
    out: dict[str, str] = {}
    vram_floor: int | None = None
    window = dt.timedelta(seconds=max(0.0, exclude_s))
    for failure in past:
        if failure.failure_type in EXCLUDE_NODE_FOR:
            if failure.ended_at is None or failure.ended_at + window > now:
                _label(out, failure.node_id, failure.failure_type)
        elif failure.failure_type == _VRAM_OOM:
            _label(out, failure.node_id, _VRAM_OOM)
            used = _vram_used(failure, by_id.get(failure.node_id))
            if used > 0:
                vram_floor = max(vram_floor or 0, used)
    if vram_floor is not None:
        for node in nodes:
            largest = _largest_vram(node)
            if node.gpu_vram_bytes and 0 < largest <= vram_floor:
                _label(out, node.node_id, _VRAM_OOM)
    return dict(sorted(out.items()))
