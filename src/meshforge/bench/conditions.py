"""Benchmark conditions for campaigns (V0.9 G4, roadmap 0.9.5, D217/D241). Pure.

The grammar and the typed models are :mod:`meshforge.common.bench_conditions`
(shared with the agent, which applies most conditions); this module adds what
a *campaign* needs: whether one configuration (node, strategy) can run a
condition at all (:func:`unsupported_reason`) and how much a condition adds to
a run's expected time (:func:`extra_seconds`, for the budget's hour estimate).

**Support rules** (create-time; the agent re-checks at run time):

* an agent condition (``cpu_load``, ``gpu_load``, ``netem``, ``cache``) needs
  the run's node to advertise the kind in its ``bench-conditions:`` tag;
  ``cpu_load:N`` also needs N <= the node's thread count;
* ``unavailable`` needs the ``mode1`` strategy (it pauses the origin; a local
  run on a paused node would never start);
* ``none`` and ``queue`` run anywhere.

A node whose capabilities are not known (``NodeRef.conditions is None``: an
offline ``compute bench plan --node``) is not checked - the controller checks
again with the real tags when the campaign is created.
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final

from meshforge.common.bench_conditions import (
    AGENT_KINDS,
    NONE,
    CacheCondition,
    Condition,
    ConditionError,
    ConditionKind,
    CpuLoad,
    GpuLoad,
    Netem,
    NoCondition,
    QueueCondition,
    Unavailable,
    advertised_kinds,
    capability_tag,
    parse_condition,
)

__all__ = [
    "AGENT_KINDS",
    "NONE",
    "WARMUP_OVERHEAD_RUNS",
    "CacheCondition",
    "Condition",
    "ConditionError",
    "ConditionKind",
    "CpuLoad",
    "GpuLoad",
    "Netem",
    "NoCondition",
    "QueueCondition",
    "Unavailable",
    "advertised_kinds",
    "capability_tag",
    "extra_seconds",
    "parse_condition",
    "unsupported_reason",
]

WARMUP_OVERHEAD_RUNS: Final = 1
"""``cache:warm`` runs the job once, unrecorded, before the measured run."""


def unsupported_reason(
    condition: Condition,
    *,
    strategy: str,
    node_kinds: Collection[ConditionKind] | None,
    node_threads: int | None,
) -> str | None:
    """Why a run of ``condition`` with ``strategy`` cannot happen on a node
    advertising ``node_kinds`` (None = unknown: not checked); None = it can."""
    kind = condition.kind
    if kind is ConditionKind.UNAVAILABLE and strategy != "mode1":
        return "unavailable needs the mode1 strategy (it pauses the origin)"
    if kind not in AGENT_KINDS or node_kinds is None:
        return None
    if kind not in node_kinds:
        return f"the node does not offer {kind.value} (bench-conditions capability)"
    if (
        isinstance(condition, CpuLoad)
        and node_threads is not None
        and condition.cores > node_threads
    ):
        return f"cpu_load:{condition.cores} needs more than the node's {node_threads} threads"
    return None


def extra_seconds(condition: Condition, expected_s: float) -> float:
    """Seconds a condition adds to one run of a job expected to take
    ``expected_s`` (fillers / warm-up run first, the pause)."""
    if isinstance(condition, QueueCondition):
        return condition.jobs * expected_s
    if isinstance(condition, CacheCondition) and condition.mode == "warm":
        return WARMUP_OVERHEAD_RUNS * expected_s
    if isinstance(condition, Unavailable):
        return float(condition.seconds)
    return 0.0
