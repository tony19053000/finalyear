"""Probe result shape, schema version, and the LAB.md §1 field map.

Every section of the inventory is a *probe result*::

    {"ok": true, "source": "lscpu", "value": {...}}
    {"ok": false, "source": "nvidia-smi", "error": "NVIDIA-SMI has failed ..."}

so one failing probe never hides the others, and a failure is recorded as data.
"""

from __future__ import annotations

from typing import Any, Final

#: Bump when a field is renamed/removed or its meaning changes (additions are fine).
SCHEMA_VERSION: Final = 1

#: JSON-compatible value (kept loose on purpose: parsers return nested dicts/lists).
type JSON = Any
type Probe = dict[str, JSON]

#: Every section key the inventory always contains, in document order.
SECTIONS: Final = (
    "git",
    "os",
    "wsl",
    "cpu",
    "memory",
    "disks",
    "gpus",
    "nvidia",
    "secure_boot",
    "python",
    "uv",
    "torch",
    "docker",
    "network",
    "power",
    "tools",
)

#: docs/LAB.md §1 row -> JSON paths (``sections.<name>...``) that answer it. Rows
#: that no script can know (Owner, Role, IP reservation, AI-agent access,
#: Inventory JSON link) are deliberately absent.
LAB_FIELDS: Final[dict[str, tuple[str, ...]]] = {
    "Hostname": ("hostname",),
    "OS / build": ("sections.os",),
    "CPU (cores/threads)": ("sections.cpu",),
    "RAM": ("sections.memory",),
    "GPU / VRAM": ("sections.gpus",),
    "Driver / CUDA": ("sections.nvidia", "sections.gpus"),
    "nvidia-smi / torch.cuda.is_available()": ("sections.nvidia", "sections.torch"),
    "Docker (GPU in container?)": ("sections.docker",),
    "Python / uv": ("sections.python", "sections.uv"),
    "Other tooling": ("sections.tools",),
    "Network adapters (Wi-Fi / Ethernet, link speed)": ("sections.network",),
    "MAC (Ethernet / Wi-Fi)": ("sections.network",),
}


def ok(source: str, value: JSON) -> Probe:
    """A successful probe result."""
    return {"ok": True, "source": source, "value": value}


def fail(source: str, error: str) -> Probe:
    """A failed probe result; ``error`` is human-readable and already short."""
    return {"ok": False, "source": source, "error": error.strip() or "unknown error"}
