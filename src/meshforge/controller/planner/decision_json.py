"""A :class:`~.types.Decision` as stored in ``plan_decision.candidates`` (D112).

One JSON object per candidate, best first, at most
:data:`MAX_DECISION_CANDIDATES` (the chosen one and every feasible one come
before the rejected ones, so truncation drops rejections first)::

    {"node_id", "mode": "local"|"offload", "feasible", "slot",
     "reject_code", "reject_text",
     "estimate": {"total_s", "terms": {name: {"seconds", "source", "detail"}}},
     "score", "rank"}

``compute explain`` prints exactly these fields; nothing is recomputed later.
"""

from __future__ import annotations

from typing import Any, Final

from meshforge.controller.planner.types import CandidateEval, Decision, Estimate

__all__ = ["MAX_DECISION_CANDIDATES", "candidates_json", "estimate_json"]

MAX_DECISION_CANDIDATES: Final = 64


def estimate_json(estimate: Estimate) -> dict[str, Any]:
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
        "estimate": None if item.estimate is None else estimate_json(item.estimate),
        "score": item.score,
        "rank": item.rank,
    }


def candidates_json(decision: Decision) -> list[dict[str, Any]]:
    """The bounded JSON list (see module docstring)."""
    return [_candidate(c) for c in decision.candidates[:MAX_DECISION_CANDIDATES]]
