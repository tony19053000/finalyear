"""The workload-family contract and registry (V0.9 G2, roadmap 0.9.2, D214).

A **family** is one small, deterministic benchmark job type - ``cpu.matmul``,
``ml.cnn_train`` ... - described by:

* ``name``, ``kind`` (cpu / memory / gpu / ml) and a one-line description;
* a **pydantic parameter model** with per-field bounds (``params_model``);
* ``size_params``: the parameters whose growth makes the run longer (the hint
  is non-decreasing in each; a unit test holds every family to it);
* :meth:`Family.expected_runtime_s`: a rough compute-time hint from
  ``(params, HardwareHint)`` = work units / (node metric x efficiency), where the
  efficiency was calibrated on Laptop 1 (CPU families) or assumed (GPU / ML,
  no CUDA on Laptop 1 yet - see ``docs/BENCHMARKS.md``);
* :meth:`Family.run` / :meth:`Family.verify` - one in-process run with a
  checksum and the run's own self-check, through the **same** job-side code
  (:mod:`.kernels.runner`) a benchmark job runs.

**Run-time window (ADR-0012, D214).** Benchmark campaigns need thousands of runs,
so :meth:`Family.parse_params` refuses a parameter combination whose hint on the
:data:`MID_LAPTOP` reference is outside :data:`MIN_RUNTIME_S` ..
:data:`MAX_RUNTIME_S` (5 s - 3 min). Per-field bounds alone cannot express that
(``n`` and ``repeats`` trade off), and a refusal names the hint so a campaign
spec can be fixed before anything runs.

Nothing here imports numpy or torch: listing families and validating params
works on a plain install.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, fields
from enum import StrEnum
from typing import Any, ClassVar, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from meshforge.common.errors import FailureType, MeshForgeError

from .kernels._support import Unavailable
from .kernels.runner import MAX_SEED, RESULT_SCHEMA_VERSION, execute

__all__ = [
    "CALIBRATION_HW",
    "MAX_RUNTIME_S",
    "MAX_SEED",
    "MID_LAPTOP",
    "MIN_RUNTIME_S",
    "Family",
    "HardwareHint",
    "Kind",
    "Params",
    "ParamsError",
    "Unavailable",
    "UnknownFamilyError",
    "Verdict",
    "WorkloadResult",
    "get_family",
    "list_families",
    "register",
]

MIN_RUNTIME_S: Final = 5.0
MAX_RUNTIME_S: Final = 180.0


class Kind(StrEnum):
    CPU = "cpu"
    MEMORY = "memory"
    GPU = "gpu"
    ML = "ml"


Metric = Literal[
    "cpu_fp32_gflops_1t",
    "cpu_fp32_gflops_all",
    "cpu_fp64_gflops_all",
    "mem_triad_gb_s",
    "gpu_fp32_tflops",
    "gpu_fp16_tflops",
    "gpu_mem_gb_s",
]
"""Hardware-score metrics (the agent's ``hardware score`` names, V0.3 D30)."""

_SI: Final[dict[str, float]] = {
    "cpu_fp32_gflops_1t": 1e9,
    "cpu_fp32_gflops_all": 1e9,
    "cpu_fp64_gflops_all": 1e9,
    "mem_triad_gb_s": 1e9,
    "gpu_fp32_tflops": 1e12,
    "gpu_fp16_tflops": 1e12,
    "gpu_mem_gb_s": 1e9,
}


@dataclass(frozen=True)
class HardwareHint:
    """A node's hardware scores, in the units ``hardware score`` reports.

    ``None`` = not measured (a CPU-only node has no GPU metrics). Build one from a
    node's stored scores with :meth:`from_scores`.
    """

    cpu_fp32_gflops_1t: float | None = None
    cpu_fp32_gflops_all: float | None = None
    cpu_fp64_gflops_all: float | None = None
    mem_triad_gb_s: float | None = None
    gpu_fp32_tflops: float | None = None
    gpu_fp16_tflops: float | None = None
    gpu_mem_gb_s: float | None = None

    @classmethod
    def from_scores(cls, scores: Mapping[str, float | None]) -> HardwareHint:
        """From ``{metric name: value}``; unknown names are ignored."""
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in scores.items() if k in known})

    def per_second(self, metric: Metric) -> float:
        """``metric`` in SI units per second (FLOP/s, bytes/s).

        Raises :class:`Unavailable` when the node has no such score - a GPU
        family's hint on a node without a GPU is a typed outcome, not a guess.
        """
        value = getattr(self, metric)
        if value is None or value <= 0:
            raise Unavailable(f"the node has no {metric} score")
        return float(value) * _SI[metric]


CALIBRATION_HW: Final = HardwareHint(
    cpu_fp32_gflops_1t=131.8,
    cpu_fp32_gflops_all=481.0,
    cpu_fp64_gflops_all=262.3,
    mem_triad_gb_s=29.4,
)
"""Laptop 1 (i7-12650H, 16 threads, 16 GB; `meshforge-agent hardware score`,
2026-10-17), where the CPU and memory families' efficiencies were measured."""

