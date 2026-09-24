"""``fabric_check inventory``: every node's hardware as the controller knows it
(``GET /v1/nodes/{id}`` -> ``hardware``, exactly as the agent's ``Hello`` sent it).

This is the evidence for V0.3 success criterion 1 ("all 4 laptops appear with
hardware matching docs/LAB.md"). **It does not decide "matches".** docs/LAB.md §1
is partly provisional and a human compares the two; the tool only lays the facts
out side by side and points at what looks odd (two node ids with one hostname).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from _fabric.api import FabricApi, JsonObject

GIB = 1024**3


def collect(api: FabricApi) -> list[JsonObject]:
    """Details of every node, in ``node_id`` order (the API's order)."""
    return [api.node(str(node["node_id"])) for node in api.nodes()]


def _int(value: object) -> int | None:
    """64-bit proto values arrive as JSON strings (ARCHITECTURE §20)."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return None


def _gib(value: object) -> str:
    n = _int(value)
    return "?" if n is None else f"{n / GIB:.1f} GiB"


def gpu_text(hardware: dict[str, Any]) -> str:
    gpus = hardware.get("gpus")
    if not isinstance(gpus, list) or not gpus:
        return "none reported"
    parts = []
    for gpu in gpus:
        if not isinstance(gpu, dict):
            continue
        parts.append(
            f"#{gpu.get('index', 0)} {gpu.get('model') or '?'} {_gib(gpu.get('vram_bytes'))}"
            f" drv {gpu.get('driver_version') or '?'} cc {gpu.get('compute_capability') or '?'}"
        )
    return "; ".join(parts) or "none reported"


def row(detail: JsonObject) -> dict[str, str]:
    raw = detail.get("hardware")
    hw: dict[str, Any] = raw if isinstance(raw, dict) else {}
    caps = hw.get("capabilities")
    link = "connected" if detail.get("connected") else "not connected"
    cores, threads = hw.get("cpu_cores", "?"), hw.get("cpu_threads", "?")
    cpu = f"{hw.get('cpu_model') or '?'} ({cores}c/{threads}t)"
    return {
        "hostname": str(detail.get("hostname", "")),
        "node_id": str(detail.get("node_id", "")),
        "os": f"{detail.get('os', '')} / {detail.get('platform', '')}",
        "state": f"{detail.get('state', '')} / {link}",
        "agent": str(detail.get("agent_version") or "?"),
        "cpu": cpu,
        "ram": _gib(hw.get("ram_bytes")),
        "gpus": gpu_text(hw),
        "capabilities": ", ".join(str(c) for c in caps)
        if isinstance(caps, list) and caps
        else "-",
    }


COLUMNS = (
    "hostname",
    "node_id",
    "os",
    "state",
    "agent",
    "cpu",
    "ram",
    "gpus",
    "capabilities",
)


def notes(details: list[JsonObject]) -> list[str]:
    out: list[str] = []
    names = Counter(str(d.get("hostname", "")) for d in details)
    for name, count in sorted(names.items()):
        if count > 1:
            out.append(
                f"hostname {name!r} has {count} node ids - its identity was reset or "
                "re-created at some point (criterion 4 cares about this); the "
                "not-connected ones are history"
            )
    if not any(d.get("connected") for d in details):
        out.append("no node is connected right now - start the agents and run again")
    return out


def text_table(rows: list[dict[str, str]]) -> str:
    """A plain fixed-width table for the terminal (ASCII only: Windows consoles)."""
    widths = {c: max([len(c)] + [len(r[c]) for r in rows]) for c in COLUMNS}
    lines = ["  ".join(c.upper().ljust(widths[c]) for c in COLUMNS)]
    lines += ["  ".join(r[c].ljust(widths[c]) for c in COLUMNS) for r in rows]
    return "\n".join(lines)
