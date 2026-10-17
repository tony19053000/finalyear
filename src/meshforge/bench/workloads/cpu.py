"""CPU workload families (V0.9 G2, D214): matmul, primes, sort, zlib, sha256,
integrate.

Efficiencies are measured on Laptop 1 (:data:`~.base.CALIBRATION_HW`,
2026-10-17) with the job-side kernels in :mod:`.kernels.cpu`; the numbers and
how they were taken are in ``docs/BENCHMARKS.md``.
"""

from __future__ import annotations

import math
from typing import ClassVar, Literal

from pydantic import Field

from .base import Family, Kind, Metric, Params, calibrated, register

__all__ = [
    "IntegrateFamily",
    "MatmulFamily",
    "PrimesFamily",
    "Sha256Family",
    "SortFamily",
    "ZlibFamily",
]

MIB = 1024 * 1024
_BASE_RAM_GB = 0.3
"""Interpreter + numpy, before the family's own arrays."""


class MatmulParams(Params):
    n: int = Field(2048, ge=256, le=4096, description="matrix side (n x n)")
    repeats: int = Field(22, ge=1, le=500, description="multiplications timed")
    dtype: Literal["float64", "float32"] = "float64"


class MatmulFamily(Family[MatmulParams]):
    name = "cpu.matmul"
    kind = Kind.CPU
    description = (
        "dense n x n matrix multiply (numpy / BLAS, all cores); Freivalds check"
    )
    params_model = MatmulParams
    size_params = ("n", "repeats")
    needs_numpy = True

    # BLAS reaches its peak only for large n: rate = peak * n / (n + N_HALF),
    # fitted to n = 512 / 2048 / 4096 on Laptop 1 (67 / 131 / 212 GFLOP/s fp64).
    _N_HALF: ClassVar = 1828.0
    _PEAK: ClassVar = {"float64": 306e9, "float32": 485e9}

    def work_units(self, params: MatmulParams) -> float:
        return 2.0 * params.n**3 * params.repeats

    def metric(self, params: MatmulParams) -> Metric:
        return (
            "cpu_fp64_gflops_all"
            if params.dtype == "float64"
            else "cpu_fp32_gflops_all"
        )

    def efficiency(self, params: MatmulParams) -> float:
        rate = self._PEAK[params.dtype] * params.n / (params.n + self._N_HALF)
        return calibrated(rate, self.metric(params))

    def ram_gb(self, params: MatmulParams) -> float:
        itemsize = 8 if params.dtype == "float64" else 4
        return _BASE_RAM_GB + 3.2 * params.n**2 * itemsize / 1e9


class PrimesParams(Params):
    limit: int = Field(
        100_000_000, ge=10_000_000, le=400_000_000, description="sieve up to"
    )
    repeats: int = Field(4, ge=1, le=100)


class PrimesFamily(Family[PrimesParams]):
    name = "cpu.primes"
    kind = Kind.CPU
    description = "sieve of Eratosthenes (pure Python bytearray, one core); pi(x) check"
    params_model = PrimesParams
    size_params = ("limit", "repeats")

    def work_units(self, params: PrimesParams) -> float:
        return float(params.limit) * params.repeats

    def metric(self, params: PrimesParams) -> Metric:
        return "cpu_fp32_gflops_1t"

    def efficiency(self, params: PrimesParams) -> float:
        return calibrated(1.15e8, "cpu_fp32_gflops_1t")  # numbers sieved per second

    def ram_gb(self, params: PrimesParams) -> float:
        return _BASE_RAM_GB + 1.1 * params.limit / 1e9


class SortParams(Params):
    n: int = Field(10_000_000, ge=1_000_000, le=50_000_000, description="int64 values")
    repeats: int = Field(18, ge=1, le=500)


class SortFamily(Family[SortParams]):
    name = "cpu.sort"
    kind = Kind.CPU
    description = "sort n random int64 values (numpy, one core); order + sum check"
    params_model = SortParams
    size_params = ("n", "repeats")
    needs_numpy = True

    def work_units(self, params: SortParams) -> float:
        return params.n * math.log2(params.n) * params.repeats

    def metric(self, params: SortParams) -> Metric:
        return "cpu_fp32_gflops_1t"

    def efficiency(self, params: SortParams) -> float:
        return calibrated(1.2e9, "cpu_fp32_gflops_1t")  # n log2 n units per second

    def ram_gb(self, params: SortParams) -> float:
        return _BASE_RAM_GB + 3 * 8 * params.n / 1e9


class ZlibParams(Params):
    size_mb: int = Field(64, ge=8, le=256, description="MiB of synthetic text")
    level: Literal[1, 6, 9] = 6
    repeats: int = Field(2, ge=1, le=100)


class ZlibFamily(Family[ZlibParams]):
    name = "cpu.zlib"
    kind = Kind.CPU
    description = "zlib-compress synthetic text (stdlib, one core); round-trip check"
    params_model = ZlibParams
    size_params = ("size_mb", "repeats")

    _RATE: ClassVar = {1: 60e6, 6: 37e6, 9: 35e6}
    """Input bytes per second on Laptop 1, per level."""

    def work_units(self, params: ZlibParams) -> float:
        return float(params.size_mb) * MIB * params.repeats

    def metric(self, params: ZlibParams) -> Metric:
        return "cpu_fp32_gflops_1t"

    def efficiency(self, params: ZlibParams) -> float:
        return calibrated(self._RATE[params.level], "cpu_fp32_gflops_1t")

    def ram_gb(self, params: ZlibParams) -> float:
        return _BASE_RAM_GB + 2.6 * params.size_mb * MIB / 1e9


class Sha256Params(Params):
    size_mb: int = Field(8192, ge=1024, le=400_000, description="MiB streamed")
    chunk_kb: Literal[4, 64, 1024] = 64


class Sha256Family(Family[Sha256Params]):
    name = "cpu.sha256"
    kind = Kind.CPU
    description = (
        "sha256 over a byte stream in chunks (hashlib, one core); re-chunk check"
    )
    params_model = Sha256Params
    size_params = ("size_mb",)

    def work_units(self, params: Sha256Params) -> float:
        return float(params.size_mb) * MIB

    def metric(self, params: Sha256Params) -> Metric:
        return "cpu_fp32_gflops_1t"

    def efficiency(self, params: Sha256Params) -> float:
        # Laptop 1 has SHA-NI (1.7 GB/s); a CPU without it is several times slower
        # than its FLOP score suggests - the hint is rough by design.
        return calibrated(1.7e9, "cpu_fp32_gflops_1t")


class IntegrateParams(Params):
    points_m: int = Field(
        70, ge=10, le=4000, description="millions of Simpson intervals"
    )
    repeats: int = Field(1, ge=1, le=50)


class IntegrateFamily(Family[IntegrateParams]):
    name = "cpu.integrate"
    kind = Kind.CPU
    description = (
        "composite Simpson integral (numpy, chunked, one core); analytic check"
    )
    params_model = IntegrateParams
    size_params = ("points_m", "repeats")
    needs_numpy = True

    def work_units(self, params: IntegrateParams) -> float:
        return params.points_m * 1e6 * params.repeats

    def metric(self, params: IntegrateParams) -> Metric:
        return "cpu_fp32_gflops_1t"

    def efficiency(self, params: IntegrateParams) -> float:
        return calibrated(1.85e7, "cpu_fp32_gflops_1t")  # intervals per second


for _family in (
    MatmulFamily(),
    PrimesFamily(),
    SortFamily(),
    ZlibFamily(),
    Sha256Family(),
    IntegrateFamily(),
):
    register(_family)
