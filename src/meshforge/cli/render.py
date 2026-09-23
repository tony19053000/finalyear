"""Terminal rendering for ``compute`` (roadmap 0.3.12).

Pure functions from an API payload to a Rich renderable, so every table in the
unit tests is built from a canned payload with no network and no terminal.

**Defensive by design.** The payload comes from
:mod:`meshforge.controller.api.models`, which the CLI may not import (D15), so
nothing here assumes a field is present or has the expected type: a missing or
null value renders as ``-`` and a field this build has never heard of is ignored
(it is still visible in ``--json``). That way an older ``compute`` against a newer
controller degrades instead of crashing.

**Portability.** Tables use the ASCII box style, not Unicode box-drawing, so the
output is identical in Windows Terminal, a legacy ``cmd.exe`` code page and a
pipe. Colour is Rich's own TTY detection (a redirected stdout gets none).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rich import box
from rich.table import Table

__all__ = [
    "NODE_COLUMNS",
    "format_age",
    "format_bytes",
    "node_detail_table",
    "nodes_table",
    "state_counts",
    "status_table",
]

NODE_COLUMNS = ("NODE", "HOSTNAME", "STATE", "CONN", "RTT", "CPU%", "RAM", "GPU", "AGE")

DASH = "-"
"""What a null, missing or unusable value renders as (ASCII, one character)."""

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _num(value: object) -> float | None:
    """A finite number or ``None`` - a JSON string or null never raises here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None  # NaN


def _text(value: object) -> str:
    return value if isinstance(value, str) and value else DASH


def format_bytes(value: object) -> str:
    """``2147483648`` -> ``"2.0 GiB"``; anything unusable -> ``"-"``."""
    number = _num(value)
    if number is None or number < 0:
        return DASH
    size = float(number)
    for unit in _UNITS:
        if size < 1024 or unit == _UNITS[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return DASH  # pragma: no cover - the loop always returns


def format_age(value: object) -> str:
    """Seconds -> ``"2.4s"`` / ``"3m"`` / ``"4h"``; null -> ``"-"`` (no sample yet)."""
    seconds = _num(value)
    if seconds is None or seconds < 0:
        return DASH
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.0f}h"


def _percent(value: object) -> str:
    number = _num(value)
    return DASH if number is None else f"{number:.0f}"


def _rtt(value: object) -> str:
    number = _num(value)
    return DASH if number is None else f"{number:.1f}"


def _ram(node: Mapping[str, Any]) -> str:
    used, total = node.get("ram_used_bytes"), node.get("ram_total_bytes")
    if _num(used) is None or _num(total) is None:
        return DASH
    return f"{format_bytes(used)}/{format_bytes(total)}"


def _gpus(node: Mapping[str, Any]) -> str:
    """``"0:50% 1:60%"`` - utilisation per index; ``"-"`` on a node with no GPU."""
    gpus = node.get("gpus")
    if not isinstance(gpus, Sequence) or isinstance(gpus, (str, bytes)) or not gpus:
        return DASH
    parts: list[str] = []
    for entry in gpus:
        if not isinstance(entry, Mapping):
            continue
        index = _num(entry.get("index"))
        util = _num(entry.get("util_percent"))
        label = DASH if index is None else f"{index:.0f}"
        parts.append(f"{label}:{DASH}" if util is None else f"{label}:{util:.0f}%")
    return " ".join(parts) if parts else DASH


def _conn(node: Mapping[str, Any]) -> str:
    """D16: ``connected`` is its own field and may disagree with ``state``."""
    value = node.get("connected")
    return "yes" if value is True else "no" if value is False else DASH


def _table(*columns: str, title: str | None = None) -> Table:
    table = Table(title=title, box=box.ASCII, header_style="bold", pad_edge=False)
    for column in columns:
        table.add_column(column, overflow="fold", no_wrap=False)
    return table


