"""V0.6 G2: planner inputs from stored rows (D105 slots, scores, D108 fingerprint)."""

from __future__ import annotations

from typing import Any

import pytest

from meshforge.common.manifest import parse_manifest_data
from meshforge.controller.planner.inputs import (
    effective_slots,
    node_snapshot,
    plan_request,
    scores_of,
    workload_fingerprint,
)

pytestmark = pytest.mark.unit

GIB = 1024**3
GPU = frozenset({"exec", "exec-gpu"})


@pytest.mark.parametrize(
    ("cpu", "gpu", "caps", "count", "expected"),
    [
        (None, None, frozenset({"exec"}), 0, (1, 0)),  # old agent, CPU only
        (None, None, GPU, 1, (1, 1)),  # old agent with exec-gpu
        (None, None, GPU, 0, (1, 0)),  # exec-gpu but no GPU listed
        (4, 2, GPU, 2, (4, 2)),  # what the Hello says
        (0, 0, GPU, 1, (0, 0)),  # owner allows nothing
        (2, 3, frozenset({"exec"}), 1, (2, 0)),  # no exec-gpu: no GPU slot
        (-1, -1, GPU, 1, (0, 0)),  # never negative
    ],
)
def test_effective_slots(
    cpu: int | None,
    gpu: int | None,
    caps: frozenset[str],
    count: int,
    expected: tuple[int, int],
) -> None:
    assert effective_slots(cpu, gpu, capabilities=caps, gpu_count=count) == expected


def _metric(name: str, mean: Any, status: str = "ok") -> dict[str, Any]:
    return {"name": name, "status": status, "mean": mean}


def test_scores_of_reads_ok_metrics_only() -> None:
    doc = {
        "metrics": [
            _metric("cpu_fp32_gflops_all", 120.5),
            _metric("gpu_fp32_tflops", 4.0),
            _metric("gpu_fp32_tflops", 9.0),
            _metric("gpu_fp32_tflops", 99.0, status="failed"),
            _metric("cpu_fp32_gflops_1t", 999.0),
        ]
    }
    assert scores_of(doc) == (120.5, 9.0)
    assert scores_of(None) == (None, None)
    assert scores_of({"metrics": "x"}) == (None, None)
    bad = {
        "metrics": [_metric("cpu_fp32_gflops_all", True), _metric("gpu_fp32_tflops", 0)]
    }
    assert scores_of(bad) == (None, None)


def test_node_snapshot_parses_hardware_json() -> None:
    snap = node_snapshot(
        "n1",
        state="online",
        connected=True,
        hardware={
            "capabilities": ["exec", "exec-gpu", 7],
            "gpus": [{"vram_bytes": str(4 * GIB)}, {"vram_bytes": "x"}, "junk"],
            "ram_bytes": str(16 * GIB),
            "cpu_cores": 4,
        },
        advertised_cpu_slots=2,
        advertised_gpu_slots=None,
    )
    assert snap.capabilities == frozenset({"exec", "exec-gpu"})
    assert snap.gpu_vram_bytes == (4 * GIB, 0)
    assert snap.ram_bytes == 16 * GIB and snap.cpu_threads == 4
    assert (snap.cpu_slots, snap.gpu_slots) == (2, 1)


def test_plan_request_rounds_requirements_up() -> None:
    manifest = parse_manifest_data(
        {
            "entrypoint": "python a.py",
            "resources": {"gpu": "required", "vram_gb": 4.01, "ram_gb": 0.1},
        }
    )
    req = plan_request(manifest, job_id="j", attempt_no=1, env_key="e")
    assert req.vram_bytes is not None and req.vram_bytes >= 4.01 * GIB
    assert req.ram_bytes is not None and req.ram_bytes >= 0.1 * GIB


def test_workload_fingerprint() -> None:
    a = parse_manifest_data({"entrypoint": "python a.py"})
    b = parse_manifest_data({"entrypoint": "python b.py"})
    tree = "sha256:" + "1" * 64
    fp = workload_fingerprint(a, project=tree, env_key="e")
    assert len(fp) == 64 and fp == workload_fingerprint(a, project=tree, env_key="e")
    assert fp != workload_fingerprint(b, project=tree, env_key="e")
    assert fp != workload_fingerprint(a, project="sha256:" + "2" * 64, env_key="e")
    assert fp != workload_fingerprint(a, project=tree, env_key="f")
    # the job name and priority are not part of the workload
    renamed = parse_manifest_data(
        {"name": "other", "entrypoint": "python a.py", "priority": 5}
    )
    assert workload_fingerprint(renamed, project=tree, env_key="e") == fp
