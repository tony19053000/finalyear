"""The benchmark workload library (V0.9 G2, roadmap 0.9.2, D214).

::

    from meshforge.bench.workloads import get_family, list_families, build_job

    family = get_family("cpu.matmul")
    params = family.smallest_params()  # validated, inside 5 s - 3 min
    family.expected_runtime_s(params)  # rough hint on the mid laptop
    result = family.run(params, seed=7)  # in-process, same code as a job
    family.verify(result).ok
    build_job("cpu.matmul", params, 7, dest)  # a normal job folder to submit

Layout: :mod:`.base` (contract, registry, hints), :mod:`.cpu` / :mod:`.memory`
/ :mod:`.gpu` / :mod:`.ml` (the families), :mod:`.kernels` (the job-side code
copied into job folders), :mod:`.materialise` (job folders). Reference:
``docs/BENCHMARKS.md`` "Workload families".
"""

from __future__ import annotations

from .base import (
    CALIBRATION_HW,
    MAX_RUNTIME_S,
    MID_LAPTOP,
    MIN_RUNTIME_S,
    Family,
    HardwareHint,
    Kind,
    Params,
    ParamsError,
    Unavailable,
    UnknownFamilyError,
    Verdict,
    WorkloadResult,
    get_family,
    list_families,
)
from .materialise import BuiltJob, MaterialiseError, build_job

__all__ = [
    "CALIBRATION_HW",
    "MAX_RUNTIME_S",
    "MID_LAPTOP",
    "MIN_RUNTIME_S",
    "BuiltJob",
    "Family",
    "HardwareHint",
    "Kind",
    "MaterialiseError",
    "Params",
    "ParamsError",
    "Unavailable",
    "UnknownFamilyError",
    "Verdict",
    "WorkloadResult",
    "build_job",
    "get_family",
    "list_families",
]
