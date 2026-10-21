"""The risk term: the predicted cost of a node failing the attempt (V0.14 G3,
D296; roadmap 0.14.4). Pure, like the rest of the planner.

**Which attempt ends count** (:data:`END_CLASS`, :func:`classify_end`). Only
what says something about the *node*:

* ``node`` - the node failed the work: the attempt was ``lost``, or failed
  ``node_lost`` / ``gpu_session_lost`` / ``owner_preempted`` (the owner took
  the laptop back) or an infrastructure failure on the node - ``disk_full``,
  ``env_error`` (its environment builder), ``transfer_error`` (the attempt's
  node is the one receiving). Counted in ``failures`` and ``total``.
* ``clean`` - the node did its part: ``done``, or the workload failed on its
  own (``user_error``, ``oom`` / ``vram_oom`` of the job's own limit,
  ``timeout``, ``deadline_exceeded``, ``output_error``, ``combine_error``,
  ``invalid_input``). Counted in ``total`` only - a user's crashing script never
  makes a node look unreliable.
* ``not_counted`` - says nothing either way: ``cancelled``, ``fenced``, a
  start refused ``resource_busy``, ``integrity_error`` (the bad bytes came
  from a holder, not this node), a controller-side error (``internal_error``,
  ``config_error``, ``network_error`` ...).

Every :class:`~meshforge.common.errors.FailureType` member is classified
explicitly: a new failure type fails the unit test until it is placed.

**Probability.** ``p_fail = (a + failures) / (a + b + total)`` with the Beta
prior ``(a, b)`` = ``[controller.planner] risk_prior`` (1, 19: 5 % for a node
with no history). Source ``history`` once the node has a counted end in the
window, ``assumed`` while only the prior speaks.

**What a failure costs** (D296, D267):

* Local / Mode 1 (:func:`single_risk`): the attempt is retried elsewhere, so
  ``risk = p_fail x (T + retry_overhead)`` - ``T`` the candidate's own
  un-risked total, ``retry_overhead`` the queue + transfer + env of the best
  other feasible single (or ``T`` when there is none).
* A split job's candidates (:func:`lane_risk`, one lane or a Mode 3 set):
  a lost lane only re-runs the chunk it had leased, once the lease comes back,
  so ``risk = sum over active lanes p_fail(n) x (chunk_time_n +
  lease_release_s)``.

A node without reliability inputs (``NodeSnapshot.reliability`` None - risk
off, or a snapshot from before G3, D307) adds nothing; a candidate none of
whose nodes has them gets **no** ``risk`` term at all, so its estimate is
exactly the pre-G3 one.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from enum import StrEnum
from typing import Any, Final

from meshforge.common.errors import FailureType
from meshforge.common.states import AttemptState
from meshforge.controller.planner.estimator import bounded, weakest
from meshforge.controller.planner.types import (
    Estimate,
    EstimateTerm,
    NodeSnapshot,
    Reliability,
    RiskConfig,
    TermSource,
)

__all__ = [
    "END_CLASS",
    "RISK_TERM",
    "EndClass",
    "classify_end",
    "failure_types_of",
    "lane_risk",
    "p_fail",
    "retry_overhead_s",
    "risked_singles",
    "single_risk",
    "with_risk",
]

RISK_TERM: Final = "risk"
"""The name of the term in :attr:`~.types.Estimate.extra` (always last)."""
_DEFAULT_PRIOR: Final = (1.0, 19.0)
_ROUND: Final = 6


class EndClass(StrEnum):
    """How an attempt end counts for its node's reliability (module docstring)."""

    NODE = "node"
    CLEAN = "clean"
    NOT_COUNTED = "not_counted"


END_CLASS: Final[Mapping[FailureType, EndClass]] = {
    # --- the node failed the work ------------------------------------------
    FailureType.NODE_LOST: EndClass.NODE,
    FailureType.GPU_SESSION_LOST: EndClass.NODE,
    FailureType.OWNER_PREEMPTED: EndClass.NODE,
    FailureType.DISK_FULL: EndClass.NODE,
    FailureType.ENV_ERROR: EndClass.NODE,
    FailureType.TRANSFER_ERROR: EndClass.NODE,
    # --- the workload failed on its own: the node did its part -------------
    FailureType.USER_ERROR: EndClass.CLEAN,
    FailureType.OOM: EndClass.CLEAN,
    FailureType.VRAM_OOM: EndClass.CLEAN,
    FailureType.TIMEOUT: EndClass.CLEAN,
    FailureType.DEADLINE_EXCEEDED: EndClass.CLEAN,
    FailureType.OUTPUT_ERROR: EndClass.CLEAN,
    FailureType.COMBINE_ERROR: EndClass.CLEAN,
    FailureType.INVALID_INPUT: EndClass.CLEAN,
    # --- says nothing about the node -----------------------------------------
    FailureType.CANCELLED: EndClass.NOT_COUNTED,
    FailureType.FENCED: EndClass.NOT_COUNTED,
    FailureType.RESOURCE_BUSY: EndClass.NOT_COUNTED,
    FailureType.INTEGRITY_ERROR: EndClass.NOT_COUNTED,
    FailureType.NO_FEASIBLE_PLAN: EndClass.NOT_COUNTED,
    FailureType.NETWORK_ERROR: EndClass.NOT_COUNTED,
    FailureType.CONFIG_ERROR: EndClass.NOT_COUNTED,
    FailureType.UNAUTHORIZED: EndClass.NOT_COUNTED,
    FailureType.NOT_FOUND: EndClass.NOT_COUNTED,
    FailureType.CONFLICT: EndClass.NOT_COUNTED,
    FailureType.INTERNAL_ERROR: EndClass.NOT_COUNTED,
}
"""Every failure type's class (module docstring); exhaustive by test."""