MID_LAPTOP: Final = HardwareHint(
    cpu_fp32_gflops_1t=90.0,
    cpu_fp32_gflops_all=240.0,
    cpu_fp64_gflops_all=120.0,
    mem_triad_gb_s=20.0,
    gpu_fp32_tflops=5.0,
    gpu_fp16_tflops=12.0,
    gpu_mem_gb_s=180.0,
)
"""The reference "mid laptop" of the 5 s - 3 min window: about half of Laptop 1's
multi-core score, 70 % of its single-thread score, and an entry-level laptop GPU
(RTX 3050-class). An assumption until the team's other laptops are scored."""


def calibrated(units_per_s: float, metric: Metric) -> float:
    """Efficiency (work units per SI metric unit) from a rate measured on
    :data:`CALIBRATION_HW`."""
    return units_per_s / CALIBRATION_HW.per_second(metric)


class Params(BaseModel):
    """Base of every family's parameter model: frozen, no unknown keys."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ParamsError(MeshForgeError, ValueError):
    """Parameters a family refuses (out of bounds, unknown key, outside the
    5 s - 3 min window). ``problems`` lists ``"field: why"`` lines."""

    default_failure_type: ClassVar[FailureType] = FailureType.INVALID_INPUT

    def __init__(self, family: str, problems: list[str]) -> None:
        super().__init__(
            f"{family}: invalid parameters: " + "; ".join(problems),
            details={"family": family, "problems": problems},
        )
        self.problems = problems


class UnknownFamilyError(MeshForgeError, LookupError):
    """No registered family has this name."""

    default_failure_type: ClassVar[FailureType] = FailureType.INVALID_INPUT

    def __init__(self, name: str) -> None:
        super().__init__(
            f"unknown workload family {name!r}; `compute bench workloads` lists them",
            details={"family": name},
        )


class WorkloadResult(BaseModel):
    """One run's ``results/workload.json`` (see :mod:`.kernels.runner`)."""

    model_config = ConfigDict(extra="allow", frozen=True)

    schema_version: int
    family: str
    params: dict[str, Any]
    seed: int
    status: Literal["ok", "check_failed", "unavailable", "bad_params"]
    reason: str | None = None
    device: str | None = None
    compute_ns: int | None = Field(default=None, ge=0)
    compute_ms: float | None = Field(default=None, ge=0)
    prepare_ms: float | None = None
    warmup_ms: float | None = None
    check_ms: float | None = None
    checksum: str | None = None
    checksum_exact: bool | None = None
    verified: bool
    check_detail: str | None = None
    libraries: dict[str, str | None] = Field(default_factory=dict)
    platform: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class Verdict:
    """:meth:`Family.verify`: ``ok`` and, when not, every problem found."""

    ok: bool
    problems: tuple[str, ...] = ()


