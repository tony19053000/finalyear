"""Which nodes a retry avoids (V0.6 G3, D110) - pure, like the rest of the planner.

An automatically placed job that failed retryably is tried again **elsewhere**
first, so one sick laptop cannot eat every attempt:

* ``node_lost``, ``transfer_error``, ``integrity_error``, ``timeout`` - the
  node it failed on, for ``retry_exclude_s`` after that attempt ended;
* ``vram_oom`` - every node whose largest GPU is no larger than the GPU that
  ran out of memory (that node included), for the rest of the job: a retry
  only helps on a bigger GPU;
* anything else (``resource_busy``, ``network_error``, a retryable
  ``output_error``...) - no exclusion.

The planner makes it soft (:func:`~.plan._with_exclusion`): a pinned job never
excludes, and when no other node could ever run the job the exclusion is
lifted. The result maps ``node_id -> failure type`` so ``compute explain``
can say why a node was skipped.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

from meshforge.controller.planner.types import NodeSnapshot

__all__ = ["EXCLUDE_NODE_FOR", "PastFailure", "exclusions"]

EXCLUDE_NODE_FOR: Final = frozenset(
    {"node_lost", "transfer_error", "integrity_error", "timeout"}
)
"""Failure types (values) that exclude the node they happened on (D110)."""
_VRAM_OOM: Final = "vram_oom"


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
                out.setdefault(failure.node_id, failure.failure_type)
        elif failure.failure_type == _VRAM_OOM:
            out.setdefault(failure.node_id, _VRAM_OOM)
            used = _vram_used(failure, by_id.get(failure.node_id))
            if used > 0:
                vram_floor = max(vram_floor or 0, used)
    if vram_floor is not None:
        for node in nodes:
            largest = _largest_vram(node)
            if node.gpu_vram_bytes and 0 < largest <= vram_floor:
                out.setdefault(node.node_id, _VRAM_OOM)
    return dict(sorted(out.items()))
