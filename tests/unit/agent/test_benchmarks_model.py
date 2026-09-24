"""G7-A (0.3.4, D31/D36): the ScoreSet model - JSON round trip and the
"never a number standing in for unknown" validators."""

from __future__ import annotations

import json
import math
import struct
from datetime import datetime

import pytest
from mf_bench_fakes import make_scoreset, ok_metric
from pydantic import ValidationError

from meshforge.agent.benchmarks.model import (
    Metric,
    MetricName,
    MetricReason,
    MetricStatus,
    ScoreSet,
)
from meshforge.common.ids import new_session_id

pytestmark = pytest.mark.unit


def _bits(value: float) -> bytes:
    return struct.pack("<d", value)


def test_json_round_trip_is_lossless() -> None:
    original = make_scoreset()
    again = ScoreSet.from_json(original.to_json())
    assert again == original
    for a, b in zip(original.metrics, again.metrics, strict=True):
        if a.mean is not None:
            assert b.mean is not None and _bits(a.mean) == _bits(b.mean)
    assert again.measured_at == original.measured_at
    assert again.measured_at.microsecond == 123456
    assert again.measured_at.utcoffset() is not None


def test_json_shape_is_plain_and_uses_stable_codes() -> None:
    doc = json.loads(make_scoreset().to_json())
    assert set(doc) == {
        "run_id",
        "measured_at",
        "score_version",
        "fingerprint",
        "conditions",
        "metrics",
    }
    assert doc["run_id"].startswith("score_")
    gpu = doc["metrics"][2]
    assert gpu["status"] == "unavailable"
    assert gpu["reason"] == "torch_not_installed"
    assert gpu["mean"] is None and gpu["gpu_index"] == 0


@pytest.mark.parametrize("mean", [0.0, -1.0, math.nan, math.inf])
def test_ok_metric_refuses_a_non_positive_or_non_finite_mean(mean: float) -> None:
    with pytest.raises(ValidationError):
        Metric(
            name=MetricName.CPU_FP32_GFLOPS_1T,
            status=MetricStatus.OK,
            mean=mean,
            stdev=0.0,
            n=5,
        )


def test_ok_metric_needs_five_repetitions() -> None:
    with pytest.raises(ValidationError, match="n >= 5"):
        Metric(
            name=MetricName.CPU_FP32_GFLOPS_1T,
            status=MetricStatus.OK,
            mean=1.0,
            stdev=0.0,
            n=4,
        )
    assert ok_metric(MetricName.CPU_FP32_GFLOPS_1T).n == 5


@pytest.mark.parametrize("status", [MetricStatus.UNAVAILABLE, MetricStatus.FAILED])
def test_a_not_measured_metric_carries_no_value_and_needs_a_reason(
    status: MetricStatus,
) -> None:
    with pytest.raises(ValidationError, match="carries no value"):
        Metric(
            name=MetricName.MEM_TRIAD_GB_S,
            status=status,
            reason=MetricReason.TIMEOUT,
            mean=0.0,
        )
    with pytest.raises(ValidationError, match="needs a reason"):
        Metric(name=MetricName.MEM_TRIAD_GB_S, status=status)


def test_gpu_index_is_required_for_gpu_metrics_only() -> None:
    with pytest.raises(ValidationError, match="gpu_index"):
        ok_metric(MetricName.GPU_FP32_TFLOPS)
    with pytest.raises(ValidationError, match="gpu_index"):
        ok_metric(MetricName.CPU_FP32_GFLOPS_ALL, gpu_index=0)
    assert ok_metric(MetricName.GPU_FP32_TFLOPS, gpu_index=1).key == (
        "gpu1.gpu_fp32_tflops"
    )


def test_duplicate_metrics_are_rejected() -> None:
    metric = ok_metric(MetricName.CPU_FP32_GFLOPS_1T)
    with pytest.raises(ValidationError, match="twice"):
        make_scoreset(metrics=(metric, metric))


def test_run_id_must_be_a_score_run_id() -> None:
    with pytest.raises(ValidationError):
        make_scoreset(run_id=new_session_id())


def test_measured_at_must_be_timezone_aware() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        make_scoreset(measured_at=datetime(2026, 9, 24, 12, 0))  # noqa: DTZ001


def test_fingerprint_must_be_sha256_hex() -> None:
    with pytest.raises(ValidationError):
        make_scoreset(fingerprint="not-a-hash")


def test_detail_is_clipped_and_single_line() -> None:
    metric = Metric.not_measured(
        MetricName.MEM_TRIAD_GB_S,
        MetricStatus.FAILED,
        MetricReason.CHILD_CRASHED,
        "line one\nline two  " + "x" * 1000,
    )
    assert "\n" not in metric.detail
    assert len(metric.detail) == 300


def test_statuses_carry_codes_not_numbers() -> None:
    statuses = make_scoreset().statuses()
    assert statuses == {
        "cpu_fp32_gflops_1t": "ok",
        "mem_triad_gb_s": "ok",
        "gpu0.gpu_fp32_tflops": "unavailable:torch_not_installed",
    }


def test_units_follow_the_metric() -> None:
    assert ok_metric(MetricName.CPU_FP64_GFLOPS_ALL).unit == "GFLOP/s"
    assert ok_metric(MetricName.GPU_FP16_TFLOPS, gpu_index=0).unit == "TFLOP/s"
    assert ok_metric(MetricName.GPU_MEM_GB_S, gpu_index=0).unit == "GB/s"
