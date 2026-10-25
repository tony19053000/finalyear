"""Host facts recorded in ``extra.facts`` so a result is explainable: OS and
WSL status (reused from ``_s2.env``), CPU count, RAM, ``uv`` version, whether
the host has an NVIDIA GPU (``nvidia-smi -L``), and free disk where images and
temp files go (a failed 4 GB pull must be explainable from the JSON).
"""

from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path
from typing import Any, Final

from _inventory import probes
from spikes._harness.envelope import sanitize_text
from spikes._s2.env import wsl_status_here
from spikes._s6 import proc

type JSON = Any

_SMI_TIMEOUT_S: Final = 20.0
FIX_NO_GPU: Final = (
    "no NVIDIA GPU visible to the host driver: GPU-in-container is n/a here "
    "(if the laptop has one, install the NVIDIA driver and check nvidia-smi)"
)


def memory_total_bytes() -> int | None:
    """Total RAM via the inventory probe (psutil, /proc/meminfo, or CIM).

    ``None`` when no source answers: the Windows CIM path raises when
    PowerShell is missing or fails, and a host fact must never end the run.
    """
    try:
        memory = probes.probe_memory()
    except (OSError, RuntimeError):
        return None  # the fact is unknown; the envelope records None
    if not memory.get("ok"):
        return None
    value = memory.get("value") or {}
    total = value.get("total_bytes") if isinstance(value, dict) else None
    return int(total) if isinstance(total, int | float) and total > 0 else None


def disk_free_bytes(path: Path | str | None) -> int | None:
    if path is None:
        return None
    try:
        return int(shutil.disk_usage(path).free)
    except (OSError, ValueError):
        return None


def parse_smi_list(text: str) -> list[str]:
    """``nvidia-smi -L`` lines -> GPU names (``GPU 0: NAME (UUID: ...)``)."""
    names: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("GPU "):
            continue
        _, _, rest = line.partition(":")
        name = rest.split("(UUID")[0].strip()
        if name:
            names.append(sanitize_text(name))
    return names


def host_gpu() -> dict[str, JSON]:
    """``{"present": bool, "devices": [...]}``; typed failure when absent."""
    smi = shutil.which("nvidia-smi")
    if smi is None:
        return {
            "present": False,
            "devices": [],
            "failure_type": "no_gpu_on_host",
            "message": "nvidia-smi not found on PATH",
            "fix": FIX_NO_GPU,
        }
    out = proc.run([smi, "-L"], timeout=_SMI_TIMEOUT_S)
    if not out.ok:
        return {
            "present": False,
            "devices": [],
            "failure_type": "no_gpu_on_host",
            "message": f"nvidia-smi -L failed: {sanitize_text(out.describe()[:300])}",
            "fix": FIX_NO_GPU,
        }
    devices = parse_smi_list(out.stdout)
    if not devices:
        return {
            "present": False,
            "devices": [],
            "failure_type": "no_gpu_on_host",
            "message": "nvidia-smi -L listed no GPU",
            "fix": FIX_NO_GPU,
        }
    return {"present": True, "devices": devices}


def host_facts(
    *, temp_dir: Path, docker_root: str | None, uv_version: str | None
) -> dict[str, JSON]:
    wsl = wsl_status_here()
    return {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "wsl": wsl,
        "is_wsl": bool(wsl.get("is_wsl")),
        "cpu_count": os.cpu_count(),
        "memory_total_bytes": memory_total_bytes(),
        "uv_version": uv_version,
        "gpu": host_gpu(),
        "disk_free_bytes": {
            "temp_dir": disk_free_bytes(temp_dir),
            "docker_root": disk_free_bytes(docker_root)
            if docker_root and Path(docker_root).exists()
            else None,
        },
    }
