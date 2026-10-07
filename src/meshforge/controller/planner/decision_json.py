"""A :class:`~.types.Decision` as stored in ``plan_decision.candidates`` (D112).

One JSON object per candidate, best first, at most
:data:`MAX_DECISION_CANDIDATES` (the chosen one and every feasible one come
before the rejected ones, so truncation drops rejections first)::

    {"node_id", "mode": "local"|"offload", "feasible", "slot",
     "reject_code", "reject_text",
     "estimate": {"total_s", "terms": {name: {"seconds", "source", "detail"}}},
     "score", "rank"}

``compute explain`` prints exactly these fields; nothing is recomputed later.

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

from meshforge.controller.planner.types import CandidateEval, Decision, Estimate

__all__ = [
    "MAX_DECISION_CANDIDATES",
    "candidates_json",
    "estimate_json",
    "json_safe",
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
    }


def candidates_json(decision: Decision) -> list[dict[str, Any]]:
    """The bounded JSON list (see module docstring)."""
    return [
        json_safe(_candidate(c)) for c in decision.candidates[:MAX_DECISION_CANDIDATES]
    ]
