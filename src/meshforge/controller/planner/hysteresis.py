"""Mode hysteresis: near-tie decisions do not flap between modes (V1.0 G12, D460).

Lab v0.17.3 ran the same workload again and again and the planner alternated
Local and Mode 3: the two predictions were within a few percent, so the
noise of each new measurement flipped the pick. Every flip pays a cold start
(a lane's first chunk, an environment on another laptop) the estimates do
not see, and makes the system look erratic.

**Rule.** :attr:`~.types.PlanRequest.previous_mode` is the mode the latest
dispatched decision of the same fingerprint chose (``local`` / ``offload`` /
``split``). When the new pick has another mode ``N`` and the previous mode
``M`` still has a feasible, eligible candidate, the decision keeps ``M``
(its best candidate by predicted total) unless::

    T(best N) <= (1 - mode_switch_margin) x T(best M now)

i.e. the new best must be at least ``mode_switch_margin`` faster. Within one
mode nothing changes (Mode 1 may still move between nodes).

**Never overrides** (:func:`active`): a pinned job (``--node`` / ``--local``),
a campaign's forced node set, a job with ``--exclude-node``, a rule policy
or a ``cost_model`` objective other than ``min_time`` (they do not rank by
predicted time), a decision planned with an ML model, refusals (only
feasible candidates are kept) or a Mode 3 set blocked by the measured-rates
rule. ``mode_switch_margin`` 0 or no previous decision = the planner without
hysteresis. Deterministic: candidates are compared by (total, node ids).

Pure: no database, no clock.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Final

from meshforge.controller.planner.objectives import COST_MODEL, objective_of
from meshforge.controller.planner.types import (
    CandidateEval,
    KeptMode,
    Mode,
    PlanRequest,
)

__all__ = [
    "HYSTERESIS_MODES",
    "MAX_MODE_SWITCH_MARGIN",
    "active",
    "keep_previous",
    "kept_mode",
    "with_kept",
]

HYSTERESIS_MODES: Final[frozenset[str]] = frozenset({"local", "offload", "split"})
MAX_MODE_SWITCH_MARGIN: Final = 0.5
"""The largest margin the planner honours (``[controller.planner]
mode_switch_margin`` is validated to it; a larger replayed value is capped)."""
_ROUND: Final = 6


def active(
    request: PlanRequest, margin: float, policy: str, *, ml: bool
) -> float | None:
    """The margin to apply to this decision, None = no hysteresis here
    (module docstring, "Never overrides")."""
    if margin <= 0.0 or request.previous_mode not in HYSTERESIS_MODES:
        return None
    if request.pinned_node_id is not None or request.node_set or request.exclude_nodes:
        return None
    if policy != COST_MODEL or objective_of(request.objective) != "min_time" or ml:
        return None
    return min(margin, MAX_MODE_SWITCH_MARGIN)


def keep_previous(best_s: float, kept_s: float, margin: float) -> bool:
    """Keep the previous mode: the new best is NOT at least ``margin`` faster
    than the previous mode's best now (rounded like every stored estimate)."""
    return round(best_s, _ROUND) > round((1.0 - margin) * kept_s, _ROUND)


def kept_mode(
    previous: Mode, best_mode: Mode, best_s: float, kept_s: float, margin: float
) -> KeptMode:
    return KeptMode(
        previous_mode=previous,
        best_mode=best_mode,
        best_s=round(best_s, _ROUND),
        kept_s=round(kept_s, _ROUND),
        margin=margin,
    )


def _total(row: CandidateEval) -> float:
    return row.estimate.total_s if row.estimate is not None else float("inf")


def with_kept(
    rows: list[CandidateEval], request: PlanRequest, margin: float
) -> list[CandidateEval]:
    """A whole job's ranked rows (rank 1 first) with hysteresis applied: the
    previous mode's best feasible row moved to rank 1 and marked
    :attr:`~.types.CandidateEval.kept`, the others shifted down in their
    order; ``rows`` unchanged when nothing is kept."""
    ranked = [r for r in rows if r.rank is not None]
    if not ranked:
        return rows
    chosen = min(ranked, key=lambda r: r.rank or 0)
    previous = request.previous_mode
    if previous is None or chosen.mode == previous:
        return rows
    pool = [r for r in ranked if r.mode == previous and r.feasible]
    if not pool:
        return rows
    keep = min(pool, key=lambda r: (_total(r), r.node_id))
    best_s, kept_s = _total(chosen), _total(keep)
    if not keep_previous(best_s, kept_s, margin):
        return rows
    order = sorted(ranked, key=lambda r: r.rank or 0)
    order.remove(keep)
    new_rank = {id(r): i + 2 for i, r in enumerate(order)}
    new_rank[id(keep)] = 1
    note = kept_mode(previous, chosen.mode, best_s, kept_s, margin)
    out: list[CandidateEval] = []
    for row in rows:
        if row.rank is None:
            out.append(row)
            continue
        kept = note if row is keep else None
        out.append(replace(row, rank=new_rank[id(row)], kept=kept))
    out.sort(key=lambda c: (c.rank is None, c.rank or 0, c.node_id))
    return out
