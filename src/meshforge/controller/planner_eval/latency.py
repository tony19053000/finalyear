"""Decision latency over real decisions: the report's ``latency`` block and
criterion 3's verdict (V0.14 G6, D324).

The dispatcher stores each decision's wall time in ``plan_decision.
decision_ms`` (migration 0033). The loader hands the evaluated campaigns'
``dispatched`` decisions here as :class:`LatencySample` s - the time and the
number of nodes the decision's snapshot held.

**Block** (``latency`` in the report)::

    {decisions, without_latency, p50_ms, p99_ms, max_ms, over_budget,
     budget_ms, node_count, min_decisions,
     by_node_count: {"<n>": {decisions, p99_ms}}}

* ``decisions`` - samples with a time; ``without_latency`` - samples without
  one (a row written before 0033, or a non-finite / negative value): never
  counted as 0, only counted;
* percentiles are **nearest-rank** and exact: the ``p``-th percentile of
  ``n`` sorted values is the value at rank ``ceil(p * n / 100)`` (integer
  arithmetic - no interpolation, no float rank);
* ``over_budget`` - decisions slower than ``budget_ms`` (``>``, the same
  rule as the dispatcher's counter);
* ``by_node_count`` - the same per snapshot size (``unknown`` = the size
  could not be read).

**Criterion 3** (``criterion`` 3 in ``verdicts[]``): on the decisions whose
snapshot had exactly ``node_count`` nodes (4: the lab fabric), ``PASS`` iff
their p99 is **strictly below** ``budget_ms``, else ``FAIL``; fewer than
``min_decisions`` such decisions -> ``too_few_decisions``, never ``PASS``.
A leaked report stamps it ``leaked`` like every verdict (D299).

Pure (no I/O): the planner_eval AST test pins the imports.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "LATENCY_CRITERION",
    "UNKNOWN_NODES",
    "LatencyConfig",
    "LatencySample",
    "latency_block",
    "latency_verdict",
    "nearest_rank",
]

LATENCY_CRITERION: Final = 3
UNKNOWN_NODES: Final = "unknown"
"""The ``by_node_count`` key of decisions whose snapshot size is unknown."""
_ROUND: Final = 6


@dataclass(frozen=True, slots=True)
class LatencySample:
    """One ``dispatched`` decision: its wall time (None = not stored) and its
    snapshot's node count (None = unknown)."""

    decision_ms: float | None
    node_count: int | None


@dataclass(frozen=True, slots=True)
class LatencyConfig:
    budget_ms: float = 100.0
    """``[controller.planner] decision_latency_budget_ms`` (criterion 3)."""
    node_count: int = 4
    """The fabric size criterion 3 is judged at (the 4 lab laptops)."""
    min_decisions: int = 30
    """Fewer decisions at ``node_count`` -> ``too_few_decisions``."""


def _r(value: float | None) -> float | None:
    return None if value is None else round(value, _ROUND)


def nearest_rank(ordered: Sequence[float], pct: int) -> float | None:
    """The ``pct``-th percentile (1..100) of ascending ``ordered`` by the
    nearest-rank method; None for no value."""
    if not 1 <= pct <= 100:
        raise ValueError("pct must be within 1..100")
    n = len(ordered)
    if n == 0:
        return None
    rank = -(-pct * n // 100)  # ceil(pct * n / 100), exact
    return ordered[rank - 1]


def _usable(value: float | None) -> float | None:
    """``value`` when it is a real time (finite, not negative), else None."""
    if value is None or not math.isfinite(value) or value < 0.0:
        return None
    return float(value)


def _bucket_key(count: int | None) -> str:
    return UNKNOWN_NODES if count is None or count < 0 else str(count)


def _bucket_sort(key: str) -> tuple[int, int]:
    return (1, 0) if key == UNKNOWN_NODES else (0, int(key))


def latency_block(
    samples: Sequence[LatencySample], config: LatencyConfig
) -> dict[str, Any]:
    """The report's ``latency`` block (module docstring)."""
    usable = [
        (ms, sample.node_count)
        for sample in samples
        if (ms := _usable(sample.decision_ms)) is not None
    ]
    timed = sorted(ms for ms, _ in usable)
    buckets: dict[str, list[float]] = {}
    for ms, count in usable:
        buckets.setdefault(_bucket_key(count), []).append(ms)
    by_node_count: dict[str, dict[str, Any]] = {}
    for key in sorted(buckets, key=_bucket_sort):
        values = sorted(buckets[key])
        by_node_count[key] = {
            "decisions": len(values),
            "p99_ms": _r(nearest_rank(values, 99)),
        }
    return {
        "decisions": len(timed),
        "without_latency": len(samples) - len(timed),
        "p50_ms": _r(nearest_rank(timed, 50)),
        "p99_ms": _r(nearest_rank(timed, 99)),
        "max_ms": _r(timed[-1]) if timed else None,
        "over_budget": sum(1 for v in timed if v > config.budget_ms),
        "budget_ms": config.budget_ms,
        "node_count": config.node_count,
        "min_decisions": config.min_decisions,
        "by_node_count": by_node_count,
    }


def latency_verdict(block: Mapping[str, Any], *, leaked: bool) -> dict[str, Any]:
    """Criterion 3's verdict from a :func:`latency_block` (module docstring),
    in the shape of the report's other verdicts (``counted_groups`` holds the
    decisions judged)."""
    nodes = int(block["node_count"])
    budget = float(block["budget_ms"])
    least = int(block["min_decisions"])
    bucket = (block.get("by_node_count") or {}).get(str(nodes)) or {}
    n = int(bucket.get("decisions") or 0)
    p99 = bucket.get("p99_ms")
    if n < least or p99 is None:
        status = "too_few_decisions"
        detail = (
            f"{n} dispatched decision(s) with a {nodes}-node snapshot; at least "
            f"{least} needed"
        )
    else:
        status = "PASS" if float(p99) < budget else "FAIL"
        detail = (
            f"p99 {float(p99):.3f} ms over {n} decision(s) with a {nodes}-node "
            f"snapshot (budget < {budget:g} ms)"
        )
    return {
        "criterion": LATENCY_CRITERION,
        "status": "leaked" if leaked else status,
        "would_be": status if leaked else None,
        "value": _r(None if p99 is None else float(p99)),
        "threshold": _r(budget),
        "counted_groups": n,
        "detail": detail,
        "comparisons": [],
    }