class Family[P: Params](ABC):
    """One workload family (see the module docstring)."""

    name: ClassVar[str]
    kind: ClassVar[Kind]
    description: ClassVar[str]
    params_model: ClassVar[type[Params]]
    size_params: ClassVar[tuple[str, ...]]
    needs_numpy: ClassVar[bool] = False

    # ------------------------------------------------------------ cost model

    @abstractmethod
    def work_units(self, params: P) -> float:
        """Work in the family's own units (FLOPs, bytes, elements x log n ...)."""

    @abstractmethod
    def metric(self, params: P) -> Metric:
        """The hardware score this family's speed follows."""

    @abstractmethod
    def efficiency(self, params: P) -> float:
        """Work units per second per SI unit of :meth:`metric`."""

    def ram_gb(self, params: P) -> float:
        """Host RAM the run needs (manifest ``resources.ram_gb``)."""
        del params
        return 0.5

    def vram_gb(self, params: P) -> float | None:
        """GPU memory the run needs (manifest ``resources.vram_gb``); None = no GPU."""
        del params
        return None

    @property
    def gpu(self) -> bool:
        return self.kind in (Kind.GPU, Kind.ML)

    @property
    def env_base(self) -> Literal["python-cpu", "cuda-pytorch"]:
        return "cuda-pytorch" if self.gpu else "python-cpu"

    def expected_runtime_s(
        self, params: P | Mapping[str, Any], hw: HardwareHint = MID_LAPTOP
    ) -> float:
        """Rough compute-phase seconds on ``hw`` (prepare/check not included).

        Raises :class:`Unavailable` when ``hw`` lacks the metric the family
        follows (no GPU score for a GPU family).
        """
        p = self._model(params)
        return self.work_units(p) / (hw.per_second(self.metric(p)) * self.efficiency(p))

    # ------------------------------------------------------------ parameters

    def _model(self, params: P | Mapping[str, Any]) -> P:
        if isinstance(params, self.params_model):
            return params
        return self._validate(params)

    def _validate(self, raw: object) -> P:
        try:
            return self.params_model.model_validate(raw)  # type: ignore[return-value]
        except ValidationError as exc:
            problems = [
                f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
                for err in exc.errors(include_url=False, include_input=False)
            ]
            raise ParamsError(self.name, problems) from None

    def parse_params(self, raw: P | Mapping[str, Any]) -> P:
        """Validated params inside the 5 s - 3 min window (else :class:`ParamsError`)."""
        params = self._model(raw)
        hint = self.expected_runtime_s(params, MID_LAPTOP)
        if not MIN_RUNTIME_S <= hint <= MAX_RUNTIME_S:
            raise ParamsError(
                self.name,
                [
                    f"expected about {hint:.1f} s on the reference mid laptop; "
                    f"a benchmark run must take {MIN_RUNTIME_S:g}-{MAX_RUNTIME_S:g} s "
                    f"(change {', '.join(self.size_params)})"
                ],
            )
        return params

    def check_seed(self, seed: object) -> int:
        """``seed`` as an int in ``0..MAX_SEED``; bool, float, str and None are
        refused (:class:`ParamsError`) - the same rule as the job side."""
        if (
            isinstance(seed, bool)
            or not isinstance(seed, int)
            or not 0 <= seed <= MAX_SEED
        ):
            raise ParamsError(self.name, [f"seed: must be an integer 0..{MAX_SEED}"])
        return seed

    def smallest_params(self) -> P:
        """The model's defaults: by convention the smallest valid run (about
        :data:`MIN_RUNTIME_S` on the mid laptop; the component test runs it)."""
        return self.parse_params({})

    def params_dict(self, params: P | Mapping[str, Any]) -> dict[str, Any]:
        """JSON-ready params, every default filled in (the run's identity)."""
        return self._model(params).model_dump(mode="json")

    # ------------------------------------------------------------------- run

    def run(
        self,
        params: P | Mapping[str, Any],
        seed: int,
        *,
        device: str | None = None,
    ) -> WorkloadResult:
        """One in-process run through the job-side runner.

        ``device`` overrides the family's device (``"cpu"`` lets a test run a GPU
        kernel without CUDA); jobs never set it. Raises :class:`Unavailable`.
        """
        self.check_seed(seed)
        document = execute(self.name, self.params_dict(params), seed, device=device)
        return WorkloadResult.model_validate(document)

    def verify(self, result: WorkloadResult) -> Verdict:
        """Self-check of one result: the right family and schema, a positive
        monotonic compute time, a checksum, and the run's own output check
        (Freivalds, analytic integral, round trip ...) passed."""
        problems: list[str] = []
        if result.schema_version != RESULT_SCHEMA_VERSION:
            problems.append(f"schema_version {result.schema_version}")
        if result.family != self.name:
            problems.append(f"family {result.family!r} is not {self.name!r}")
        try:
            self._model(result.params)
        except ParamsError as exc:
            problems.extend(exc.problems)
        if result.status != "ok" or not result.verified:
            problems.append(f"status {result.status}: {result.reason or ''}".strip())
        if not result.compute_ns:
            problems.append("no compute time")
        if not (result.checksum or "").startswith("sha256:"):
            problems.append("no checksum")
        return Verdict(ok=not problems, problems=tuple(problems))

    # --------------------------------------------------------------- display

    def describe(self) -> dict[str, Any]:
        """The ``compute bench workloads --json`` entry of this family."""
        smallest = self.smallest_params()
        return {
            "name": self.name,
            "kind": self.kind.value,
            "description": self.description,
            "env_base": self.env_base,
            "gpu": "required" if self.gpu else "none",
            "size_params": list(self.size_params),
            "params_schema": self.params_model.model_json_schema(),
            "smallest": self.params_dict(smallest),
            "smallest_hint_s": round(self.expected_runtime_s(smallest), 1),
            "runtime_window_s": [MIN_RUNTIME_S, MAX_RUNTIME_S],
        }


_REGISTRY: dict[str, Family[Any]] = {}


def register(family: Family[Any]) -> Family[Any]:
    """Add ``family`` to the registry (names are unique)."""
    if family.name in _REGISTRY:
        raise ValueError(f"workload family {family.name!r} is registered twice")
    _REGISTRY[family.name] = family
    return family


def get_family(name: str) -> Family[Any]:
    _load()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise UnknownFamilyError(name) from None


def list_families() -> list[Family[Any]]:
    """Every registered family, sorted by name."""
    _load()
    return [_REGISTRY[name] for name in sorted(_REGISTRY)]


def iter_names() -> Iterator[str]:
    _load()
    yield from sorted(_REGISTRY)


def _load() -> None:
    # The family modules register themselves on import (not at this module's
    # import: they import this one).
    from . import cpu, gpu, memory, ml  # noqa: F401


def round_up_gb(value: float) -> float:
    """Round a memory need up to the next 0.5 GB (at least 0.5)."""
    return max(0.5, math.ceil(value * 2) / 2)
