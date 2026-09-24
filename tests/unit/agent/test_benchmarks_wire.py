"""G7-B (0.3.4, D37): ScoreSet <-> HardwareScores proto is lossless, and a
malformed message is refused rather than half-parsed."""

from __future__ import annotations

import math
import struct

import pytest
from mf_bench_fakes import make_scoreset, ok_metric

from meshforge.agent.benchmarks.model import (
    GPU_METRICS,
    HOST_METRICS,
    Conditions,
    Metric,
    MetricName,
    MetricReason,
    MetricStatus,
    ScoreSet,
)
from meshforge.agent.benchmarks.wire import from_proto, to_proto
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


def _bits(value: float) -> bytes:
    return struct.pack("<d", value)


def _through_the_wire(message: agent_pb2.HardwareScores) -> agent_pb2.HardwareScores:
    return agent_pb2.HardwareScores.FromString(message.SerializeToString())


def _full_set() -> ScoreSet:
    """Every metric, every optional field both set and unset somewhere."""
    metrics: list[Metric] = [
        ok_metric(name, mean=1.0 / 3.0 + i) for i, name in enumerate(HOST_METRICS)
    ]
    metrics[1] = metrics[1].model_copy(
        update={"lower_bound": True, "throttling_suspected": None}
    )
    for gpu in (0, 3):
        metrics.append(ok_metric(MetricName.GPU_FP32_TFLOPS, 8.123456789, gpu))
        metrics.append(
            Metric.not_measured(
                MetricName.GPU_FP16_TFLOPS,
                MetricStatus.FAILED,
                MetricReason.DEVICE_MISMATCH,
                "torch reports a different device name",
                gpu_index=gpu,
                backend="torch",
                backend_version="2.9.0+cu128",
            )
        )
        metrics.append(
            Metric.not_measured(
                MetricName.GPU_MEM_GB_S,
                MetricStatus.UNAVAILABLE,
                MetricReason.CUDA_UNAVAILABLE,
                gpu_index=gpu,
            )
        )
    return make_scoreset(
        metrics=tuple(metrics),
        conditions=Conditions(
            on_battery=None,
            cpu_load_percent_before=None,
            background_load_high=None,
            priority_lowered=False,
            budget_s=90.0,
            duration_s=0.0,
            budget_exhausted=True,
        ),
    )


def test_round_trip_is_lossless_through_serialised_bytes() -> None:
    originals: tuple[ScoreSet, ...] = (make_scoreset(), _full_set())
    for original in originals:
        again = from_proto(_through_the_wire(to_proto(original)))
        assert again == original
        for a, b in zip(original.metrics, again.metrics, strict=True):
            for x, y in ((a.mean, b.mean), (a.stdev, b.stdev)):
                assert (x is None) == (y is None)
                if x is not None and y is not None:
                    assert _bits(x) == _bits(y)
        assert again.measured_at.microsecond == original.measured_at.microsecond


def test_unknown_stays_absent_never_zero() -> None:
    wire = to_proto(_full_set())
    assert not wire.conditions.HasField("on_battery")
    assert not wire.conditions.HasField("cpu_load_percent_before")
    unavailable = [
        s for s in wire.scores if s.status == agent_pb2.SCORE_STATUS_UNAVAILABLE
    ]
    assert unavailable
    for score in unavailable:
        assert not score.HasField("mean")
        assert not score.HasField("stdev")
        assert score.reason == "cuda_unavailable"
    host = [s for s in wire.scores if not s.HasField("gpu_index")]
    assert len(host) == len(HOST_METRICS)


def test_every_metric_and_status_has_a_proto_value() -> None:
    names = {agent_pb2.ScoreMetric.Name(v) for v in agent_pb2.ScoreMetric.values()}
    for metric in (*HOST_METRICS, *GPU_METRICS):
        assert f"SCORE_METRIC_{metric.name}" in names
    statuses = {agent_pb2.ScoreStatus.Name(v) for v in agent_pb2.ScoreStatus.values()}
    for status in MetricStatus:
        assert f"SCORE_STATUS_{status.name}" in statuses


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [
        (lambda m: setattr(m.scores[0], "metric", 0), "UNSPECIFIED"),
        (lambda m: setattr(m.scores[0], "metric", 99), "not a known metric"),
        (lambda m: setattr(m.scores[0], "status", 0), "UNSPECIFIED"),
        (lambda m: setattr(m.scores[0], "status", 42), "not a known status"),
        (lambda m: setattr(m.scores[0], "mean", math.nan), "finite"),
        (lambda m: m.ClearField("measured_at"), "measured_at"),
        (lambda m: m.ClearField("conditions"), "conditions"),
        (lambda m: setattr(m, "run_id", "job_01J00000000000000000000000"), "run_id"),
    ],
)
def test_malformed_message_is_refused(mutate: object, needle: str) -> None:
    wire = to_proto(make_scoreset())
    mutate(wire)  # type: ignore[operator]
    with pytest.raises(ValueError, match=needle):
        from_proto(wire)


def test_hello_and_report_carry_the_same_message() -> None:
    scores = to_proto(make_scoreset())
    hello = agent_pb2.Hello(node_id="n", hardware_scores=scores)
    report = agent_pb2.AgentMessage(message_id="m", hardware_scores=scores)
    assert (
        agent_pb2.Hello.FromString(hello.SerializeToString()).hardware_scores == scores
    )
    parsed = agent_pb2.AgentMessage.FromString(report.SerializeToString())
    assert parsed.WhichOneof("payload") == "hardware_scores"
    assert parsed.hardware_scores == scores
