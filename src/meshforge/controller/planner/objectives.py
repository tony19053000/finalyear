"""Objectives: what the ``cost_model`` policy ranks by (V0.14 G3, D295;
roadmap 0.14.5). Pure. An objective is a **ranking rule over the same
estimates** - never another estimator.

* ``min_time`` (the default; also a request that names none) - the lowest
  predicted (risk-adjusted) completion time: exactly the ``cost_model`` of
  before G3.
* ``min_network`` - the fewest predicted **bytes moved** (:func:`bytes_moved`:
  the trees the node does not hold, the split items it would fetch, the
  outputs it uploads and, for a Mode 3 set, the chunk results moved to the
  combine node); the predicted time breaks ties, then the node id. A Mode 3 set
  must still pass its margin test against the best single to be chosen.
* ``prefer_local`` - the submitter's own node (Local) unless the ``min_time``
  pick is predicted at most ``(1 - prefer_local_margin)`` x Local's time
  (``[controller.planner] prefer_local_margin``, 0.25). No feasible Local
  candidate: ``min_time``.

Rule policies (``random``, ``round_robin`` ...) ignore the objective: they
rank by their own rule, and the decision still records which objective the
job asked for. A decision whose pick the objective changed (it differs from
the ``min_time`` pick) says so in its ``reason_key`` (:func:`reason_suffix`).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Final

from meshforge.common.manifest import OBJECTIVES
from meshforge.controller.planner.policies import Ranked, Scored
from meshforge.controller.planner.split import MIN_SECONDS_PER_ITEM, SetEstimate
from meshforge.controller.planner.types import Estimate, Objective, PlanRequest

__all__ = [
    "COST_MODEL",
    "DEFAULT_OBJECTIVE",
    "OBJECTIVES",
    "bytes_moved",
    "objective_of",
    "prefer_local_wins",
    "rank_singles",
    "reason_suffix",
    "set_bytes_moved",
]

DEFAULT_OBJECTIVE: Final[Objective] = "min_time"
COST_MODEL: Final = "cost_model"
"""The one policy that reads the objective."""
_ROUND: Final = 6


def objective_of(objective: str | None) -> Objective:
    """The objective a request ranks by (None = ``min_time``);
    :class:`ValueError` for a name that is not one of :data:`OBJECTIVES`."""
    if objective is None:
        return DEFAULT_OBJECTIVE
    if objective not in OBJECTIVES:
        raise ValueError(f"unknown planner objective {objective!r}")
    return objective  # type: ignore[return-value]  # checked against OBJECTIVES


def _number(detail: Mapping[str, Any], key: str) -> float:
    value = detail.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) and number > 0.0 else 0.0


def bytes_moved(estimate: Estimate, *, staged_bytes: float | None = None) -> float:
    """Predicted bytes a candidate moves over the network, read from its own
    estimate (the numbers ``compute explain`` shows): the ``transfer`` term's
    missing bytes (or ``staged_bytes`` when the caller summed them over a
    set's lanes) + ``output`` bytes + a Mode 3 set's ``combine`` parts."""
    combine = estimate.extra.get("combine")
    staged = (
        _number(estimate.transfer.detail, "missing_bytes")
        if staged_bytes is None
        else max(0.0, staged_bytes)
    )
    return (
        staged
        + _number(estimate.output.detail, "out_bytes")
        + (0.0 if combine is None else _number(combine.detail, "parts_bytes"))
    )


def set_bytes_moved(item: SetEstimate, request: PlanRequest) -> float:
    """:func:`bytes_moved` of a split job's candidate (one lane or a Mode 3
    set): every active lane's staged bytes, plus the items' bytes in the share
    of the items the lanes that do not hold them would run (rate-weighted,
    like the water filling), plus output and combine parts."""
    active = [lane for lane in item.lanes if lane.node_id in item.active]
    staged = sum(_number(lane.stage.detail, "missing_bytes") for lane in active)
    split = request.split
    item_bytes = 0.0 if split is None else float(max(0, split.item_bytes or 0))
    weights = {
        lane.node_id: 1.0 / max(lane.per_item_s, MIN_SECONDS_PER_ITEM)
        for lane in active
    }
    total_weight = sum(weights.values())
    fetched = 0.0
    if item_bytes > 0.0 and total_weight > 0.0:
        for lane in active:
            per_item = _number(lane.fetch.detail, "bytes_per_item")
            if per_item > 0.0 and not lane.fetch.detail.get("held"):
                fetched += item_bytes * weights[lane.node_id] / total_weight
    return bytes_moved(item.estimate, staged_bytes=staged + fetched)


def prefer_local_wins(local_s: float, best_s: float, margin: float) -> bool:
    """``prefer_local``: Local keeps the job unless the best other candidate
    is predicted at most ``(1 - margin) x`` Local's time."""
    margin = min(max(margin, 0.0), 0.95)
    return not best_s <= (1.0 - margin) * local_s


def rank_singles(
    objective: Objective,
    items: Sequence[Scored],
    *,
    prefer_local_margin: float,
    bytes_of: Callable[[Scored], float] | None = None,
) -> tuple[list[Ranked], bool]:
    """``cost_model`` under ``objective`` for Local / Mode 1 candidates:
    ``(ranking best first, changed)`` - ``changed`` = rank 1 is not the
    ``min_time`` pick. ``min_time`` is the pre-G3 ``cost_model`` exactly
    (key: total, then node id; score: the total); ``min_network`` scores by
    the bytes (``bytes_of``, default :func:`bytes_moved` of the estimate)."""
    weigh = bytes_of or (lambda item: bytes_moved(item.estimate))
    by_time = sorted(items, key=lambda i: (i.estimate.total_s, i.node.node_id))
    if objective == "min_network":
        moved = {id(i): weigh(i) for i in items}
        order = sorted(
            items,
            key=lambda i: (moved[id(i)], i.estimate.total_s, i.node.node_id),
        )
        scores = [float(round(moved[id(i)])) for i in order]
    else:
        order = list(by_time)
        if objective == "prefer_local" and order and not order[0].local:
            local = next((i for i in order if i.local), None)
            if local is not None and prefer_local_wins(
                local.estimate.total_s, order[0].estimate.total_s, prefer_local_margin
            ):
                order = [local, *(i for i in order if i is not local)]
        scores = [round(i.estimate.total_s, _ROUND) for i in order]
    ranked = [
        Ranked(node_id=item.node.node_id, score=score, rank=index + 1)
        for index, (item, score) in enumerate(zip(order, scores, strict=True))
    ]
    changed = bool(order) and order[0].node.node_id != by_time[0].node.node_id
    return ranked, changed


def reason_suffix(objective: Objective) -> str:
    """What a decision's reason key adds when its objective changed the pick."""
    return f"objective:{objective}"
