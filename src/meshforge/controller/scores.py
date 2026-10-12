"""Hardware scores on the controller: proto -> stored document (roadmap 0.3.4, D38).

The agent's :class:`~meshforge.agent.benchmarks.model.ScoreSet` arrives as a
``HardwareScores`` proto (``Hello.hardware_scores`` or the ``hardware_scores``
report). The controller never imports the agent, so this module owns the
controller's side of the mapping: a **validated** message (``session/limits.py``
``check_hardware_scores`` runs first - this module assumes it did) becomes the JSON
document stored in ``node.hardware_scores`` and served by REST.

The document uses the agent's snake_case names (``cpu_fp32_gflops_1t``, ``ok``),
keeps "unknown" as ``null`` (never ``0.0``) and writes ``measured_at`` as
ISO-8601 with an explicit UTC offset - the registry's ordering guard compares that
string as a ``timestamptz``, so :func:`measured_at_iso` is the only formatter.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = [
    "GPU_METRICS",
    "METRIC_NAMES",
    "STATUS_NAMES",
    "UNITS",
    "measured_at_iso",
    "metric_name",
    "scores_document",
    "status_name",
]

_METRIC_PREFIX: Final = "SCORE_METRIC_"
_STATUS_PREFIX: Final = "SCORE_STATUS_"

METRIC_NAMES: Final[frozenset[str]] = frozenset(
    name.removeprefix(_METRIC_PREFIX).lower()
    for name in agent_pb2.ScoreMetric.DESCRIPTOR.values_by_name
    if name != "SCORE_METRIC_UNSPECIFIED"
)
"""Every metric this build knows, as stored (``cpu_fp32_gflops_1t``...)."""

STATUS_NAMES: Final[frozenset[str]] = frozenset(
    name.removeprefix(_STATUS_PREFIX).lower()
    for name in agent_pb2.ScoreStatus.DESCRIPTOR.values_by_name
    if name != "SCORE_STATUS_UNSPECIFIED"
)

GPU_METRICS: Final[frozenset[str]] = frozenset(
    {"gpu_fp32_tflops", "gpu_fp16_tflops", "gpu_mem_gb_s"}
)
"""Metrics measured per GPU (``gpu_index`` required); every other one is per host."""

UNITS: Final[dict[str, str]] = {
    "cpu_fp32_gflops_1t": "GFLOP/s",
    "cpu_fp32_gflops_all": "GFLOP/s",
    "cpu_fp64_gflops_all": "GFLOP/s",
    "mem_triad_gb_s": "GB/s",
    "gpu_fp32_tflops": "TFLOP/s",
    "gpu_fp16_tflops": "TFLOP/s",
    "gpu_mem_gb_s": "GB/s",
}
"""Display unit per metric. ``test_every_known_metric_has_a_unit`` keeps it total."""


def metric_name(value: agent_pb2.ScoreMetric.ValueType) -> str | None:
    """``SCORE_METRIC_CPU_FP32_GFLOPS_1T`` -> ``cpu_fp32_gflops_1t``; None if unknown."""
    if value == agent_pb2.SCORE_METRIC_UNSPECIFIED:
        return None
    try:
        return agent_pb2.ScoreMetric.Name(value).removeprefix(_METRIC_PREFIX).lower()
    except ValueError:
        return None


def status_name(value: agent_pb2.ScoreStatus.ValueType) -> str | None:
    """``SCORE_STATUS_OK`` -> ``ok``; None if unknown or unspecified."""
    if value == agent_pb2.SCORE_STATUS_UNSPECIFIED:
        return None
    try:
        return agent_pb2.ScoreStatus.Name(value).removeprefix(_STATUS_PREFIX).lower()
    except ValueError:
        return None


def measured_at_iso(moment: dt.datetime) -> str:
    """The one ``measured_at`` format: ISO-8601, microseconds, ``+00:00``."""
    return moment.astimezone(dt.UTC).isoformat(timespec="microseconds")


def _opt(message: Any, field: str) -> Any:
    return getattr(message, field) if message.HasField(field) else None


def _metric_document(score: agent_pb2.Score) -> dict[str, Any]:
    return {
        "name": metric_name(score.metric),
        "gpu_index": _opt(score, "gpu_index"),
        "status": status_name(score.status),
        "mean": _opt(score, "mean"),
        "stdev": _opt(score, "stdev"),
        "n": score.n,
        "backend": _opt(score, "backend"),
        "backend_version": _opt(score, "backend_version"),
        "threads_effective": _opt(score, "threads_effective"),
        "lower_bound": score.lower_bound,
        "throttling_suspected": _opt(score, "throttling_suspected"),
        "reason": score.reason or None,
        "detail": score.detail,
    }


def scores_document(
    message: agent_pb2.HardwareScores, *, node_id: str
) -> tuple[dict[str, Any], dt.datetime]:
    """The stored JSON document and its ``measured_at`` (UTC).

    Precondition: ``check_hardware_scores(message, ...) is None``. Metrics keep the order
    the agent sent them in. ``node_id`` is the node whose **certificate-authenticated**
    session reported the set (V0.7 G5, D145b): the agent's ``fingerprint`` is
    stored bound to it, and readers use a document only for that node
    (:func:`meshforge.controller.planner.inputs.bound_to`).
    """
    measured_at = message.measured_at.ToDatetime(tzinfo=dt.UTC)
    conditions = message.conditions
    document: dict[str, Any] = {
        "run_id": message.run_id,
        "measured_at": measured_at_iso(measured_at),
        "score_version": message.score_version,
        "fingerprint": message.fingerprint,
        "node_id": node_id,
        "conditions": {
            "on_battery": _opt(conditions, "on_battery"),
            "cpu_load_percent_before": _opt(conditions, "cpu_load_percent_before"),
            "background_load_high": _opt(conditions, "background_load_high"),
            "priority_lowered": conditions.priority_lowered,
            "budget_s": conditions.budget_s,
            "duration_s": conditions.duration_s,
            "budget_exhausted": conditions.budget_exhausted,
        },
        "metrics": [_metric_document(score) for score in message.scores],
    }
    return document, measured_at