def nodes_table(payload: Mapping[str, Any]) -> Table:
    """``GET /v1/nodes`` as the operator table."""
    nodes = payload.get("nodes")
    rows = (
        [node for node in nodes if isinstance(node, Mapping)]
        if isinstance(nodes, Sequence) and not isinstance(nodes, (str, bytes))
        else []
    )
    table = _table(*NODE_COLUMNS, title=f"nodes ({len(rows)})")
    for node in rows:
        table.add_row(
            _text(node.get("node_id")),
            _text(node.get("hostname")),
            _text(node.get("state")),
            _conn(node),
            _rtt(node.get("rtt_ms")),
            _percent(node.get("cpu_percent")),
            _ram(node),
            _gpus(node),
            format_age(node.get("sample_age_s")),
        )
    return table


_DETAIL_FIELDS: tuple[tuple[str, str], ...] = (
    ("node_id", "node_id"),
    ("hostname", "hostname"),
    ("os", "os"),
    ("platform", "platform"),
    ("state", "state (database, §12.3)"),
    ("connected", "connected (live stream now)"),
    ("sample_age_s", "sample age"),
    ("last_seen_at", "last seen"),
    ("first_seen_at", "first seen"),
    ("session_epoch", "session epoch"),
    ("last_session_id", "last session"),
    ("agent_version", "agent version"),
    ("protocol_version", "protocol version"),
    ("peer_address", "peer address"),
    ("rtt_ms", "rtt (ms)"),
    ("rtt_ewma_ms", "rtt ewma (ms)"),
)


def node_detail_table(payload: Mapping[str, Any]) -> Table:
    """``GET /v1/nodes/{id}`` as a two-column field list plus the hardware summary."""
    table = _table("FIELD", "VALUE", title="node")
    for key, label in _DETAIL_FIELDS:
        value = payload.get(key)
        if key == "connected":
            rendered = _conn(payload)
        elif key == "sample_age_s":
            rendered = format_age(value)
        elif key in {"rtt_ms", "rtt_ewma_ms"}:
            rendered = _rtt(value)
        else:
            rendered = DASH if value is None else str(value)
        table.add_row(label, rendered)
    hardware = payload.get("hardware")
    if isinstance(hardware, Mapping):
        for key in sorted(hardware):
            table.add_row(f"hardware.{key}", str(hardware[key]))
    live = payload.get("cpu_percent"), payload.get("ram_used_bytes")
    if _num(live[0]) is not None or _num(live[1]) is not None:
        table.add_row("cpu%", _percent(payload.get("cpu_percent")))
        table.add_row("ram", _ram(payload))
        table.add_row("gpu", _gpus(payload))
    return table


def state_counts(payload: Mapping[str, Any]) -> dict[str, int]:
    """How many nodes are in each ``state``, unknown values included as given."""
    nodes = payload.get("nodes")
    counts: dict[str, int] = {}
    if not isinstance(nodes, Sequence) or isinstance(nodes, (str, bytes)):
        return counts
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        key = node.get("state")
        counts[key if isinstance(key, str) else "unknown"] = (
            counts.get(key if isinstance(key, str) else "unknown", 0) + 1
        )
    return dict(sorted(counts.items()))


def status_table(
    *,
    controller_url: str,
    reachable: bool,
    detail: str | None = None,
    nodes: Mapping[str, Any] | None = None,
    sessions: object = None,
) -> Table:
    """``compute status``: where we looked, whether it answered, and what it said."""
    table = _table("FIELD", "VALUE", title="fabric status")
    table.add_row("controller", controller_url)
    table.add_row("reachable", "yes" if reachable else "no")
    if detail:
        table.add_row("detail", detail)
    if nodes is not None:
        total = nodes.get("count")
        table.add_row("nodes", DASH if _num(total) is None else str(total))
        for state, count in state_counts(nodes).items():
            table.add_row(f"  {state}", str(count))
        connected = sum(
            1
            for node in (nodes.get("nodes") or [])
            if isinstance(node, Mapping) and node.get("connected") is True
        )
        table.add_row("connected now", str(connected))
    live = _num(sessions)
    table.add_row("live sessions", DASH if live is None else f"{live:.0f}")
    return table
