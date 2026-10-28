# ruff: noqa: E501  (long xfail reasons / parametrize ids are clearer on one line)
"""V0.9 G2 tester round 1 (split from ``..._process.py``, G3 item 0.2): every exit
code of ``run_workload.py`` (0 / 2 / 3 / 4) in a real process, and the cost model
against a real run on this host.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from mf_bench_procs import (  # tests/_support, shared by the split files
    CPU_NAMES,
    TINY,
    TORCH_NAMES,
    _doc,
    _entry,
    _env,
    _job_with_request,
    _torch_available,
    needs_torch,
    skip_without_numpy,
)

from meshforge.bench.workloads import (
    CALIBRATION_HW,
    build_job,
    get_family,
)
from meshforge.bench.workloads.kernels import runner

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]


# ------------------------------------------------- exit codes, real processes


def test_exit_0_for_a_verified_run(tmp_path: Path) -> None:
    skip_without_numpy("cpu.sort")
    job = _job_with_request(
        tmp_path,
        "cpu.sort",
        {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": 1},
    )
    done = _entry(job)
    assert done.returncode == 0, done.stderr
    assert "benchmark: ok" in done.stdout


@pytest.mark.parametrize(
    ("label", "request_doc", "raw"),
    [
        ("file is not json", None, "{nope"),
        ("json array", None, "[]"),
        ("empty file", None, ""),
        ("unknown family", {"family": "nope.nope", "params": {}, "seed": 1}, None),
        ("family not a string", {"family": 7, "params": {}, "seed": 1}, None),
        (
            "params not an object",
            {"family": "cpu.sort", "params": [1], "seed": 1},
            None,
        ),
        (
            "missing param",
            {"family": "cpu.sort", "params": {"repeats": 1}, "seed": 1},
            None,
        ),
        (
            "bool for int",
            {"family": "cpu.sort", "params": {"n": True, "repeats": 1}, "seed": 1},
            None,
        ),
        (
            "str for int",
            {"family": "cpu.sort", "params": {"n": "9", "repeats": 1}, "seed": 1},
            None,
        ),
        (
            "float for int",
            {"family": "cpu.sort", "params": {"n": 9.5, "repeats": 1}, "seed": 1},
            None,
        ),
        (
            "bad literal",
            {
                "family": "cpu.matmul",
                "params": {"n": 8, "repeats": 1, "dtype": "bf16"},
                "seed": 1,
            },
            None,
        ),
        (
            "seed is float",
            {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": 1.5},
            None,
        ),
        (
            "seed is bool",
            {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": True},
            None,
        ),
        ("seed missing", {"family": "cpu.sort", "params": TINY["cpu.sort"]}, None),
        (
            "sha chunk not dividing 1 MiB",
            {
                "family": "cpu.sha256",
                "params": {"size_mb": 1, "chunk_kb": 3},
                "seed": 1,
            },
            None,
        ),
    ],
)
def test_exit_2_and_a_bad_params_document_for_a_damaged_request(
    label: str, request_doc: Any, raw: str | None, tmp_path: Path
) -> None:
    if label == "missing param":
        # an absent key is the kernel's to report (runner.check_params), and
        # the cpu.sort kernel needs numpy to get that far
        skip_without_numpy("cpu.sort")
    job = _job_with_request(tmp_path, "cpu.sort", request_doc, raw=raw)
    done = _entry(job)
    assert done.returncode == runner.EXIT_BAD_PARAMS, (label, done.stderr)
    doc = _doc(job)
    assert doc["status"] == "bad_params" and doc["verified"] is False
    assert doc["compute_ns"] is None and doc["reason"]
    assert "Traceback" not in done.stderr, label


@pytest.mark.parametrize(
    "request_doc",
    [
        {"family": "cpu.sort", "params": {"n": 100, "repeats": 1}, "seed": -1},
        {"family": "cpu.sort", "params": {"n": -5, "repeats": 1}, "seed": 1},
        {
            "family": "cpu.matmul",
            "params": {"n": -3, "repeats": 1, "dtype": "float64"},
            "seed": 1,
        },
    ],
    ids=["negative seed", "negative n sort", "negative n matmul"],
)
def test_exit_2_for_out_of_range_numbers_in_a_damaged_request(
    request_doc: Any, tmp_path: Path
) -> None:
    job = _job_with_request(tmp_path, request_doc["family"], request_doc)
    done = _entry(job)
    assert done.returncode == runner.EXIT_BAD_PARAMS, done.stderr
    assert _doc(job)["status"] == "bad_params"


def test_exit_3_unavailable_when_numpy_is_missing(tmp_path: Path) -> None:
    job = _job_with_request(
        tmp_path,
        "cpu.sort",
        {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": 1},
    )
    blocker = tmp_path / "hooks2"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\nsys.modules['numpy'] = None\n"
    )
    done = _entry(job, _env(PYTHONPATH=str(blocker)))
    assert done.returncode == runner.EXIT_UNAVAILABLE, done.stderr
    doc = _doc(job)
    assert doc["status"] == "unavailable" and "numpy" in doc["reason"]
    assert doc["verified"] is False and doc["compute_ns"] is None


@needs_torch
@pytest.mark.skipif(
    _torch_available() and __import__("torch").cuda.is_available(),
    reason="this host has CUDA: the no-GPU contract cannot be observed here",
)
@pytest.mark.parametrize("name", TORCH_NAMES)
def test_exit_3_unavailable_for_every_gpu_family_on_a_host_without_cuda(
    name: str, tmp_path: Path
) -> None:
    job = _job_with_request(
        tmp_path, name, {"family": name, "params": TINY[name], "seed": 1}
    )
    done = _entry(job)
    assert done.returncode == runner.EXIT_UNAVAILABLE, done.stderr
    doc = _doc(job)
    assert doc["status"] == "unavailable" and "CUDA" in doc["reason"]
    assert doc["compute_ns"] is None and doc["verified"] is False
    assert "Traceback" not in done.stderr
    family = get_family(name)
    from meshforge.bench.workloads import WorkloadResult

    assert not family.verify(WorkloadResult.model_validate(doc)).ok


def test_exit_3_when_torch_itself_is_missing(tmp_path: Path) -> None:
    name = "gpu.matmul"
    job = _job_with_request(
        tmp_path, name, {"family": name, "params": TINY[name], "seed": 1}
    )
    blocker = tmp_path / "hooks3"
    blocker.mkdir()
    (blocker / "sitecustomize.py").write_text(
        "import sys\nsys.modules['torch'] = None\n"
    )
    done = _entry(job, _env(PYTHONPATH=str(blocker)))
    assert done.returncode == runner.EXIT_UNAVAILABLE, done.stderr
    assert "torch" in _doc(job)["reason"]


@pytest.mark.parametrize(
    "tamper",
    [
        "KERNELS['cpu.sort'] = Kernel(_sort_prepare, lambda s: s['values'], _sort_check, _sort_warmup)",
        "KERNELS['cpu.sort'] = Kernel(_sort_prepare, lambda s: s['np'].sort(s['values'])[::-1].copy(), _sort_check, _sort_warmup)",
    ],
    ids=["unsorted", "reversed"],
)
def test_exit_4_and_a_check_failed_document_when_the_kernel_output_is_wrong(
    tamper: str, tmp_path: Path
) -> None:
    skip_without_numpy("cpu.sort")
    job = _job_with_request(
        tmp_path,
        "cpu.sort",
        {"family": "cpu.sort", "params": TINY["cpu.sort"], "seed": 1},
    )
    kernel_file = job / "bench_kernels" / "cpu.py"
    kernel_file.write_text(kernel_file.read_text() + "\n" + tamper + "\n")
    done = _entry(job)
    assert done.returncode == runner.EXIT_CHECK_FAILED, done.stderr
    doc = _doc(job)
    assert doc["status"] == "check_failed" and doc["verified"] is False
    assert (
        doc["reason"] and doc["compute_ns"] > 0
    )  # time is still reported, flagged unverified
    from meshforge.bench.workloads import WorkloadResult

    assert not get_family("cpu.sort").verify(WorkloadResult.model_validate(doc)).ok


@pytest.mark.parametrize("name", CPU_NAMES)
def test_every_cpu_job_survives_an_unwritable_results_dir_loudly(
    name: str, tmp_path: Path
) -> None:
    job = _job_with_request(
        tmp_path, name, {"family": name, "params": TINY[name], "seed": 1}
    )
    (job / "results").write_text("a file where the folder must go")
    done = _entry(job)
    assert done.returncode not in (0, 2, 3, 4), (
        "a lost result must not look like a clean outcome"
    )


# --------------------------------------------------- cost model vs reality


@pytest.mark.parametrize("name", list(CPU_NAMES))
def test_default_run_time_on_this_host_is_near_the_calibration_hint(
    name: str, tmp_path: Path
) -> None:
    """The hints were calibrated on Laptop 1. This host may be a different, busier
    machine, so the band is wide (0.3x - 3x); it still catches an efficiency that
    is off by an order of magnitude, which would silently move runs out of 5 s - 3 min."""
    skip_without_numpy(name)
    family = get_family(name)
    built = build_job(name, {}, 9, tmp_path / "job")
    done = _entry(built.path)
    assert done.returncode == 0, done.stderr
    measured = _doc(built.path)["compute_ms"] / 1000.0
    hint = family.expected_runtime_s(family.smallest_params(), CALIBRATION_HW)
    assert 0.3 * hint <= measured <= 3.0 * hint, (name, measured, hint)
    # prepare/check are real work not counted in compute: the job takes longer
    doc = _doc(built.path)
    assert doc["prepare_ms"] >= 0 and doc["check_ms"] >= 0
    digest = hashlib.sha256(
        (built.path / "results" / "workload.json").read_bytes()
    ).hexdigest()
    assert digest  # the file is readable, complete JSON (parsed above)
