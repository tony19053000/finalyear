"""A :class:`~.types.Decision` as stored in ``plan_decision.candidates`` (D112).

One JSON object per candidate, best first, at most
:data:`MAX_DECISION_CANDIDATES` (the chosen one and every feasible one come
before the rejected ones, so truncation drops rejections first)::

    {"node_id", "mode": "local"|"offload", "feasible", "slot",
     "reject_code", "reject_text",
     "estimate": {"total_s", "terms": {name: {"seconds", "source", "detail"}}},
     "score", "rank"}

``compute explain`` prints exactly these fields; nothing is recomputed later.

**Split jobs** (V0.10 G3): ``mode`` may be ``"split"`` (a Mode 3 node set led
by ``node_id``), the estimate has the extra terms ``items_fetch``,
``chunk_overhead``, ``tail``, ``combine``, and every row of the job adds::

    {"node_set": [...], "chunk_items", "chunks",
     # Local / Mode 1 rows (one lane):
     "rate_items_s", "rate_source", "start_s",
     # split rows:
     "inactive": [...], "margin_test": {"set_s", "best_single_s",
     "best_single_node_id", "margin", "threshold_s", "passed", "forced"}}

A rejected ``split`` row (``split_too_small`` / ``split_too_few_nodes``) has
``node_set`` and no estimate. Rows of a job that runs whole are unchanged.

**Never a non-finite number (D131).** PostgreSQL's ``jsonb`` refuses
``Infinity``/``NaN``, and one refused row would undo the whole dispatch
tick's transaction. :func:`json_safe` therefore replaces any non-finite float
by ``None`` (shown as ``-``) everywhere in the list - the estimator already
keeps its numbers finite, this is the boundary that guarantees it.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any, Final

from meshforge.controller.planner.types import (
    CandidateEval,
    Decision,
    Estimate,
    MarginTest,
    SplitPlan,
)

__all__ = [
    "MAX_DECISION_CANDIDATES",
    "candidates_json",
    "estimate_json",
    "json_safe",
    "split_json",
]

MAX_DECISION_CANDIDATES: Final = 64


def json_safe(value: Any) -> Any:
    """``value`` with every non-finite float replaced by ``None`` (mappings
    and sequences walked; keys become strings as JSON would make them)."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [json_safe(v) for v in value]
    return value


def estimate_json(estimate: Estimate) -> dict[str, Any]:
    safe: dict[str, Any] = json_safe(_estimate(estimate))
    return safe


def _estimate(estimate: Estimate) -> dict[str, Any]:
    return {
        "total_s": round(estimate.total_s, 6),
        "terms": {
            name: {
                "seconds": round(term.seconds, 6),
                "source": term.source.value,
                "detail": dict(term.detail),
            }
            for name, term in estimate.terms().items()
        },
    }


def _margin(test: MarginTest) -> dict[str, Any]:
    return {
        "set_s": test.set_s,
        "best_single_s": test.best_single_s,
        "best_single_node_id": test.best_single_node_id,
        "margin": test.margin,
        "threshold_s": test.threshold_s,
        "passed": test.passed,
        "forced": test.forced,
    }


def _split_fields(item: CandidateEval) -> dict[str, Any]:
    """V0.10 G3: what a split job's row adds (nothing for a job that runs
    whole, so those rows are byte-identical to before)."""
    if not item.node_set:
        return {}
    out: dict[str, Any] = {
        "node_set": list(item.node_set),
        "chunk_items": item.chunk_items,
        "chunks": item.chunks,
    }
    if item.mode == "split":
        out["inactive"] = [lane.node_id for lane in item.lanes if not lane.active]
        if item.margin is not None:
            out["margin_test"] = _margin(item.margin)
    elif item.lanes:  # a single: its node's rate (the set rows reuse it)
        lane = item.lanes[0]
        out["rate_items_s"] = lane.rate_items_s
        out["rate_source"] = lane.rate_source.value
        out["start_s"] = lane.start_s
    return out


def split_json(plan: SplitPlan) -> dict[str, Any]:
    """:class:`~.types.SplitPlan` as JSON (for the dispatcher's records)."""
    safe: dict[str, Any] = json_safe(
        {
            "split": plan.split,
            "node_set": list(plan.node_ids),
            "lanes": [
                {
                    "node_id": lane.node_id,
                    "slot": lane.slot,
                    "local": lane.local,
                    "rate_items_s": lane.rate_items_s,
                    "rate_source": lane.rate_source.value,
                    "start_s": lane.start_s,
                }
                for lane in plan.lanes
            ],
            "chunk_items": plan.chunk_items,
            "chunks": plan.chunks,
            "predicted_makespan_s": plan.predicted_makespan_s,
            "margin_test": _margin(plan.margin),
        }
    )
    return safe


def _candidate(item: CandidateEval) -> dict[str, Any]:
    return {
        "node_id": item.node_id,
        "mode": item.mode,
        "feasible": item.feasible,
        "slot": item.slot,
        "reject_code": None if item.rejection is None else item.rejection.code.value,
        "reject_text": None if item.rejection is None else item.rejection.text,
        "estimate": None if item.estimate is None else _estimate(item.estimate),
        "score": item.score,
        "rank": item.rank,
        **_split_fields(item),
    }


def candidates_json(decision: Decision) -> list[dict[str, Any]]:
    """The bounded JSON list (see module docstring)."""
    return [
        json_safe(_candidate(c)) for c in decision.candidates[:MAX_DECISION_CANDIDATES]
    ]
