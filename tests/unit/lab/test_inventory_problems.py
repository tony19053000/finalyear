"""The top-level ``problems`` list of lab/inventory.py (0.2.2 fix round 1)."""

from __future__ import annotations

from typing import Any

from _inventory.model import fail, ok
from _inventory.problems import find_problems
from _inventory.summary import render

NVIDIA_GPU = {"vendor": "NVIDIA", "name": "RTX 3050"}
BLOCKED = {
    "driver_working": False,
    "nvidia_smi": {"ok": False, "available": True, "error": "NVIDIA-SMI has failed\nx"},
    "nvml": {"ok": False, "error": "NVMLError_DriverNotLoaded"},
}


def healthy() -> dict[str, Any]:
    return {
        "gpus": ok("lspci", [NVIDIA_GPU]),
        "nvidia": ok("nvidia", {"driver_working": True, "nvidia_smi": {"ok": True}}),
        "torch": ok(
            "py", {"installed": True, "cuda_build": "12.4", "cuda_available": True}
        ),
        "docker": ok("docker", {"installed": True, "reachable": True}),
    }


def test_healthy_machine_has_no_problems() -> None:
    assert find_problems(healthy()) == []


def test_every_problem_kind_sorted_and_deterministic() -> None:
    s = healthy()
    s["nvidia"] = ok("nvidia", BLOCKED)
    s["torch"] = ok(
        "py",
        {
            "installed": True,
            "cuda_build": "12.4",
            "cuda_available": False,
            "cuda_error": "RuntimeError: no driver",
        },
    )
    s["docker"] = ok(
        "docker", {"installed": True, "reachable": False, "error": "cannot connect"}
    )
    s["uv"] = fail("uv", "uv: skipped: run deadline exceeded")
    problems = find_problems(s)
    assert problems == [
        "docker: installed but the daemon is not reachable (cannot connect)",
        "nvidia: NVIDIA GPU present but the driver is not working (NVIDIA-SMI has failed)",
        "torch: CUDA build 12.4 installed but torch.cuda.is_available() is False "
        "(RuntimeError: no driver)",
        "uv: probe failed (uv: skipped: run deadline exceeded)",
    ]
    assert find_problems(dict(reversed(list(s.items())))) == problems


def test_no_nvidia_hardware_is_not_a_problem() -> None:
    s = healthy()
    s["gpus"] = ok("lspci", [{"vendor": "Intel"}])
    s["nvidia"] = ok(
        "nvidia",
        {
            "driver_working": False,
            "nvidia_smi": {"ok": False, "available": False, "error": "not found"},
            "nvml": {"ok": False, "error": "not installed"},
        },
    )
    assert find_problems(s) == []


def test_installed_but_broken_nvidia_smi_without_listed_gpu() -> None:
    s = healthy()
    s["gpus"] = ok("lspci", [])
    s["nvidia"] = ok("nvidia", BLOCKED)
    assert find_problems(s) == [
        "nvidia: nvidia-smi is installed but not working (NVIDIA-SMI has failed)"
    ]


def test_cpu_only_torch_and_missing_docker_are_not_problems() -> None:
    s = healthy()
    s["torch"] = ok(
        "py", {"installed": True, "cuda_build": None, "cuda_available": False}
    )
    s["docker"] = ok("docker", {"installed": False, "reachable": False})
    assert find_problems(s) == []


def test_summary_prints_problems_or_none() -> None:
    doc: dict[str, Any] = {"sections": healthy(), "failed_sections": [], "problems": []}
    assert "problems     : none" in render(doc)
    doc["problems"] = ["docker: installed but the daemon is not reachable (x)"]
    text = render(doc)
    assert "problems     : 1 (need a human)" in text
    assert "  - docker: installed but the daemon is not reachable (x)" in text
