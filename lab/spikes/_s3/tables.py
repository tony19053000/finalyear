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
from spikes._harness.report import cell, fmt, link_name
from spikes._s3.protocol import GRPC_DEFAULT_LIMIT, format_size

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


def _cell(
    row: Row | None,
    value: float | None,
    stdev: float | None = None,
    *,
    digits: int = 1,
) -> str:
    if row is None or value is None:
        return "TBD"
    text = (
        fmt(value, digits)
        if stdev is None
        else f"{value:.{digits}f} ± {fmt(stdev, digits)}"
    )
    return text if row.valid else f"{text} **INVALID (n={row.n})**"


def _index(rows: Sequence[Row]) -> dict[tuple[str, int], Row]:
    """One row per (family, size) for the matrices: never a sweep row (those
    share the family of the default-chunk row and would overwrite it), and the
    row with the most valid repetitions when a pair+link was measured twice."""
    index: dict[tuple[str, int], Row] = {}
    for row in rows:
        if row.sweep:
            continue
        key = (row.family, row.size)
        current = index.get(key)
        if current is None or (row.valid, row.n) > (current.valid, current.n):
            index[key] = row
    return index


def _matrix(
    rows: Sequence[Row],
    *,
    families: Sequence[str],
    sizes: Sequence[int],
    value: str,
) -> list[str]:
    index = _index(rows)
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
                # two decimals: at ~0.4 ms/MiB one decimal loses the resolution
                # of the one metric that does not shrink on a faster link
                cells.append(_cell(row, row.cpu_mean if row else None, digits=2))
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
    """The gRPC chunk sweep: one ``grpc-down`` row per chunk at the swept size.

    The row measured with the default chunk is part of the sweep even though it
    is not marked ``sweep`` (it is not run twice), so "best chunk" sees it too.
    When the same pair+link was measured more than once, each chunk keeps the
    row with the most valid repetitions (like :func:`pick`), never the fastest.
    """
    sizes = sorted({r.size for r in rows if r.sweep})
    if not sizes:
        return []
    target = sizes[-1]
    per_chunk: dict[int, Row] = {}
    for row in rows:
        if row.family != "grpc-down" or row.size != target or not row.chunk:
            continue
        current = per_chunk.get(row.chunk)
        if current is None or (row.valid, row.n) > (current.valid, current.n):
            per_chunk[row.chunk] = row
    return [per_chunk[chunk] for chunk in sorted(per_chunk)]


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
            error = item.get("error")
            if not isinstance(value, int | float):
                text = f"failed: {cell(error)}"
            else:
                text = f"{fmt(value, 0)} ± {fmt(item.get('stdev_mbps'), 0)}"
                if error:
                    # A ceiling that died part-way still has samples, so a mean
                    # exists - but it is a mean of the reps that ran before the
                    # failure. Rendering it bare would let a broken run read as a
                    # confident low ceiling, and the verdict tells the reader a
                    # LAN result near the ceiling is tool-limited. Say so.
                    text += f" **(incomplete: {cell(error)})**"
            blocks.append(f"| {case_id} | {text} | {item.get('n', 0)} |")
        blocks.append("")
    return "\n".join(blocks) + "\n" if found else ""


def pick(rows: Sequence[Row], family: str, size: int) -> Row | None:
    """The *single* row of ``family`` at ``size`` that the verdict reads.

    Sweep rows are excluded (they vary the chunk, so they are not comparable
    with the raw-TCP baseline measured at the default chunk). If the same
    pair+link was measured more than once, the row with the most valid
    repetitions is used - never the best-looking one - and ties keep the first
    in case-id order.
    """
    candidates = [
        r
        for r in rows
        if r.family == family
        and r.size == size
        and not r.sweep
        and r.mbps_mean is not None
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (r.valid, r.n))


def decision_size(rows: Sequence[Row]) -> int:
    """Largest payload with a **valid** raw TCP, gRPC and HTTP download (0 if none).

    Only single-stream, non-sweep download rows count: the decision compares
    one connection of each transport at the same chunk and payload.
    """
    per_family: dict[str, set[int]] = {}
    for row in rows:
        if row.sweep or row.mbps_mean is None or not row.valid:
            continue
        if row.family in {"tcp-down", "grpc-down", "http-get"}:
            per_family.setdefault(row.family, set()).add(row.size)
    if not {"tcp-down", "grpc-down", "http-get"} <= per_family.keys():
        return 0
    common = per_family["tcp-down"] & per_family["grpc-down"] & per_family["http-get"]
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