def classify_end(state: str, failure_type: str | None) -> EndClass:
    """One ended attempt's class: ``done`` is clean, ``lost`` node-caused,
    ``failed`` by its failure type (unknown or none: not counted), anything
    else (``fenced``, a live state) not counted."""
    if state == AttemptState.DONE.value:
        return EndClass.CLEAN
    if state == AttemptState.LOST.value:
        return EndClass.NODE
    if state != AttemptState.FAILED.value or failure_type is None:
        return EndClass.NOT_COUNTED
    try:
        return END_CLASS[FailureType(failure_type)]
    except (ValueError, KeyError):
        return EndClass.NOT_COUNTED


def failure_types_of(end: EndClass) -> tuple[str, ...]:
    """The failure-type values of class ``end`` (the snapshot builder's SQL
    filter), sorted."""
    return tuple(sorted(t.value for t, c in END_CLASS.items() if c is end))


# ------------------------------------------------------------------ maths


def _prior(config: RiskConfig) -> tuple[float, float]:
    a, b = config.prior
    if not (math.isfinite(a) and math.isfinite(b) and a > 0.0 and b > 0.0):
        return _DEFAULT_PRIOR  # a hand-built config (the settings refuse it)
    return a, b


def p_fail(reliability: Reliability, config: RiskConfig) -> float:
    """``(a + failures) / (a + b + total)``, in ``(0, 1)``; counts outside
    ``0 <= failures <= total`` are read inside it."""
    a, b = _prior(config)
    total = max(0, reliability.total)
    failures = min(max(0, reliability.failures), total)
    return (a + failures) / (a + b + total)


def _source(reliability: Reliability) -> TermSource:
    return TermSource.HISTORY if reliability.total > 0 else TermSource.ASSUMED


def _node_detail(
    node: NodeSnapshot, reliability: Reliability, config: RiskConfig
) -> dict[str, Any]:
    return {
        "node_id": node.node_id,
        "p_fail": round(p_fail(reliability, config), _ROUND),
        "failures": reliability.failures,
        "total": reliability.total,
    }


def with_risk(estimate: Estimate, term: EstimateTerm | None) -> Estimate:
    """``estimate`` with its ``risk`` term (last); unchanged for None."""
    if term is None:
        return estimate
    return replace(estimate, extra={**estimate.extra, RISK_TERM: term})


def retry_overhead_s(estimate: Estimate) -> float:
    """What a retry on this candidate costs before it computes: queue +
    transfer + env."""
    return bounded(
        estimate.queue.seconds + estimate.transfer.seconds + estimate.env.seconds
    )


def single_risk(
    node: NodeSnapshot,
    total_s: float,
    retry_s: float,
    config: RiskConfig,
) -> EstimateTerm | None:
    """Local / Mode 1: ``p_fail x (T + retry_overhead)`` (module docstring);
    None when the node has no reliability inputs."""
    reliability = node.reliability
    if reliability is None:
        return None
    p = p_fail(reliability, config)
    detail = _node_detail(node, reliability, config)
    del detail["node_id"]
    return EstimateTerm(
        bounded(p * (bounded(total_s) + bounded(retry_s))),
        _source(reliability),
        {
            **detail,
            "window_days": reliability.window_days,
            "retry_overhead_s": round(bounded(retry_s), _ROUND),
        },
    )


def lane_risk(
    lanes: Sequence[tuple[NodeSnapshot, float]],
    config: RiskConfig,
) -> EstimateTerm | None:
    """A split job's candidate: ``sum p_fail(n) x (chunk_time_n +
    lease_release_s)`` over its active lanes ``(node, chunk seconds)``; None
    when none of them has reliability inputs."""
    known = [(n, s, n.reliability) for n, s in lanes if n.reliability is not None]
    if not known:
        return None
    release = bounded(config.lease_release_s)
    seconds = 0.0
    rows: list[dict[str, Any]] = []
    sources: list[TermSource] = []
    window: float | None = None
    for node, chunk_s, reliability in known:
        assert reliability is not None  # noqa: S101 - filtered above
        seconds += p_fail(reliability, config) * (bounded(chunk_s) + release)
        sources.append(_source(reliability))
        rows.append(
            {
                **_node_detail(node, reliability, config),
                "chunk_s": round(bounded(chunk_s), _ROUND),
            }
        )
        window = reliability.window_days
    return EstimateTerm(
        bounded(seconds),
        weakest(*sources),
        {
            "lanes": rows,
            "lease_release_s": round(release, _ROUND),
            "window_days": window,
        },
    )


def risked_singles(
    singles: Sequence[tuple[NodeSnapshot, Estimate]], config: RiskConfig
) -> list[Estimate]:
    """Each Local / Mode 1 estimate with its risk term (same order). The
    retry overhead of one is that of the best **other** single by un-risked
    total (ties on node id); with no other, its own total ``T``."""
    order = sorted(
        range(len(singles)),
        key=lambda i: (singles[i][1].total_s, singles[i][0].node_id),
    )
    out: list[Estimate] = []
    for index, (node, guess) in enumerate(singles):
        other = next((i for i in order if i != index), None)
        total = guess.total_s
        retry = total if other is None else retry_overhead_s(singles[other][1])
        out.append(with_risk(guess, single_risk(node, total, retry, config)))
    return out
