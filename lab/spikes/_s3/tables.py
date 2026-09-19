"""The S3-specific part of the report: transport x size tables and the Q2 answer.

The generic harness renders one row per case x metric; that is the raw evidence
but nobody can read a transport decision out of it. These tables put the same
numbers in the shape the decision needs - transports as rows, payload sizes as
columns - and :func:`verdict` states, in numbers, how close gRPC and HTTP get to
the raw-TCP baseline, which is exactly ARCHITECTURE §27 Q2 and the open point in
ADR-0003 / ADR-0005. The wording is generated from the data, never hard-coded;
missing data becomes ``TBD`` instead of a guess.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from spikes._harness import MIN_VALID_REPS
from spikes._harness.envelope import host_label
from spikes._harness.report import fmt, link_name
from spikes._s3.protocol import format_size

#: A transport is "as good as raw TCP" above this share of the baseline.
GOOD_PCT: Final = 85.0
#: Below this share the overhead is a design problem, not a rounding error.
POOR_PCT: Final = 60.0
_FAMILY_ORDER: Final = ("tcp-down", "tcp-up", "grpc-down", "grpc-up", "http-get")
_FAMILY_NAMES: Final = {
    "tcp-down": "raw TCP (server -> client)",
    "tcp-up": "raw TCP (client -> server)",
    "grpc-down": "gRPC server-stream",
    "grpc-up": "gRPC client-stream",
    "http-get": "HTTP GET",
}


@dataclass(frozen=True, slots=True)
class Row:
    """One case of one result file, flattened for the tables."""

    pair: str
    link: str
    family: str
    case_id: str
    size: int
    chunk: int
    sweep: bool
    n: int
    mbps_mean: float | None
    mbps_stdev: float | None
    cpu_mean: float | None

    @property
    def valid(self) -> bool:
        return self.n >= MIN_VALID_REPS


def family_of(attrs: dict[str, Any]) -> str:
    transport = str(attrs.get("transport", "?"))
    if transport == "http":
        parts = attrs.get("parts")
        return f"http-range{parts}" if attrs.get("ranged") else "http-get"
    direction = "up" if attrs.get("direction") == "upload" else "down"
    return f"{transport}-{direction}"


def family_name(family: str) -> str:
    if family.startswith("http-range"):
        return f"HTTP {family.removeprefix('http-range')} parallel ranges"
    return _FAMILY_NAMES.get(family, family)


def _mean(case: dict[str, Any], metric: str) -> float | None:
    summary = case.get("summary", {}).get(metric) or {}
    value = summary.get("mean")
    return float(value) if isinstance(value, int | float) else None


def collect(envelopes: Sequence[dict[str, Any]]) -> list[Row]:
    """Flatten every S3 envelope into :class:`Row` records."""
    rows: list[Row] = []
    for doc in envelopes:
        params = doc.get("params") or {}
        pair = f"{host_label(doc)} -> {params.get('server_name', '?')}"
        for case_id in sorted(doc.get("cases", {})):
            case = doc["cases"][case_id]
            attrs = case.get("attrs") or {}
            summary = case.get("summary", {}).get("recv_mbps") or {}
            rows.append(
                Row(
                    pair=pair,
                    link=str(doc.get("link", "?")),
                    family=family_of(attrs),
                    case_id=case_id,
                    size=int(attrs.get("size_bytes") or 0),
                    chunk=int(attrs.get("chunk_bytes") or 0),
                    sweep=bool(attrs.get("sweep")),
                    n=int(summary.get("n") or 0),
                    mbps_mean=_mean(case, "recv_mbps"),
                    mbps_stdev=(
                        float(summary["stdev"])
                        if isinstance(summary.get("stdev"), int | float)
                        else None
                    ),
                    cpu_mean=_mean(case, "cpu_ms_per_mib"),
                )
            )
    return rows


def _cell(row: Row | None, value: float | None, stdev: float | None = None) -> str:
    if row is None or value is None:
        return "TBD"
    text = fmt(value, 1) if stdev is None else f"{value:.1f} ± {fmt(stdev, 1)}"
    return text if row.valid else f"{text} **INVALID (n={row.n})**"


def _matrix(
    rows: Sequence[Row],
    *,
    families: Sequence[str],
    sizes: Sequence[int],
    value: str,
) -> list[str]:
    index = {(r.family, r.size): r for r in rows}
    lines = [
        "| transport | " + " | ".join(format_size(s) for s in sizes) + " |",
        "|---" * (len(sizes) + 1) + "|",
    ]
    for family in families:
        cells = []
        for size in sizes:
            row = index.get((family, size))
            if value == "mbps":
                cells.append(
                    _cell(
                        row,
                        row.mbps_mean if row else None,
                        row.mbps_stdev if row else None,
                    )
                )
            else:
                cells.append(_cell(row, row.cpu_mean if row else None))
        lines.append(f"| {family_name(family)} | " + " | ".join(cells) + " |")
    return lines


def _groups(rows: Sequence[Row]) -> dict[tuple[str, str], list[Row]]:
    groups: dict[tuple[str, str], list[Row]] = {}
    for row in rows:
        groups.setdefault((row.pair, row.link), []).append(row)
    return dict(sorted(groups.items()))


def _families(rows: Sequence[Row]) -> list[str]:
    seen = {r.family for r in rows if not r.sweep}
    ordered: list[str] = [f for f in _FAMILY_ORDER if f in seen]
    ordered += sorted(f for f in seen if f not in _FAMILY_ORDER)
    return ordered


def _sizes(rows: Sequence[Row]) -> list[int]:
    return sorted({r.size for r in rows if not r.sweep and r.size})


def throughput_tables(rows: Sequence[Row]) -> str:
    """Receiver throughput, transports x payload sizes, one block per pair+link."""
    if not rows:
        return "### Throughput by transport and payload size\n\nTBD (no results)\n"
    out = ["### Throughput by transport and payload size (Mbit/s, mean ± stdev)\n"]
    for (pair, link), group in _groups(rows).items():
        out.append(f"**{pair} · {link_name(link)}**\n")
        out += _matrix(
            group, families=_families(group), sizes=_sizes(group), value="mbps"
        )
        out.append("")
    return "\n".join(out) + "\n"


def cpu_tables(rows: Sequence[Row]) -> str:
    """CPU cost per MiB (client + server) - the "is this Python or the link?" table."""
    if not rows:
        return ""
    out = ["### CPU cost (ms of CPU per MiB moved, client + server)\n"]
    for (pair, link), group in _groups(rows).items():
        out.append(f"**{pair} · {link_name(link)}**\n")
        out += _matrix(
            group, families=_families(group), sizes=_sizes(group), value="cpu"
        )
        out.append("")
    return "\n".join(out) + "\n"


def sweep_rows(rows: Sequence[Row]) -> list[Row]:
    """The gRPC chunk sweep: every ``grpc-down`` row at the swept payload size.

    The row measured with the default chunk is part of the sweep even though it
    is not marked ``sweep`` (it is not run twice), so "best chunk" sees it too.
    """
    sizes = sorted({r.size for r in rows if r.sweep})
    if not sizes:
        return []
    target = sizes[-1]
    return sorted(
        (r for r in rows if r.family == "grpc-down" and r.size == target and r.chunk),
        key=lambda r: r.chunk,
    )


def sweep_table(rows: Sequence[Row]) -> str:
    """gRPC message (chunk) size sweep at one payload size."""
    if not any(r.sweep for r in rows):
        return ""
    out = ["### gRPC chunk (message) size sweep\n"]
    for (pair, link), group in _groups(rows).items():
        entries = sweep_rows(group)
        if not entries:
            continue
        target = entries[0].size
        out.append(f"**{pair} · {link_name(link)} · payload {format_size(target)}**\n")
        out.append("| chunk | throughput (Mbit/s) | CPU (ms/MiB) | n |")
        out.append("|---|---|---|---|")
        out.extend(
            f"| {format_size(row.chunk)} | {_cell(row, row.mbps_mean, row.mbps_stdev)} "
            f"| {fmt(row.cpu_mean, 2)} | {row.n} |"
            for row in entries
        )
        out.append("")
    return "\n".join(out) + "\n"


def ceiling_table(envelopes: Sequence[dict[str, Any]]) -> str:
    """The tool's own loopback limit per transport, so LAN numbers can be read."""
    blocks: list[str] = ["### Loopback ceiling (this tool, no network)\n"]
    found = False
    for doc in envelopes:
        ceiling = (doc.get("extra") or {}).get("loopback_ceiling")
        if not isinstance(ceiling, dict):
            continue
        found = True
        blocks.append(
            f"**{host_label(doc)}** · payload "
            f"{format_size(int(ceiling.get('size_bytes') or 0))} · CRC-32 verification "
            f"{fmt(ceiling.get('crc_rate_mib_s'), 0)} MiB/s\n"
        )
        if not ceiling.get("ok"):
            blocks.append(f"- not measured: {ceiling.get('error')}\n")
            continue
        blocks.append("| transport | ceiling (Mbit/s, mean ± stdev) | n |")
        blocks.append("|---|---|---|")
        for case_id, item in sorted((ceiling.get("per_transport") or {}).items()):
            value = item.get("mean_mbps")
            text = (
                f"{fmt(value, 0)} ± {fmt(item.get('stdev_mbps'), 0)}"
                if isinstance(value, int | float)
                else f"failed: {item.get('error')}"
            )
            blocks.append(f"| {case_id} | {text} | {item.get('n', 0)} |")
        blocks.append("")
    return "\n".join(blocks) + "\n" if found else ""


