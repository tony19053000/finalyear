"""``ScoreSet``: one hardware-score run, typed (roadmap 0.3.4, brief §5g D31/D36).

A score is a feature the planner's cost model (ARCHITECTURE §7) and later the ML
predictors (V0.15) consume, so the rule is the G4 one: **a wrong number stored
quietly is worse than no number.** Every :class:`Metric` is therefore either a
measured value *with its conditions* (``OK``: mean, stdev, n >= 5, backend,
threads, lower-bound and throttling flags) or a typed "not measured, because ..."
(``UNAVAILABLE`` - this machine cannot measure it; ``FAILED`` - it tried and it
broke). Never a ``0.0`` standing in for "unknown": the validators reject that.

The model is frozen Pydantic and round-trips through JSON losslessly
(:meth:`ScoreSet.to_json` / :meth:`ScoreSet.from_json`; floats are written with
``repr`` precision, timestamps in ISO-8601 with microseconds). Run B adds the proto
conversion.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from meshforge.common.ids import validate_score_run_id
from meshforge.common.score_codes import ScoreReason

__all__ = [
    "GPU_METRICS",
    "HOST_METRICS",
    "MIN_REPS",
    "UNITS",
    "Conditions",
    "Metric",
    "MetricName",
    "MetricReason",
    "MetricStatus",
    "ScoreSet",
]

MIN_REPS: Final = 5
"""An ``OK`` metric has at least this many measured repetitions (S7's rule)."""

_DETAIL_MAX: Final = 300
_BACKEND_MAX: Final = 64
_MAX_METRICS: Final = 4 + 3 * 16
"""Four host metrics plus three per GPU, for the 16-GPU cap of ``Hello.hardware``."""


class MetricName(StrEnum):
    """The fixed, small metric set (D31)."""

    CPU_FP32_GFLOPS_1T = "cpu_fp32_gflops_1t"
    CPU_FP32_GFLOPS_ALL = "cpu_fp32_gflops_all"
    CPU_FP64_GFLOPS_ALL = "cpu_fp64_gflops_all"
    MEM_TRIAD_GB_S = "mem_triad_gb_s"
    GPU_FP32_TFLOPS = "gpu_fp32_tflops"
    GPU_FP16_TFLOPS = "gpu_fp16_tflops"
    GPU_MEM_GB_S = "gpu_mem_gb_s"


HOST_METRICS: Final = (
    MetricName.CPU_FP32_GFLOPS_1T,
    MetricName.CPU_FP32_GFLOPS_ALL,
    MetricName.CPU_FP64_GFLOPS_ALL,
    MetricName.MEM_TRIAD_GB_S,
)
GPU_METRICS: Final = (
    MetricName.GPU_FP32_TFLOPS,
    MetricName.GPU_FP16_TFLOPS,
    MetricName.GPU_MEM_GB_S,
)
UNITS: Final[dict[MetricName, str]] = {
    MetricName.CPU_FP32_GFLOPS_1T: "GFLOP/s",
    MetricName.CPU_FP32_GFLOPS_ALL: "GFLOP/s",
    MetricName.CPU_FP64_GFLOPS_ALL: "GFLOP/s",
    MetricName.MEM_TRIAD_GB_S: "GB/s",
    MetricName.GPU_FP32_TFLOPS: "TFLOP/s",
    MetricName.GPU_FP16_TFLOPS: "TFLOP/s",
    MetricName.GPU_MEM_GB_S: "GB/s",
}


class MetricStatus(StrEnum):
    OK = "ok"
    """Measured: mean/stdev over >= 5 repetitions."""
    UNAVAILABLE = "unavailable"
    """This machine cannot measure it (no BLAS, no torch, no usable CUDA)."""
    FAILED = "failed"
    """A measurement was attempted and broke (timeout, crash, device mismatch...)."""


MetricReason = ScoreReason
"""Why a metric is not ``OK``: the shared, frozen :class:`~meshforge.common.score_codes
.ScoreReason` (roadmap 0.4.17 item 4) - one set of codes for the agent that sets them
and the controller that validates them."""


class _Frozen(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, hide_input_in_errors=True
    )


class Metric(_Frozen):
    """One metric of one run: a measured value with its conditions, or why not."""

    name: MetricName
    gpu_index: int | None = Field(
        default=None,
        ge=0,
        description="NVML index (= HardwareSummary.Gpu.index) for GPU metrics, else None.",
    )
    status: MetricStatus
    mean: float | None = None
    stdev: float | None = None
    n: int = Field(default=0, ge=0, le=10_000)
    backend: str | None = Field(default=None, max_length=_BACKEND_MAX)
    backend_version: str | None = Field(default=None, max_length=_BACKEND_MAX)
    threads_effective: int | None = Field(default=None, ge=1)
    lower_bound: bool = False
    """The backend could not use the whole machine (numpy's STREAM kernels)."""
    throttling_suspected: bool | None = None
    """S7's stability flag; None when the run was too short to judge or not OK."""
    reason: MetricReason | None = None
    detail: str = Field(default="", max_length=_DETAIL_MAX)
    """Human-readable context for a non-OK status (no paths, no secrets)."""

    @property
    def unit(self) -> str:
        return UNITS[self.name]

    @property
    def is_gpu(self) -> bool:
        return self.name in GPU_METRICS

    @field_validator("detail", mode="before")
    @classmethod
    def _clip_detail(cls, value: object) -> object:
        if isinstance(value, str):
            return " ".join(value.split())[:_DETAIL_MAX]
        return value

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if (self.gpu_index is not None) != self.is_gpu:
            raise ValueError(f"{self.name}: gpu_index is required for GPU metrics only")
        if self.status is MetricStatus.OK:
            if self.reason is not None:
                raise ValueError(f"{self.name}: an OK metric has no reason")
            if self.mean is None or not (math.isfinite(self.mean) and self.mean > 0):
                raise ValueError(f"{self.name}: an OK metric needs a finite mean > 0")
            if self.stdev is None or not (
                math.isfinite(self.stdev) and self.stdev >= 0
            ):
                raise ValueError(f"{self.name}: an OK metric needs a finite stdev >= 0")
            if self.n < MIN_REPS:
                raise ValueError(f"{self.name}: an OK metric needs n >= {MIN_REPS}")
        else:
            if self.reason is None:
                raise ValueError(f"{self.name}: a {self.status} metric needs a reason")
            if self.mean is not None or self.stdev is not None:
                raise ValueError(
                    f"{self.name}: a {self.status} metric carries no value "
                    "(never a number standing in for 'unknown')"
                )
        return self

    @classmethod
    def not_measured(
        cls,
        name: MetricName,
        status: MetricStatus,
        reason: MetricReason,
        detail: str = "",
        *,
        gpu_index: int | None = None,
        backend: str | None = None,
        backend_version: str | None = None,
    ) -> Metric:
        return cls(
            name=name,
            gpu_index=gpu_index,
            status=status,
            reason=reason,
            detail=detail,
            backend=backend,
            backend_version=backend_version,
        )

    @property
    def key(self) -> str:
        """``cpu_fp32_gflops_1t`` or ``gpu0.gpu_fp32_tflops`` (logs, lookups)."""
        return (
            self.name.value
            if self.gpu_index is None
            else f"gpu{self.gpu_index}.{self.name.value}"
        )


class Conditions(_Frozen):
    """How the run was measured (D36): recorded, never used to refuse."""

    on_battery: bool | None = None
    """True/False from the OS battery API; None = unknown (no API, or no battery info)."""
    cpu_load_percent_before: float | None = Field(default=None, ge=0, le=100)
    """Machine-wide CPU utilisation over the ~2 s before the first case."""
    background_load_high: bool | None = None
    """``cpu_load_percent_before > 25``; None when the load is unknown."""
    priority_lowered: bool = False
    """The benchmark child ran at lowered OS priority (``nice 10`` / below-normal)."""
    budget_s: float = Field(gt=0)
    duration_s: float = Field(ge=0)
    budget_exhausted: bool = False
    """The total budget ran out before every planned case finished."""


class ScoreSet(_Frozen):
    """One complete scoring run of this node."""

    run_id: str
    measured_at: datetime
    score_version: int = Field(ge=1)
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    conditions: Conditions
    metrics: tuple[Metric, ...] = Field(max_length=_MAX_METRICS)

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, value: str) -> str:
        return validate_score_run_id(value)

    @field_validator("measured_at")
    @classmethod
    def _utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("measured_at must be timezone-aware (UTC)")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def _unique_metrics(self) -> Self:
        keys = [m.key for m in self.metrics]
        if len(keys) != len(set(keys)):
            raise ValueError("a metric appears twice")
        return self

    def metric(self, name: MetricName, gpu_index: int | None = None) -> Metric | None:
        for metric in self.metrics:
            if metric.name is name and metric.gpu_index == gpu_index:
                return metric
        return None

    def statuses(self) -> dict[str, str]:
        """``{"cpu_fp32_gflops_1t": "ok", "gpu0.gpu_fp32_tflops": "unavailable:..."}``.

        What the logs carry instead of numbers (G7-A brief item 7).
        """
        return {
            m.key: m.status.value
            if m.reason is None
            else f"{m.status.value}:{m.reason.value}"
            for m in self.metrics
        }

    def to_json(self) -> str:
        return self.model_dump_json()

    @classmethod
    def from_json(cls, text: str | bytes) -> ScoreSet:
        """Raises ``pydantic.ValidationError`` on anything malformed."""
        return cls.model_validate_json(text)