#: Parallel ranges count as a gain / a loss outside this band around x1.00.
#: The band is half-open, [PARALLEL_LOSS_X, PARALLEL_GAIN_X): its lower edge is
#: "no measurable gain", its upper edge is already "faster".
PARALLEL_GAIN_X: Final = 1.10
PARALLEL_LOSS_X: Final = 0.90


def _speedup(row: Row | None, baseline: Row | None) -> float | None:
    if (
        row is None
        or baseline is None
        or not baseline.mbps_mean
        or row.mbps_mean is None
    ):
        return None
    return row.mbps_mean / baseline.mbps_mean


def parallel_lines(
    rows: Sequence[Row], size: int, http_plain: Row | None, tcp: Row | None
) -> list[str]:
    """One judgement per HTTP parallel-range family at ``size``.

    N connections are compared with one HTTP GET (the honest baseline for
    "does splitting into parallel chunks help on this link?") and, for the
    ADR-0003 decision, also placed against raw TCP. A ratio below x1 is called
    **slower**, never "x0.8 as fast". The "no measurable gain" band is the
    half-open interval [PARALLEL_LOSS_X, PARALLEL_GAIN_X): exactly x0.90 reads
    "no measurable gain", exactly x1.10 reads "faster".
    """
    lines: list[str] = []
    families = sorted({r.family for r in rows if r.family.startswith("http-range")})
    for family in families:
        row = pick(rows, family, size)
        speedup = _speedup(row, http_plain)
        name = family_name(family)
        if row is None or speedup is None:
            lines.append(f"- {name}: TBD (not measured at {format_size(size)}).")
            continue
        mbps = fmt(row.mbps_mean, 0)
        vs_tcp = _share(row, tcp)
        tcp_text = f"; {vs_tcp:.0f}% of raw TCP" if vs_tcp is not None else ""
        invalid = "" if row.valid else f" **INVALID (n={row.n})**"
        if speedup >= PARALLEL_GAIN_X:
            lines.append(
                f"- {name}: **x{speedup:.2f} faster** than a single HTTP GET "
                f"({mbps} Mbit/s{tcp_text}){invalid} - the parallel chunk fetch of "
                "ADR-0003 pays on this link."
            )
        elif speedup >= PARALLEL_LOSS_X:
            lines.append(
                f"- {name}: **x{speedup:.2f}** of a single HTTP GET ({mbps} "
                f"Mbit/s{tcp_text}){invalid} - no measurable gain: one connection "
                "already fills this link; ADR-0003's parallel fetch is worth keeping "
                "for resumability and multi-holder fetches, not for speed here."
            )
        else:
            lines.append(
                f"- {name}: **x{speedup:.2f} - slower** than a single HTTP GET "
                f"({mbps} Mbit/s{tcp_text}){invalid} - the extra connections cost "
                "more than they gain; ADR-0003 should default to one connection "
                "per holder on links like this."
            )
    return lines


#: A runner-up chunk within this share of the best is "not distinguishable".
CHUNK_TIE_PCT: Final = 5.0

#: Slack for the tie comparison. ``gap`` is a float division, so a
#: mathematically exact 5% gap (1000 vs 950) lands one ULP *above* 5.0 and would
#: silently fall out of the band the docs promise is inclusive. The epsilon is
#: far below the precision the gap is ever printed or reasoned about at, so it
#: restores the documented boundary without widening the band.
CHUNK_TIE_EPS: Final = 1e-9


def best_chunk(rows: Sequence[Row]) -> Row | None:
    """The fastest **valid** gRPC chunk of one pair+link's sweep, or ``None``.

    Judged inside one group only: mixing pairs or links would let the fastest
    link, not the best chunk, win. Needs at least two valid chunks to compare.
    """
    valid = [r for r in sweep_rows(rows) if r.valid and r.mbps_mean is not None]
    if len(valid) < 2:
        return None
    return max(valid, key=lambda r: r.mbps_mean or 0.0)


