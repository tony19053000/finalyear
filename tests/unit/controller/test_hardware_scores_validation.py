"""G7-B (0.3.4, D38): ingestion validation of ``HardwareScores`` and the stored
document.

Each rejection class is triggered on an otherwise valid message built by the
agent's own converter (``to_proto``), so the test also pins that what the agent
sends is accepted. Reasons must name the field and never echo the value.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import pytest
from mf_bench_fakes import make_scoreset, ok_metric

from meshforge.agent.benchmarks import model as agent_model
from meshforge.agent.benchmarks.model import (
    GPU_METRICS,
    HOST_METRICS,
    Metric,
    MetricName,
    MetricReason,
    MetricStatus,
)
from meshforge.agent.benchmarks.wire import to_proto
from meshforge.common.ids import new_score_run_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller import scores as ctl_scores
from meshforge.controller.session.limits import (
    MAX_GPUS,
    MAX_SCORE_METRICS,
    SCORE_BACKEND_MAX_LENGTH,
    SCORE_DETAIL_MAX_LENGTH,
    check_hardware_scores,
)

pytestmark = pytest.mark.unit


def _valid() -> agent_pb2.HardwareScores:
    return to_proto(make_scoreset())


def test_a_valid_set_from_the_agent_is_accepted() -> None:
    assert check_hardware_scores(_valid()) is None


def test_unicode_backend_names_and_details_are_accepted() -> None:
    scores = _valid()
    scores.scores[0].backend = "numpy-ÄÖÜ"
    scores.scores[0].backend_version = "2.5.3+ローカル"
    scores.scores[2].detail = "torch fehlt - トーチなし"
    assert check_hardware_scores(scores) is None


def test_the_agent_and_controller_caps_agree() -> None:
    assert MAX_SCORE_METRICS == agent_model._MAX_METRICS
    assert SCORE_BACKEND_MAX_LENGTH == agent_model._BACKEND_MAX
    assert SCORE_DETAIL_MAX_LENGTH == agent_model._DETAIL_MAX
    assert {m.value for m in GPU_METRICS} == ctl_scores.GPU_METRICS
    assert {m.value for m in (*HOST_METRICS, *GPU_METRICS)} == ctl_scores.METRIC_NAMES
    assert {s.value for s in MetricStatus} == ctl_scores.STATUS_NAMES


def test_every_known_metric_has_a_unit() -> None:
    assert set(ctl_scores.UNITS) == ctl_scores.METRIC_NAMES
    for name in MetricName:
        assert ctl_scores.UNITS[name.value] == agent_model.UNITS[name]


type Mutation = Callable[[agent_pb2.HardwareScores], None]


def _set(path: str, value: Any) -> Mutation:
    def mutate(scores: agent_pb2.HardwareScores) -> None:
        *parents, leaf = path.split(".")
        target: Any = scores
        for part in parents:
            target = target[int(part)] if part.isdigit() else getattr(target, part)
        setattr(target, leaf, value)

    return mutate


def _clear(path: str) -> Mutation:
    def mutate(scores: agent_pb2.HardwareScores) -> None:
        *parents, leaf = path.split(".")
        target: Any = scores
        for part in parents:
            target = target[int(part)] if part.isdigit() else getattr(target, part)
        target.ClearField(leaf)

    return mutate


def _too_many_metrics(scores: agent_pb2.HardwareScores) -> None:
    for i in range(MAX_SCORE_METRICS):
        scores.scores.append(
            agent_pb2.Score(
                metric=agent_pb2.SCORE_METRIC_GPU_MEM_GB_S,
                gpu_index=100 + i,
                status=agent_pb2.SCORE_STATUS_UNAVAILABLE,
                reason="cuda_unavailable",
            )
        )


def _too_many_gpus(scores: agent_pb2.HardwareScores) -> None:
    del scores.scores[:]
    for i in range(MAX_GPUS + 1):
        scores.scores.append(
            agent_pb2.Score(
                metric=agent_pb2.SCORE_METRIC_GPU_FP32_TFLOPS,
                gpu_index=i,
                status=agent_pb2.SCORE_STATUS_UNAVAILABLE,
                reason="torch_not_installed",
            )
        )


def _duplicate(scores: agent_pb2.HardwareScores) -> None:
    scores.scores.append(scores.scores[0])


def _oversized(scores: agent_pb2.HardwareScores) -> None:
    for score in scores.scores:
        score.detail = "x" * 70_000


REJECTIONS: list[tuple[str, Mutation, str]] = [
    # NaN / inf / negative / zero-as-unknown
    ("nan mean", _set("scores.0.mean", math.nan), "scores[0].mean"),
    ("inf mean", _set("scores.0.mean", math.inf), "scores[0].mean"),
    ("-inf stdev", _set("scores.0.stdev", -math.inf), "scores[0].stdev"),
    ("negative mean", _set("scores.0.mean", -1.0), "scores[0].mean"),
    ("zero mean", _set("scores.0.mean", 0.0), "scores[0].mean"),
    ("negative stdev", _set("scores.0.stdev", -0.5), "scores[0].stdev"),
    ("ok without mean", _clear("scores.0.mean"), "scores[0].mean"),
    ("ok with n < 5", _set("scores.0.n", 4), "scores[0].n"),
    ("n unbounded", _set("scores.0.n", 10_001), "scores[0].n"),
    ("ok with a reason", _set("scores.0.reason", "timeout"), "scores[0].reason"),
    ("unavailable with a mean", _set("scores.2.mean", 1.0), "carries a mean"),
    ("unavailable w/o reason", _set("scores.2.reason", ""), "scores[2].reason"),
    ("reason not a code", _set("scores.2.reason", "Timed Out!"), "scores[2].reason"),
    ("nan budget", _set("conditions.budget_s", math.nan), "budget_s"),
    ("zero budget", _set("conditions.budget_s", 0.0), "budget_s"),
    ("negative duration", _set("conditions.duration_s", -1.0), "duration_s"),
    ("inf duration", _set("conditions.duration_s", math.inf), "duration_s"),
    (
        "load > 100",
        _set("conditions.cpu_load_percent_before", 101.0),
        "cpu_load_percent_before",
    ),
    (
        "nan load",
        _set("conditions.cpu_load_percent_before", math.nan),
        "cpu_load_percent_before",
    ),
    ("threads 0", _set("scores.0.threads_effective", 0), "threads_effective"),
    # unknown / unspecified enums
    ("metric unspecified", _set("scores.0.metric", 0), "scores[0].metric"),
    ("metric unknown", _set("scores.0.metric", 77), "scores[0].metric"),
    ("status unspecified", _set("scores.0.status", 0), "scores[0].status"),
    ("status unknown", _set("scores.0.status", 9), "scores[0].status"),
    # shape
    ("host metric with gpu", _set("scores.0.gpu_index", 0), "gpu_index"),
    ("gpu metric w/o gpu", _clear("scores.2.gpu_index"), "gpu_index"),
    ("gpu index huge", _set("scores.2.gpu_index", 70_000), "gpu_index"),
    ("duplicate metric", _duplicate, "repeats a metric"),
    ("too many metrics", _too_many_metrics, "entries; the maximum is"),
    ("too many gpus", _too_many_gpus, "GPUs; the maximum is"),
    ("oversized", _oversized, "bytes; the maximum is"),
    # identity / time
    ("bad run id", _set("run_id", "job_01J8Z3K5Q9V6W2X4Y7A0B1C2D3xxx"), "run_id"),
    ("other id kind", _set("run_id", "sess_01J8Z3K5Q9V6W2X4Y7A0B1C2D3"), "run_id"),
    ("bad fingerprint", _set("fingerprint", "Z" * 64), "fingerprint"),
    ("version 0", _set("score_version", 0), "score_version"),
    ("no measured_at", _clear("measured_at"), "measured_at"),
    ("bad timestamp", _set("measured_at.seconds", 10**15), "measured_at"),
    ("no conditions", _clear("conditions"), "conditions"),
    # strings (D22)
    (
        "backend too long",
        _set("scores.0.backend", "n" * (SCORE_BACKEND_MAX_LENGTH + 1)),
        "scores[0].backend is",
    ),
    (
        "detail too long",
        _set("scores.2.detail", "d" * (SCORE_DETAIL_MAX_LENGTH + 1)),
        "scores[2].detail is",
    ),
    ("escape in backend", _set("scores.0.backend", "numpy\x1b[31m"), "U+001B"),
    ("bidi in detail", _set("scores.2.detail", "ok\u202egnp.exe"), "U+202E"),
    ("newline in version", _set("scores.1.backend_version", "2.5\n3"), "U+000A"),
]


@pytest.mark.parametrize(
    ("mutate", "needle"),
    [pytest.param(m, n, id=i) for i, m, n in REJECTIONS],
)
def test_each_rejection_class(mutate: Mutation, needle: str) -> None:
    scores = _valid()
    mutate(scores)
    reason = check_hardware_scores(scores)
    assert reason is not None, "accepted a malformed score set"
    assert needle in reason
    assert "\x1b" not in reason and "\u202e" not in reason  # never echoes the value


def test_the_stored_document_keeps_unknown_as_null() -> None:
    original = make_scoreset(
        metrics=(
            ok_metric(MetricName.CPU_FP32_GFLOPS_ALL, 312.5),
            Metric.not_measured(
                MetricName.GPU_FP16_TFLOPS,
                MetricStatus.FAILED,
                MetricReason.TIMEOUT,
                "case timed out",
                gpu_index=1,
            ),
        )
    )
    message = to_proto(original)
    assert check_hardware_scores(message) is None
    document, measured_at = ctl_scores.scores_document(message)
    assert measured_at == original.measured_at
    assert document["measured_at"] == "2026-09-24T12:00:00.123456+00:00"
    assert document["run_id"] == original.run_id
    ok, failed = document["metrics"]
    assert ok["name"] == "cpu_fp32_gflops_all" and ok["mean"] == 312.5
    assert ok["gpu_index"] is None and ok["reason"] is None
    assert failed == {
        "name": "gpu_fp16_tflops",
        "gpu_index": 1,
        "status": "failed",
        "mean": None,
        "stdev": None,
        "n": 0,
        "backend": None,
        "backend_version": None,
        "threads_effective": None,
        "lower_bound": False,
        "throttling_suspected": None,
        "reason": "timeout",
        "detail": "case timed out",
    }
    assert document["conditions"]["on_battery"] is False
    assert document["conditions"]["budget_s"] == 90.0


def test_measured_at_iso_is_fixed_width_utc() -> None:
    import datetime as dt

    naive_offset = dt.datetime(
        2026, 1, 2, 3, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=5))
    )
    assert (
        ctl_scores.measured_at_iso(naive_offset) == "2026-01-01T22:04:05.000000+00:00"
    )


def test_an_empty_run_id_is_rejected_without_parsing() -> None:
    scores = _valid()
    scores.run_id = ""
    assert (
        check_hardware_scores(scores) == "HardwareScores.run_id is not a score_<ULID>"
    )
    scores.run_id = new_score_run_id()
    assert check_hardware_scores(scores) is None