def _best(rows: Sequence[Row], family: str, size: int) -> Row | None:
    candidates = [
        r
        for r in rows
        if r.family == family and r.size == size and r.mbps_mean is not None
    ]
    return max(candidates, key=lambda r: r.mbps_mean or 0.0) if candidates else None


def decision_size(rows: Sequence[Row]) -> int:
    """Largest payload measured for raw TCP, gRPC and HTTP alike (0 if none)."""
    per_family: dict[str, set[int]] = {}
    for row in rows:
        if row.sweep or row.mbps_mean is None:
            continue
        per_family.setdefault(row.family.split("-")[0], set()).add(row.size)
    if not {"tcp", "grpc", "http"} <= per_family.keys():
        return 0
    common = per_family["tcp"] & per_family["grpc"] & per_family["http"]
    return max(common) if common else 0


def _share(row: Row | None, baseline: Row | None) -> float | None:
    if (
        row is None
        or baseline is None
        or not baseline.mbps_mean
        or row.mbps_mean is None
    ):
        return None
    return 100.0 * row.mbps_mean / baseline.mbps_mean


def _judge(share: float | None, what: str, adr: str) -> str:
    if share is None:
        return f"- {what}: TBD (not measured)."
    if share >= GOOD_PCT:
        extra = " (at or above the baseline)" if share >= 100 else ""
        return (
            f"- {what}: **{share:.0f}%** of the raw-TCP baseline{extra} - the overhead "
            f"is acceptable, {adr} stands as written."
        )
    if share >= POOR_PCT:
        return (
            f"- {what}: **{share:.0f}%** of raw TCP - usable, but the cost is real; "
            f"{adr} should record the measured penalty and the chunk size used."
        )
    return (
        f"- {what}: **{share:.0f}%** of raw TCP - too expensive; {adr} needs a "
        "revision (raw framed TCP for the bulk path)."
    )