def chunk_line(rows: Sequence[Row]) -> str | None:
    """The generated "best gRPC chunk" sentence for one pair+link (or ``None``)."""
    best = best_chunk(rows)
    if best is None:
        return None
    others = [
        r
        for r in sweep_rows(rows)
        if r is not best and r.valid and r.mbps_mean is not None
    ]
    listing = ", ".join(f"{format_size(r.chunk)} {fmt(r.mbps_mean, 0)}" for r in others)
    runner_up = max(others, key=lambda r: r.mbps_mean or 0.0)
    gap = 100.0 * (1 - (runner_up.mbps_mean or 0.0) / (best.mbps_mean or 1.0))
    text = (
        f"- Best gRPC chunk (message) size at {format_size(best.size)}: "
        f"**{format_size(best.chunk)}** ({fmt(best.mbps_mean, 0)} Mbit/s; "
        f"others: {listing})"
    )
    if gap <= CHUNK_TIE_PCT + CHUNK_TIE_EPS:
        text += (
            f" - {format_size(runner_up.chunk)} is within {gap:.0f}%, so the two "
            "are not distinguishable at this n"
        )
    if best.chunk >= GRPC_DEFAULT_LIMIT:
        text += (
            "; this is exactly gRPC's default 4 MB message limit - anything larger "
            "needs `max_receive_message_length` raised on the receiver (see the "
            "limit probe)"
        )
    return text + "."


def chunk_caveat(rows: Sequence[Row], decision_row: Row | None) -> str:
    """Names the chunk the ADR-0005 judgement was measured at, and warns when the
    sweep already found a better one.

    :func:`pick` deliberately reads the default-chunk row, but the sweep can show
    a materially faster message size. Without this, a gRPC penalty caused by the
    message size reads as a penalty caused by gRPC - and an ADR gets revised for
    the wrong reason. The comparison is made **inside the sweep**, at the sweep's
    own size, so it is like for like.
    """
    if decision_row is None:
        return ""
    text = f" Measured at chunk {format_size(decision_row.chunk)}."
    best = best_chunk(rows)
    if best is None or not best.mbps_mean:
        return text
    same = next(
        (
            r
            for r in sweep_rows(rows)
            if r.chunk == decision_row.chunk and r.valid and r.mbps_mean
        ),
        None,
    )
    if same is None or same is best or not same.mbps_mean:
        return text
    # Normalised exactly like chunk_line's tie test - a *gap below the best* -
    # so the two sentences in one verdict block can never disagree about the
    # same pair of chunks (a gain-over normalisation makes an exact 5.00% gap
    # read as a 5.26% gain, firing this caveat while chunk_line calls the two
    # "not distinguishable"). CHUNK_TIE_EPS keeps the boundary where it is
    # documented, as it does there.
    gap = 100.0 * (1 - same.mbps_mean / best.mbps_mean)
    if gap <= CHUNK_TIE_PCT + CHUNK_TIE_EPS:
        return text
    gain = 100.0 * (best.mbps_mean / same.mbps_mean - 1.0)
    return (
        f"{text} The chunk sweep at {format_size(best.size)} found "
        f"{format_size(best.chunk)} **{fmt(gain, 1)}% faster** than this chunk, so "
        "part of this penalty is the message size, not gRPC - re-measure at that "
        "chunk before revising the ADR."
    )


def group_verdict(pair: str, link: str, rows: Sequence[Row]) -> list[str]:
    """The Q2 lines for one pair+link (``TBD`` where data is missing)."""
    title = f"**{pair} · {link_name(link)}**"
    size = decision_size(rows)
    if size == 0:
        return [
            f"{title}: TBD - no payload size has a valid (n >= {MIN_VALID_REPS}) raw "
            "TCP, gRPC and HTTP download alike, so no transport decision can be "
            "made from this run.\n"
        ]
    tcp = pick(rows, "tcp-down", size)
    grpc_row = pick(rows, "grpc-down", size)
    http_plain = pick(rows, "http-get", size)
    lines = [
        f"{title} - at {format_size(size)}, receiver throughput over one connection: "
        f"raw TCP {fmt(tcp.mbps_mean if tcp else None, 0)} Mbit/s (baseline), gRPC "
        f"{fmt(grpc_row.mbps_mean if grpc_row else None, 0)}, HTTP GET "
        f"{fmt(http_plain.mbps_mean if http_plain else None, 0)}.\n",
        _judge(
            _share(grpc_row, tcp), "Tensors over gRPC streams (ADR-0005)", "ADR-0005"
        )
        + chunk_caveat(rows, grpc_row),
        _judge(_share(http_plain, tcp), "Blobs over HTTP (ADR-0003)", "ADR-0003"),
    ]
    lines += parallel_lines(rows, size, http_plain, tcp)
    chunk = chunk_line(rows)
    if chunk is not None:
        lines.append(chunk)
    lines.append("")
    return lines


