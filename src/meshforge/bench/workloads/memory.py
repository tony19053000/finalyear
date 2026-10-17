"""Memory workload families (V0.9 G2, D214): array copy, numpy groupby.

Efficiencies measured on Laptop 1 (:data:`~.base.CALIBRATION_HW`); see
``docs/BENCHMARKS.md``.
"""

from __future__ import annotations

import math

from pydantic import Field

from .base import Family, Kind, Metric, Params, calibrated, register

__all__ = ["CopyFamily", "GroupbyFamily"]

MIB = 1024 * 1024
_BASE_RAM_GB = 0.3


class CopyParams(Params):
    size_mb: int = Field(1024, ge=64, le=1024, description="MiB per array")
    repeats: int = Field(60, ge=1, le=5000, description="full-array copies timed")


class CopyFamily(Family[CopyParams]):
    name = "memory.copy"
    kind = Kind.MEMORY
    description = "copy one int64 array into another (numpy.copyto); equality check"
    params_model = CopyParams
    size_params = ("size_mb", "repeats")
    needs_numpy = True

    def work_units(self, params: CopyParams) -> float:
        return float(params.size_mb) * MIB * params.repeats  # bytes copied

    def metric(self, params: CopyParams) -> Metric:
        return "mem_triad_gb_s"

    def efficiency(self, params: CopyParams) -> float:
        return calibrated(15e9, "mem_triad_gb_s")  # bytes copied per second

    def ram_gb(self, params: CopyParams) -> float:
        return _BASE_RAM_GB + 2.1 * params.size_mb * MIB / 1e9


class GroupbyParams(Params):
    rows_m: int = Field(10, ge=1, le=40, description="millions of rows")
    groups: int = Field(10_000, ge=10, le=1_000_000, description="distinct keys")
    repeats: int = Field(4, ge=1, le=100)


class GroupbyFamily(Family[GroupbyParams]):
    name = "memory.groupby"
    kind = Kind.MEMORY
    description = "sort-based group-by: count/sum/max per key (numpy, pandas-free); bincount check"
    params_model = GroupbyParams
    size_params = ("rows_m", "groups", "repeats")
    needs_numpy = True

    def work_units(self, params: GroupbyParams) -> float:
        rows = params.rows_m * 1e6
        return rows * math.log2(rows) * params.repeats

    def metric(self, params: GroupbyParams) -> Metric:
        # argsort + gathers on one core: bounded by the core as much as by memory.
        return "cpu_fp32_gflops_1t"

    def efficiency(self, params: GroupbyParams) -> float:
        # More distinct keys sort slower (Laptop 1: 3.5e8 at 10 keys, 1.8e8 at
        # 10^4, 1.5e8 at 10^6 rows-log-rows per second).
        rate = 3.9e8 / (1.0 + 0.3 * math.log10(params.groups))
        return calibrated(rate, "cpu_fp32_gflops_1t")

    def ram_gb(self, params: GroupbyParams) -> float:
        return _BASE_RAM_GB + 8 * 8 * params.rows_m * 1e6 / 1e9


for _family in (CopyFamily(), GroupbyFamily()):
    register(_family)
