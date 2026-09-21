"""Rendering of :class:`HardwareFacts` for ``meshforge-agent hardware show`` (D13).

The rich facts - per-probe status, duration and reason - are **agent-local**: the
controller only ever receives the clipped ``Hello.hardware`` summary, so the only
honest place to inspect them is the laptop they describe. This module turns one
``discover()`` pass into either plain text lines or a JSON-safe mapping; it does no
I/O, contacts nothing, and is therefore fully unit-testable from fabricated facts.

Nothing here prints a filesystem path except a *mount point* (``/``, ``C:\\``),
which is the value being reported; probe reasons are path-free at construction
(:func:`meshforge.agent.hardware.probe.sanitize_reason`).
"""

from __future__ import annotations

from typing import Any, Final

from meshforge.agent.hardware.summary import HardwareFacts

__all__ = ["facts_lines", "facts_payload", "format_bytes"]

_UNITS: Final = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def format_bytes(value: int) -> str:
    """``17179869184`` -> ``"16.0 GiB"``; ``0``/negative -> ``"-"``."""
    if value <= 0:
        return "-"
    size = float(value)
    for unit in _UNITS:
        if size < 1024.0 or unit == _UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    raise AssertionError("unreachable")  # pragma: no cover


def facts_payload(facts: HardwareFacts) -> dict[str, Any]:
    """JSON-safe mapping of everything one discovery pass learned."""
    return {
        "discovered_at": facts.discovered_at.isoformat(),
        "duration_s": round(facts.duration_s, 3),
        "cpu": {
            "model": facts.cpu.model,
            "physical_cores": facts.cpu.physical_cores,
            "logical_threads": facts.cpu.logical_threads,
            "ram_bytes": facts.cpu.ram_bytes,
            "source": facts.cpu.source,
        },
        "gpus": [
            {
                "index": gpu.index,
                "model": gpu.model,
                "vram_bytes": gpu.vram_bytes,
                "driver_version": gpu.driver_version,
                "compute_capability": gpu.compute_capability,
                "source": gpu.source,
            }
            for gpu in facts.gpus
        ],
        "disks": [
            {
                "mount": disk.mount,
                "fstype": disk.fstype,
                "total_bytes": disk.total_bytes,
                "free_bytes": disk.free_bytes,
            }
            for disk in facts.disks
        ],
        "capabilities": list(facts.capabilities),
        "probes": [
            {
                "name": probe.name,
                "status": probe.status.value,
                "duration_s": round(probe.duration_s, 3),
                "reason": probe.reason,
            }
            for probe in facts.probes
        ],
        "degraded": list(facts.degraded),
    }


def facts_lines(facts: HardwareFacts) -> list[str]:
    """Human-readable rendering: one list of lines, no colour, no box drawing."""
    cpu = facts.cpu
    lines = [
        f"discovered_at: {facts.discovered_at.isoformat()}",
        f"duration_s: {facts.duration_s:.3f}",
        "",
        "cpu:",
        f"  model: {cpu.model or '(unknown)'}",
        f"  cores: {cpu.physical_cores} physical / {cpu.logical_threads} logical",
        f"  ram: {format_bytes(cpu.ram_bytes)}",
        f"  source: {cpu.source}",
        "",
    ]
    if facts.gpus:
        lines.append("gpus:")
        lines.extend(
            f"  [{gpu.index}] {gpu.model or '(unknown)'}  vram "
            f"{format_bytes(gpu.vram_bytes)}  driver {gpu.driver_version or '-'}  "
            f"compute {gpu.compute_capability or '-'}  ({gpu.source})"
            for gpu in facts.gpus
        )
    else:
        lines.append("gpus: none detected")
    lines.append("")
    if facts.disks:
        lines.append("disks:")
        lines.extend(
            f"  {disk.mount}  free {format_bytes(disk.free_bytes)} of "
            f"{format_bytes(disk.total_bytes)}"
            + (f"  ({disk.fstype})" if disk.fstype else "")
            for disk in facts.disks
        )
    else:
        lines.append("disks: none detected")
    lines.append("")
    lines.append(
        "capabilities: "
        + (", ".join(facts.capabilities) if facts.capabilities else "-")
    )
    lines.append("")
    lines.append("probes:")
    width = max((len(p.name) for p in facts.probes), default=0)
    for probe in facts.probes:
        reason = f"  {probe.reason}" if probe.reason else ""
        lines.append(
            f"  {probe.name.ljust(width)}  {probe.status.value:<11} "
            f"{probe.duration_s:6.3f} s{reason}"
        )
    if facts.degraded:
        lines.append("")
        lines.append("degraded:")
        lines.extend(f"  {entry}" for entry in facts.degraded)
    return lines