def verdict(rows: Sequence[Row]) -> str:
    """The generated answer to ARCHITECTURE §27 Q2 (never hard-coded).

    One block per measured pair and link: numbers from different laptops or
    links are never mixed into one "best". Single-stream transports are
    compared with the single-stream raw-TCP baseline at the same payload and
    chunk; HTTP's parallel ranges are judged against a single HTTP GET
    (faster / no gain / slower) and placed against raw TCP; the gRPC chunk
    sweep names the best chunk per group, valid rows only.
    """
    out = ["### Q2 answer (generated from the data above)\n"]
    groups = _groups(rows)
    if not groups:
        out.append("TBD: no S3 results, so no transport decision can be made yet.\n")
        return "\n".join(out)
    for (pair, link), group in groups.items():
        out += group_verdict(pair, link, group)
    out.append(
        "Read every number against the loopback ceiling above: where the LAN result "
        "is within a few percent of the ceiling, the tool (Python, framing, CRC "
        "verification), not the link, is the limit.\n"
    )
    return "\n".join(out)


def _probe_outcome(shot: dict[str, Any] | None, expected_ok: bool) -> str:
    if not isinstance(shot, dict):
        return "TBD"
    if shot.get("ok"):
        return "passed" + ("" if expected_ok else " (unexpected)")
    text = f"refused - {shot.get('failure_type')}: {shot.get('error')}"
    return text if not expected_ok else text + " (unexpected)"


def probe_table(envelopes: Sequence[dict[str, Any]]) -> str:
    """4 MiB gRPC messages at, and one byte over, gRPC's default 4 MB limit."""
    lines: list[str] = []
    for doc in envelopes:
        probe = (doc.get("extra") or {}).get("grpc_default_limit_probe")
        if not isinstance(probe, dict):
            continue
        params = doc.get("params") or {}
        who = f"{host_label(doc)} -> {params.get('server_name', '?')}"
        at_limit = probe.get("at_default_limit") or {
            k: probe.get(k) for k in ("ok", "failure_type", "error")
        }
        lines.append(
            f"| {who} | {format_size(int(probe.get('chunk_bytes') or 0))} | "
            f"{probe.get('default_limit_bytes')} (default) | "
            f"{_probe_outcome(at_limit, expected_ok=True)} |"
        )
        over = probe.get("over_limit")
        if isinstance(over, dict):
            lines.append(
                f"| {who} | {format_size(int(probe.get('chunk_bytes') or 0))} | "
                f"{over.get('limit_bytes')} (one byte under) | "
                f"{_probe_outcome(over, expected_ok=False)} |"
            )
    if not lines:
        return ""
    return (
        "\n".join(
            [
                "### gRPC message-limit probe (one shot each, not a measurement)\n",
                "gRPC's default `max_receive_message_length` is 4 MB = 4194304 bytes "
                "and inclusive, so a 4 MiB message is expected to pass; the second "
                "row records the exact refusal for a message larger than the limit.\n",
                "| client -> server | message | receiver limit (bytes) | outcome |",
                "|---|---|---|---|",
                *lines,
                "",
            ]
        )
        + "\n"
    )


def render_tables(envelopes: Sequence[dict[str, Any]]) -> str:
    """Everything S3 appends to the generated report, in reading order."""
    rows = collect(envelopes)
    parts = [
        throughput_tables(rows),
        sweep_table(rows),
        cpu_tables(rows),
        ceiling_table(envelopes),
        probe_table(envelopes),
        verdict(rows),
    ]
    return "\n".join(part for part in parts if part)
