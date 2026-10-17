"""V0.9 G2 (0.9.2 item 4): the smallest run of every family, for real, as a job.

Each case builds the family's real job folder (:func:`build_job`, smallest
params) and runs its real entrypoint ``python run_workload.py`` as a separate
process in that folder - exactly what an agent's subprocess runtime executes
(the interpreter is this test's, which has numpy, standing in for the job env's
pinned numpy). The run must exit 0 with a verified ``results/workload.json``.

GPU / ML families need CUDA: without it they are skipped with the reason, and
:func:`test_gpu_families_without_cuda_report_unavailable` checks the other half
of the contract - such a job exits 3 with ``status: unavailable``, never a crash.

The measured compute time is compared with the family's hint for the
calibration laptop only loosely (0.05x - 20x): a sanity bound on the cost
model that holds on any developer machine, not a benchmark claim.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from meshforge.bench.workloads import (
    CALIBRATION_HW,
    Family,
    WorkloadResult,
    build_job,
    list_families,
)
from meshforge.bench.workloads.kernels.runner import EXIT_UNAVAILABLE, RESULT_FILE

pytestmark = [pytest.mark.component, pytest.mark.timeout(240)]

CPU_FAMILIES = [f for f in list_families() if not f.gpu]
GPU_FAMILIES = [f for f in list_families() if f.gpu]


def _cuda() -> bool:
    try:
        import torch
    except ImportError:
        return False
    return bool(torch.cuda.is_available())


def _run_job(family: Family[Any], tmp_path: Path) -> tuple[int, str, WorkloadResult]:
    built = build_job(family.name, {}, 20261017, tmp_path / "job")
    done = subprocess.run(
        [sys.executable, *built.manifest.entrypoint[1:]],
        cwd=built.path,
        capture_output=True,
        text=True,
        timeout=200,
        check=False,
    )
    document = json.loads((built.path / RESULT_FILE).read_text(encoding="utf-8"))
    return (
        done.returncode,
        done.stdout + done.stderr,
        WorkloadResult.model_validate(document),
    )


@pytest.mark.parametrize("family", CPU_FAMILIES, ids=lambda f: f.name)
def test_smallest_cpu_run_as_a_job(
    family: Family[Any], tmp_path: Path, record_property: Any
) -> None:
    if family.needs_numpy:
        pytest.importorskip("numpy", reason="the job installs numpy; this env has none")
    code, output, result = _run_job(family, tmp_path)
    assert code == 0, output
    verdict = family.verify(result)
    assert verdict.ok, verdict.problems
    assert result.params == family.params_dict(family.smallest_params())
    assert result.device == "cpu"
    hint = family.expected_runtime_s(result.params, CALIBRATION_HW)
    measured = (result.compute_ns or 0) / 1e9
    record_property(f"{family.name}_compute_s", round(measured, 3))
    record_property(f"{family.name}_hint_s", round(hint, 3))
    assert 0.05 * hint <= measured <= 20 * hint, (measured, hint)


@pytest.mark.parametrize("family", GPU_FAMILIES, ids=lambda f: f.name)
def test_smallest_gpu_run_as_a_job(family: Family[Any], tmp_path: Path) -> None:
    if not _cuda():
        pytest.skip("needs a CUDA GPU with torch (none on this host)")
    code, output, result = _run_job(family, tmp_path)
    assert code == 0, output
    assert family.verify(result).ok
    assert result.device == "cuda"


@pytest.mark.parametrize("family", GPU_FAMILIES, ids=lambda f: f.name)
def test_gpu_families_without_cuda_report_unavailable(
    family: Family[Any], tmp_path: Path
) -> None:
    if _cuda():
        pytest.skip("this host has CUDA; the no-CUDA outcome cannot be observed")
    code, output, result = _run_job(family, tmp_path)
    assert code == EXIT_UNAVAILABLE, output
    assert "Traceback" not in output
    assert result.status == "unavailable"
    assert result.reason
    assert result.compute_ns is None
    assert not family.verify(result).ok
