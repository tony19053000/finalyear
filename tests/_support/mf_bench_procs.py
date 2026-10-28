"""Shared helpers of the V0.9 G2 tester r1 real-process tests (``tests/component/bench``).

Split out of ``test_v09_g2_tester_r1_process.py`` (G3 item 0.2, D225.4): tiny
params per family, the subprocess runners and the job-folder helpers.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from meshforge.bench.workloads import build_job, list_families

FAMILIES = list_families()
CPU_NAMES = [f.name for f in FAMILIES if not f.gpu]
TORCH_NAMES = [f.name for f in FAMILIES if f.gpu]

TINY: dict[str, dict[str, Any]] = {
    "cpu.matmul": {"n": 64, "repeats": 2, "dtype": "float64"},
    "cpu.primes": {"limit": 100_000, "repeats": 1},
    "cpu.sort": {"n": 20_000, "repeats": 2},
    "cpu.zlib": {"size_mb": 1, "level": 6, "repeats": 1},
    "cpu.sha256": {"size_mb": 2, "chunk_kb": 64},
    "cpu.integrate": {"points_m": 1, "repeats": 1},
    "memory.copy": {"size_mb": 2, "repeats": 2},
    "memory.groupby": {"rows_m": 1, "groups": 50, "repeats": 1},
    "gpu.matmul": {"n": 64, "repeats": 1, "dtype": "float32"},
    "gpu.conv2d": {"batch": 2, "channels": 4, "size": 8, "repeats": 1},
    "gpu.elementwise": {"elements_m": 1, "ops": 3, "repeats": 1},
    "gpu.resize": {"batch": 1, "size": 8, "out_size": 16, "repeats": 1},
    "ml.cnn_infer": {"batch": 2, "width": 8, "depth": 2, "image": 32, "batches": 2},
    "ml.cnn_train": {"batch": 8, "width": 8, "depth": 2, "image": 32, "steps": 20},
    "ml.transformer_infer": {
        "batch": 2,
        "seq_len": 8,
        "width": 64,
        "depth": 1,
        "batches": 2,
    },
}
assert set(TINY) == {f.name for f in FAMILIES}


def _torch_available() -> bool:
    try:
        import torch  # noqa: F401
    except ImportError:
        return False
    return True


needs_torch = pytest.mark.skipif(
    not _torch_available(), reason="torch is not installed"
)

_NEEDS_NUMPY = {f.name for f in FAMILIES if f.needs_numpy}


def skip_without_numpy(*names: str) -> None:
    """Skip when one of ``names`` is a numpy family and numpy is missing (a
    fresh clone without the agent extra): its job reports ``unavailable``
    (exit 3), so nothing about the run can be judged here."""
    if _NEEDS_NUMPY.intersection(names):
        pytest.importorskip("numpy", reason="the numpy families need the agent extra")


def _env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONHASHSEED",)}
    env.update(extra)
    return env


def _py(
    code: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    timeout: int = 200,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=cwd,
        env=env if env is not None else _env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _tree(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _job_with_request(
    tmp: Path, family: str, request: Any, *, raw: str | None = None
) -> Path:
    built = build_job(family, {}, 1, tmp / "job")
    target = built.path / "workload_params.json"
    target.write_text(raw if raw is not None else json.dumps(request), encoding="utf-8")
    return built.path


def _entry(
    job: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "run_workload.py"],
        cwd=job,
        env=env or _env(),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _doc(job: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads((job / "results" / "workload.json").read_text())
    return doc
