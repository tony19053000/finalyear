"""``ScoreSet`` <-> ``HardwareScores`` proto (roadmap 0.3.4, brief §5g D37).

The wire form is lossless: every field of :class:`~.model.ScoreSet` has a proto
field, "unknown" stays *absent* (proto3 ``optional``), never ``0.0``, and floats
travel as ``double``. :func:`from_proto` validates through the Pydantic model, so a
malformed message raises :class:`ValueError` (``pydantic.ValidationError`` is one)
rather than yielding a half-valid set.

Enum mapping is by name: ``MetricName.CPU_FP32_GFLOPS_1T`` <->
``SCORE_METRIC_CPU_FP32_GFLOPS_1T``, ``MetricStatus.OK`` <-> ``SCORE_STATUS_OK``.
``reason`` is the :class:`~.model.MetricReason` value as a string (the controller
checks its shape, not its membership, so a newer agent's new reason code does not
make an older controller drop the whole set).
"""

from __future__ import annotations

from datetime import UTC
from typing import Final

from meshforge.agent.benchmarks.model import (
    Conditions,
    Metric,
    MetricName,
    MetricReason,
    MetricStatus,
    ScoreSet,
)
from meshforge.common.proto.meshforge.v1 import agent_pb2

__all__ = ["from_proto", "to_proto"]

_METRIC_PREFIX: Final = "SCORE_METRIC_"
_STATUS_PREFIX: Final = "SCORE_STATUS_"


def _metric_to_proto(name: MetricName) -> agent_pb2.ScoreMetric.ValueType:
    return agent_pb2.ScoreMetric.Value(_METRIC_PREFIX + name.name)


def _status_to_proto(status: MetricStatus) -> agent_pb2.ScoreStatus.ValueType:
    return agent_pb2.ScoreStatus.Value(_STATUS_PREFIX + status.name)


def _metric_from_proto(value: int) -> MetricName:
    if value == agent_pb2.SCORE_METRIC_UNSPECIFIED:
        raise ValueError("Score.metric is SCORE_METRIC_UNSPECIFIED")
    try:
        name = agent_pb2.ScoreMetric.Name(value)
    except ValueError:
        raise ValueError(f"Score.metric {value} is not a known metric") from None
    return MetricName[name.removeprefix(_METRIC_PREFIX)]


def _status_from_proto(value: int) -> MetricStatus:
    if value == agent_pb2.SCORE_STATUS_UNSPECIFIED:
        raise ValueError("Score.status is SCORE_STATUS_UNSPECIFIED")
    try:
        name = agent_pb2.ScoreStatus.Name(value)
    except ValueError:
        raise ValueError(f"Score.status {value} is not a known status") from None
    return MetricStatus[name.removeprefix(_STATUS_PREFIX)]


def _score_to_proto(metric: Metric) -> agent_pb2.Score:
    out = agent_pb2.Score(
        metric=_metric_to_proto(metric.name),
        status=_status_to_proto(metric.status),
        n=metric.n,
        lower_bound=metric.lower_bound,
        reason=metric.reason.value if metric.reason is not None else "",
        detail=metric.detail,
    )
    if metric.gpu_index is not None:
        out.gpu_index = metric.gpu_index
    if metric.mean is not None:
        out.mean = metric.mean
    if metric.stdev is not None:
        out.stdev = metric.stdev
    if metric.backend is not None:
        out.backend = metric.backend
    if metric.backend_version is not None:
        out.backend_version = metric.backend_version
    if metric.threads_effective is not None:
        out.threads_effective = metric.threads_effective
    if metric.throttling_suspected is not None:
        out.throttling_suspected = metric.throttling_suspected
    return out


def _score_from_proto(score: agent_pb2.Score) -> Metric:
    return Metric(
        name=_metric_from_proto(score.metric),
        gpu_index=score.gpu_index if score.HasField("gpu_index") else None,
        status=_status_from_proto(score.status),
        mean=score.mean if score.HasField("mean") else None,
        stdev=score.stdev if score.HasField("stdev") else None,
        n=score.n,
        backend=score.backend if score.HasField("backend") else None,
        backend_version=(
            score.backend_version if score.HasField("backend_version") else None
        ),
        threads_effective=(
            score.threads_effective if score.HasField("threads_effective") else None
        ),
        lower_bound=score.lower_bound,
        throttling_suspected=(
            score.throttling_suspected
            if score.HasField("throttling_suspected")
            else None
        ),
        reason=MetricReason(score.reason) if score.reason else None,
        detail=score.detail,
    )


def _conditions_to_proto(conditions: Conditions) -> agent_pb2.ScoreConditions:
    out = agent_pb2.ScoreConditions(
        priority_lowered=conditions.priority_lowered,
        budget_s=conditions.budget_s,
        duration_s=conditions.duration_s,
        budget_exhausted=conditions.budget_exhausted,
    )
    if conditions.on_battery is not None:
        out.on_battery = conditions.on_battery
    if conditions.cpu_load_percent_before is not None:
        out.cpu_load_percent_before = conditions.cpu_load_percent_before
    if conditions.background_load_high is not None:
        out.background_load_high = conditions.background_load_high
    return out


def _conditions_from_proto(conditions: agent_pb2.ScoreConditions) -> Conditions:
    return Conditions(
        on_battery=(
            conditions.on_battery if conditions.HasField("on_battery") else None
        ),
        cpu_load_percent_before=(
            conditions.cpu_load_percent_before
            if conditions.HasField("cpu_load_percent_before")
            else None
        ),
        background_load_high=(
            conditions.background_load_high
            if conditions.HasField("background_load_high")
            else None
        ),
        priority_lowered=conditions.priority_lowered,
        budget_s=conditions.budget_s,
        duration_s=conditions.duration_s,
        budget_exhausted=conditions.budget_exhausted,
    )


def to_proto(scores: ScoreSet) -> agent_pb2.HardwareScores:
    """The wire form of ``scores`` (``Hello.hardware_scores`` / the report)."""
    out = agent_pb2.HardwareScores(
        run_id=scores.run_id,
        score_version=scores.score_version,
        fingerprint=scores.fingerprint,
        conditions=_conditions_to_proto(scores.conditions),
        scores=[_score_to_proto(metric) for metric in scores.metrics],
    )
    out.measured_at.FromDatetime(scores.measured_at)
    return out


def from_proto(message: agent_pb2.HardwareScores) -> ScoreSet:
    """Parse and validate; raises :class:`ValueError` on anything malformed."""
    if not message.HasField("measured_at"):
        raise ValueError("HardwareScores.measured_at is missing")
    if not message.HasField("conditions"):
        raise ValueError("HardwareScores.conditions is missing")
    return ScoreSet(
        run_id=message.run_id,
        measured_at=message.measured_at.ToDatetime(tzinfo=UTC),
        score_version=message.score_version,
        fingerprint=message.fingerprint,
        conditions=_conditions_from_proto(message.conditions),
        metrics=tuple(_score_from_proto(score) for score in message.scores),
    )
