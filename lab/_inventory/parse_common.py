"""Pure parsers for cross-platform tools: nvidia-smi, docker, git, ``--version``."""

from __future__ import annotations

import csv
import io
import json
import re
from typing import Final

from _inventory.model import JSON

#: Fields requested from ``nvidia-smi --query-gpu`` (no serial/UUID: LAB.md §6).
NVIDIA_QUERY_FIELDS: Final = (
    "index",
    "name",
    "memory.total",
    "driver_version",
    "compute_cap",
    "pci.bus_id",
)
#: Fallback for old drivers that reject ``compute_cap``.
NVIDIA_QUERY_FIELDS_LEGACY: Final = (
    "index",
    "name",
    "memory.total",
    "driver_version",
    "pci.bus_id",
)

_CUDA_HEADER_RE: Final = re.compile(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)*)")
_VERSION_RE: Final = re.compile(r"(\d+\.\d+(?:\.\d+)*(?:[-+.][0-9A-Za-z.]+)?)")
_NA: Final = frozenset(
    {"", "[n/a]", "n/a", "[not supported]", "not supported", "[unknown error]"}
)


def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _clean(text: str) -> str | None:
    text = text.strip()
    return None if text.lower() in _NA else text


def parse_nvidia_smi_csv(text: str, fields: tuple[str, ...]) -> list[dict[str, JSON]]:
    """Parse ``--format=csv,noheader,nounits`` output for ``fields``.

    Raises ValueError when a row does not have one column per field (e.g. the
    driver-failure message instead of CSV).
    """
    gpus: list[dict[str, JSON]] = []
    for row in csv.reader(io.StringIO(text.strip())):
        if not row or all(not c.strip() for c in row):
            continue
        if len(row) != len(fields):
            raise ValueError(f"unexpected nvidia-smi output: {','.join(row)[:300]}")
        raw = {f: _clean(c) for f, c in zip(fields, row, strict=True)}
        mem = _num(raw.get("memory.total") or "")
        index = _num(raw.get("index") or "")
        gpus.append(
            {
                "index": int(index) if index is not None else None,
                "name": raw.get("name"),
                "vendor": "NVIDIA",
                "vram_mib": int(mem) if mem is not None else None,
                "driver_version": raw.get("driver_version"),
                "compute_capability": raw.get("compute_cap"),
                "pci_bus_id": raw.get("pci.bus_id"),
            }
        )
    return gpus


def parse_nvidia_smi_cuda_version(text: str) -> str | None:
    """CUDA driver API version from the plain ``nvidia-smi`` banner."""
    match = _CUDA_HEADER_RE.search(text)
    return match.group(1) if match else None


def parse_version(text: str) -> str | None:
    """First dotted version in ``--version`` output (``git version 2.43.0`` -> 2.43.0)."""
    match = _VERSION_RE.search(text)
    return match.group(1) if match else None


def parse_git_status(porcelain: str) -> dict[str, JSON]:
    """Counts from ``git status --porcelain`` (file names are not kept)."""
    lines = [ln for ln in porcelain.splitlines() if ln.strip()]
    untracked = sum(1 for ln in lines if ln.startswith("??"))
    modified = len(lines) - untracked
    return {
        "dirty": modified > 0,
        "modified_files": modified,
        "untracked_files": untracked,
    }


def _first_json_object(text: str) -> dict[str, JSON]:
    text = text.strip()
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in output")
    obj, _ = json.JSONDecoder().raw_decode(text[start:])
    if not isinstance(obj, dict):
        raise TypeError("JSON output is not an object")
    return obj


def parse_docker_version(text: str) -> dict[str, JSON]:
    """``docker version --format '{{json .}}'``; Server is null when unreachable."""
    data = _first_json_object(text)
    client = data.get("Client") or {}
    server = data.get("Server") or {}
    return {
        "client_version": client.get("Version"),
        "client_os": client.get("Os"),
        "server_version": server.get("Version") if server else None,
        "server_os": server.get("Os") if server else None,
        "server_platform": (server.get("Platform") or {}).get("Name")
        if server
        else None,
    }


def parse_docker_info(text: str) -> dict[str, JSON]:
    """Selected fields of ``docker info --format '{{json .}}'`` (no full dump)."""
    data = _first_json_object(text)
    runtimes = data.get("Runtimes") or {}
    names = sorted(runtimes) if isinstance(runtimes, dict) else []
    server_errors = data.get("ServerErrors") or []
    return {
        "server_version": data.get("ServerVersion"),
        "operating_system": data.get("OperatingSystem"),
        "os_type": data.get("OSType"),
        "kernel_version": data.get("KernelVersion"),
        "ncpu": data.get("NCPU"),
        "mem_total_bytes": data.get("MemTotal"),
        "runtimes": names,
        "default_runtime": data.get("DefaultRuntime"),
        "nvidia_runtime": "nvidia" in names,
        "server_errors": [str(e) for e in server_errors],
    }