def verdict(rows: Sequence[Row]) -> str:
    """The generated answer to ARCHITECTURE §27 Q2 (never hard-coded).

    Single-stream transports are compared with the single-stream raw-TCP
    baseline; HTTP's parallel ranges are reported separately as a speed-up over
    a single HTTP GET, because comparing N connections with one would flatter
    them.
    """
    out = ["### Q2 answer (generated from the data above)\n"]
    size = decision_size(rows)
    if size == 0:
        out.append(
            "TBD: results for raw TCP, gRPC and HTTP at one common payload size are "
            "missing, so no transport decision can be made yet.\n"
        )
        return "\n".join(out)
    tcp = _best(rows, "tcp-down", size)
    grpc_row = _best(rows, "grpc-down", size)
    http_plain = _best(rows, "http-get", size)
    parallel = _http_parallel_best(rows, size)
    out.append(
        f"At {format_size(size)}, receiver throughput (one connection): raw TCP "
        f"{fmt(tcp.mbps_mean if tcp else None, 0)} Mbit/s (baseline), gRPC "
        f"{fmt(grpc_row.mbps_mean if grpc_row else None, 0)}, HTTP GET "
        f"{fmt(http_plain.mbps_mean if http_plain else None, 0)}.\n"
    )
    out.append(
        _judge(
            _share(grpc_row, tcp), "Tensors over gRPC streams (ADR-0005)", "ADR-0005"
        )
    )
    out.append(
        _judge(_share(http_plain, tcp), "Blobs over HTTP (ADR-0003)", "ADR-0003")
    )
    speedup = _speedup(parallel, http_plain)
    if speedup is not None and parallel is not None:
        out.append(
            f"- {family_name(parallel.family)} move the same payload "
            f"**x{speedup:.2f}** as fast as a single HTTP GET "
            f"({fmt(parallel.mbps_mean, 0)} Mbit/s) - this is the parallel, "
            "resumable chunk fetch ADR-0003 describes."
        )
    best_chunk = _best_chunk(rows)
    if best_chunk is not None:
        out.append(
            f"- Best gRPC chunk (message) size measured: **{format_size(best_chunk)}**; "
            "gRPC's default 4 MB receive limit must be raised for chunks near it."
        )
    out.append(
        "\nRead every number against the loopback ceiling above: where the LAN result "
        "is within a few percent of the ceiling, the tool (Python, framing, CRC "
        "verification), not the link, is the limit.\n"
    )
    return "\n".join(out)


def _http_parallel_best(rows: Sequence[Row], size: int) -> Row | None:
    families = {r.family for r in rows if r.family.startswith("http-range")}
    best = [b for b in (_best(rows, f, size) for f in sorted(families)) if b]
    return max(best, key=lambda r: r.mbps_mean or 0.0) if best else None


def _speedup(row: Row | None, baseline: Row | None) -> float | None:
    if (
        row is None
        or baseline is None
        or not baseline.mbps_mean
        or row.mbps_mean is None
    ):
        return None
    return row.mbps_mean / baseline.mbps_mean


def _best_chunk(rows: Sequence[Row]) -> int | None:
    """The fastest gRPC chunk size across every measured pair."""
    candidates: list[Row] = []
    for group in _groups(rows).values():
        candidates += [r for r in sweep_rows(group) if r.mbps_mean is not None]
    if not candidates:
        return None
    return max(candidates, key=lambda r: r.mbps_mean or 0.0).chunk


def render_tables(envelopes: Sequence[dict[str, Any]]) -> str:
    """Everything S3 appends to the generated report, in reading order."""
    rows = collect(envelopes)
    parts = [
        throughput_tables(rows),
        sweep_table(rows),
        cpu_tables(rows),
        ceiling_table(envelopes),
        verdict(rows),
    ]
    return "\n".join(part for part in parts if part)
