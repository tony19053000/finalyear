"""Terminal rendering for ``compute`` (roadmap 0.3.12).

Pure functions from an API payload to a Rich renderable, so every table in the
unit tests is built from a canned payload with no network and no terminal.

**Defensive by design.** The payload comes from
:mod:`meshforge.controller.api.models`, which the CLI may not import (D15), so
nothing here assumes a field is present or has the expected type: a missing or
null value renders as ``-`` and a field this build has never heard of is ignored
(it is still visible in ``--json``). That way an older ``compute`` against a newer
controller degrades instead of crashing.

**Untrusted text (V0.3).** gRPC enrolment is unauthenticated, and the controller
validates ``Hello.hostname`` / ``os`` / ``platform`` / ``hardware`` for *length*
only - so any host on the LAN can put escape sequences in a value that every
operator's ``compute nodes`` then prints. :func:`_cell` is the single gate every
string cell goes through: non-printable characters become spaces (the same
approach :func:`meshforge.cli.client._clean` applies to server-supplied error
text), the value is capped, and the result is a :class:`rich.text.Text` - Rich's
explicit "render this literally" type - so ``[bold red]`` in a hostname is never
interpreted as markup whatever the console's ``markup`` setting is. ``--json``
is deliberately *not* touched: it prints the API payload verbatim for scripting,
and JSON string escaping already renders a control character as ``\u001b``.

**Hardware scores (roadmap 0.3.4, D38).** ``nodes_table`` gets one compact
``SCORES`` cell per node (``CPU 312 GF, GPU0 8.1 TF``; ``pending``; ``-`` for
none) and ``compute nodes <id>`` prints :func:`scores_table` under the detail
table: one row per metric, a measured value with its spread and conditions or the
reason it was not measured. A value the backend could only lower-bound prints
as ``>=``; a metric that is not ``ok`` prints ``n/a``, never a number. Every
string in either comes from the agent (backend names, reasons, details, even
the metric names) and goes through :func:`_cell`.

**Portability.** Tables use the ASCII box style, not Unicode box-drawing, so the
output is identical in Windows Terminal, a legacy ``cmd.exe`` code page and a
pipe.

**The one output path (V0.8 G6, D178).** Every byte ``compute`` prints for a
human goes through this module: :func:`show` (a table or text on stdout),
:func:`line` (one plain line), :func:`note` (a ``compute: ...`` line on
stderr), :func:`fail` (the error line before a non-zero exit) and
:func:`emit_json` (``--json``). The only exception is the workload's own log
bytes (``compute logs``), which are copied byte for byte. Colour is off when
``NO_COLOR`` is set to anything non-empty (https://no-color.org) and when the
stream is not a terminal (Rich's own detection); it is never needed - every
status is spelled out in words. ``--json`` output is one line, keys sorted,
with a top-level ``schema_version`` (docs/CLI.md).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from typing import Any, Final

import typer
from rich import box
from rich.console import Console, RenderableType
from rich.table import Table
from rich.text import Text

__all__ = [
    "JSON_SCHEMA_VERSION",
    "MAX_CELL_LENGTH",
    "NODE_COLUMNS",
    "PROG_NAME",
    "cell",
    "clip",
    "console",
    "emit_json",
    "fail",
    "flat",
    "format_age",
    "format_bytes",
    "line",
    "no_color",
    "node_detail_table",
    "nodes_table",
    "not_enforced_cell",
    "note",
    "scores_cell",
    "scores_table",
    "scrub",
    "show",
    "state_counts",
    "status_table",
    "table",
]

PROG_NAME: Final = "compute"

JSON_SCHEMA_VERSION: Final = 1
"""``schema_version`` of every ``--json`` document (docs/CLI.md). Adding a field
keeps it; renaming, removing or retyping a documented field bumps it."""


# ------------------------------------------------------------------ output path


def no_color() -> bool:
    """``NO_COLOR`` set to any non-empty value turns colour off (no-color.org)."""
    return os.environ.get("NO_COLOR", "") != ""


def console(*, stderr: bool = False) -> Console:
    """The console for tables: no markup, emoji or highlighting (a value from
    the network is never interpreted), colour per :func:`no_color` and the
    stream's TTY-ness."""
    return Console(
        stderr=stderr,
        highlight=False,
        emoji=False,
        markup=False,
        no_color=no_color(),
    )


def show(renderable: RenderableType) -> None:
    """A table (or :class:`~rich.text.Text`) on stdout."""
    console().print(renderable)


def flat(text: object) -> str:
    """One printable line: every control character (ESC, CR, LF, NUL...) becomes
    a space, so nothing a server sent can move the cursor or forge a line."""
    raw = text if isinstance(text, str) else str(text)
    return "".join(ch if ch.isprintable() else " " for ch in raw)


def line(text: object = "", *, err: bool = False) -> None:
    """One plain line, flattened. Not through Rich: a console would wrap a long
    line at the terminal width (or at 80 columns in a pipe), which breaks
    ``grep`` and the scripts that read a job id from stdout."""
    typer.echo(flat(text), err=err)


def note(text: object) -> None:
    """A ``compute: ...`` progress or advice line on stderr (stdout stays for
    the command's result, so it can be piped)."""
    line(f"{PROG_NAME}: {flat(text)}", err=True)


def fail(message: object) -> None:
    """The error line printed before a non-zero exit, on stderr.

    Scrubbed with the logs' rules (bearer/API/session tokens, join codes,
    ``key=value`` secrets, PEM keys): the message may quote text the
    controller sent, and an error line ends up in scrollback, CI logs and bug
    reports.
    """
    note(scrub(message))


def scrub(text: object) -> str:
    """Flattened and scrubbed with the logs' rules (see :func:`fail`). For any
    server-supplied error text shown *outside* :func:`fail` - ``compute
    status`` prints it in its table and in ``--json`` ``detail`` (V0.8 G6 T-1)."""
    from meshforge.common.redaction import scrub_text

    return scrub_text(flat(text))


def clip(value: object) -> str:
    """A server value for a plain line: flattened and capped at
    :data:`MAX_CELL_LENGTH` like a table cell (V0.8 G6 T-2)."""
    text = flat("" if value is None else value)
    return text if len(text) <= MAX_CELL_LENGTH else text[:MAX_CELL_LENGTH] + "..."


def emit_json(payload: Mapping[str, Any]) -> None:
    """``--json``: one line, keys sorted, ``schema_version`` at the top level.

    The payload is the controller's answer (or, for ``compute status``, the
    CLI's own document). Not through Rich: a console would wrap and style it,
    and this output is meant for ``jq``. JSON string escaping already turns a
    control character into ``\\u001b``, so nothing needs flattening.
    """
    document = {**payload, "schema_version": JSON_SCHEMA_VERSION}
    typer.echo(json.dumps(document, separators=(",", ":"), sort_keys=True))


NODE_COLUMNS = (
    "NODE",
    "HOSTNAME",
    "STATE",
    "CONN",
    "RTT",
    "CPU%",
    "RAM",
    "GPU",
    "AGE",
    "NOT ENFORCED",
    "SCORES",
)

SCORE_COLUMNS = (
    "METRIC",
    "GPU",
    "STATUS",
    "VALUE",
    "UNIT",
    "STDEV",
    "N",
    "BACKEND",
    "THREADS",
    "NOTES",
)

DASH = "-"
"""What a null, missing or unusable value renders as (ASCII, one character)."""

MAX_CELL_LENGTH = 256
"""Longest string a cell prints before it is truncated. The longest legitimate
value is ``Hello.hostname`` at 253 characters (RFC 1035); a stored ``hardware``
value has no such bound, and a megabyte in one cell is a denial of service
against the operator's terminal."""

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def _num(value: object) -> float | None:
    """A finite number or ``None`` - a JSON string or null never raises here."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number == number and abs(number) != float("inf") else None  # NaN


def _cell(value: object) -> Text:
    """The one gate for every string that reaches the terminal.

    Flattens every non-printable character (ESC, BEL, CR, LF, NUL and the
    Unicode format characters) to a space, caps the length, and returns a
    :class:`~rich.text.Text`, which Rich renders literally - so no value can
    move the cursor, rewrite the window title, recolour another node's row or
    smuggle Rich markup. A value that is empty once flattened renders as ``-``.
    """
    if value is None:
        return Text(DASH)
    raw = value if isinstance(value, str) else str(value)
    flat = "".join(ch if ch.isprintable() else " " for ch in raw).strip()
    if not flat:
        return Text(DASH)
    if len(flat) > MAX_CELL_LENGTH:
        flat = flat[:MAX_CELL_LENGTH] + "..."
    return Text(flat)


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


def _mapping(value: object) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _metrics(scores: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    metrics = scores.get("metrics")
    if not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes)):
        return []
    return [m for m in metrics if isinstance(m, Mapping)]


def _score_value(metric: Mapping[str, Any]) -> str:
    """``312``/``8.1``/``>=19.3`` for an ok metric; ``n/a`` otherwise (never 0)."""
    mean = _num(metric.get("mean"))
    if metric.get("status") != "ok" or mean is None:
        return "n/a"
    text = f"{mean:.0f}" if mean >= 100 else f"{mean:.1f}"
    return f">={text}" if metric.get("lower_bound") is True else text


def _find(
    metrics: list[Mapping[str, Any]], name: str, gpu_index: float | None = None
) -> Mapping[str, Any] | None:
    for metric in metrics:
        if metric.get("name") == name and _num(metric.get("gpu_index")) == gpu_index:
            return metric
    return None


def scores_cell(node: Mapping[str, Any]) -> Text:
    """The compact ``SCORES`` cell: ``CPU 312 GF, GPU0 8.1 TF`` / ``pending`` / ``-``.

    CPU is ``cpu_fp32_gflops_all`` (the 1-thread figure when that is all there
    is); one ``GPUn`` per GPU from ``gpu_fp32_tflops``. Built from numbers this
    function formats, but the whole cell still goes through :func:`_cell`.
    """
    scores = _mapping(node.get("scores"))
    if scores is None:
        return Text(DASH)
    status = scores.get("status")
    if status != "measured":
        return _cell("pending" if status == "pending" else None)
    metrics = _metrics(scores)
    parts: list[str] = []
    cpu = _find(metrics, "cpu_fp32_gflops_all") or _find(metrics, "cpu_fp32_gflops_1t")
    if cpu is not None:
        parts.append(f"CPU {_score_value(cpu)} GF")
    gpu_indices = sorted(
        {
            index
            for metric in metrics
            if metric.get("name") == "gpu_fp32_tflops"
            and (index := _num(metric.get("gpu_index"))) is not None
        }
    )
    for index in gpu_indices:
        gpu = _find(metrics, "gpu_fp32_tflops", index)
        if gpu is not None:
            parts.append(f"GPU{index:.0f} {_score_value(gpu)} TF")
    return _cell(", ".join(parts) if parts else "measured")


def _notes(metric: Mapping[str, Any]) -> str:
    notes: list[str] = []
    if metric.get("lower_bound") is True:
        notes.append("lower bound")
    if metric.get("throttling_suspected") is True:
        notes.append("throttling?")
    reason = metric.get("reason")
    if isinstance(reason, str) and reason:
        detail = metric.get("detail")
        notes.append(
            f"{reason}: {detail}" if isinstance(detail, str) and detail else reason
        )
    return "; ".join(notes)


def _yes_no(value: object) -> str:
    return "yes" if value is True else "no" if value is False else "unknown"


def _conditions_caption(scores: Mapping[str, Any]) -> Text:
    conditions = _mapping(scores.get("conditions")) or {}
    load = _num(conditions.get("cpu_load_percent_before"))
    duration, budget = (
        _num(conditions.get("duration_s")),
        _num(conditions.get("budget_s")),
    )
    parts = [
        f"measured {scores.get('measured_at')}",
        f"run {scores.get('run_id')}",
        f"on battery: {_yes_no(conditions.get('on_battery'))}",
        f"load before: {DASH if load is None else f'{load:.0f}%'}",
        f"priority lowered: {_yes_no(conditions.get('priority_lowered'))}",
        f"took {DASH if duration is None else f'{duration:.1f}'} s of "
        f"{DASH if budget is None else f'{budget:.0f}'} s budget",
    ]
    if conditions.get("budget_exhausted") is True:
        parts.append("BUDGET EXHAUSTED")
    if conditions.get("background_load_high") is True:
        parts.append("BACKGROUND LOAD HIGH")
    return _cell(" / ".join(parts))


def scores_table(payload: Mapping[str, Any]) -> Table:
    """``scores`` of ``GET /v1/nodes/{id}`` as a full table (``compute nodes <id>``)."""
    scores = _mapping(payload.get("scores")) or {}
    status = scores.get("status")
    label = status if status in ("measured", "pending", "none") else "unknown"
    table = _table(*SCORE_COLUMNS, title=f"hardware scores ({label})")
    if status != "measured":
        table.caption = {
            "pending": "not measured yet for this hardware; "
            "the agent scores after it connects",
            "none": "this node does not measure hardware scores",
        }.get(label, "the controller sent no scores (older controller?)")
        return table
    table.caption = _conditions_caption(scores)
    for metric in _metrics(scores):
        gpu = _num(metric.get("gpu_index"))
        stdev = _num(metric.get("stdev"))
        n = _num(metric.get("n"))
        threads = _num(metric.get("threads_effective"))
        ok = metric.get("status") == "ok"
        version = metric.get("backend_version")
        backend = metric.get("backend")
        backend_text = (
            f"{backend} {version}" if isinstance(version, str) and version else backend
        )
        table.add_row(
            _cell(metric.get("name")),
            DASH if gpu is None else f"{gpu:.0f}",
            _cell(metric.get("status")),
            _score_value(metric),
            _cell(metric.get("unit")),
            DASH if not ok or stdev is None else f"{stdev:.3g}",
            DASH if not ok or n is None else f"{n:.0f}",
            _cell(backend_text),
            DASH if threads is None else f"{threads:.0f}",
            _cell(_notes(metric) or None),
        )
    return table


def _table(*columns: str, title: str | Text | None = None) -> Table:
    table = Table(title=title, box=box.ASCII, header_style="bold", pad_edge=False)
    for column in columns:
        table.add_column(column, overflow="fold", no_wrap=False)
    return table


cell = _cell
"""Public name of the one string gate (command modules build their own tables)."""

table = _table
"""Public name of the one table style (ASCII box, bold header, folding cells)."""


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
            _cell(node.get("node_id")),
            _cell(node.get("hostname")),
            _cell(node.get("state")),
            _conn(node),
            _rtt(node.get("rtt_ms")),
            _percent(node.get("cpu_percent")),
            _ram(node),
            _gpus(node),
            format_age(node.get("sample_age_s")),
            not_enforced_cell(node.get("not_enforced")),
            scores_cell(node),
        )
    return table


_CAP_SHORT: dict[str, str] = {
    "max_ram_bytes": "ram",
    "max_cpu_percent": "cpu",
    "max_vram_bytes": "vram",
}


def not_enforced_cell(value: object) -> Text:
    """V0.7 G6: the owner caps the node says it cannot enforce, as short field
    names (``vram, cpu``); ``none`` = all enforced; ``-`` = unknown (offline).
    The full ``<field>:<why>`` list is in ``compute nodes NODE_ID``."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return Text(DASH)
    fields: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        field = item.split(":", 1)[0]
        short = _CAP_SHORT.get(field, field)
        if short not in fields:
            fields.append(short)
    return _cell(", ".join(fields) if fields else "none")


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
        rendered: str | Text
        if key == "connected":
            rendered = _conn(payload)
        elif key == "sample_age_s":
            rendered = format_age(value)
        elif key in {"rtt_ms", "rtt_ewma_ms"}:
            rendered = _rtt(value)
        else:
            rendered = _cell(value)
        table.add_row(label, rendered)
    hardware = payload.get("hardware")
    if isinstance(hardware, Mapping):
        for key in sorted(hardware):
            # Both the key and the value are agent-supplied (D13: the controller
            # stores `Hello.hardware` as given), so both go through `_cell`.
            table.add_row(_cell(f"hardware.{key}"), _cell(hardware[key]))
    table.add_row("scores", scores_cell(payload))
    policy = payload.get("owner_policy")
    if isinstance(policy, Mapping):
        for key in sorted(policy):
            value = policy[key]
            if key == "not_enforced":
                listed = (
                    [str(v) for v in value]
                    if isinstance(value, Sequence) and not isinstance(value, str)
                    else []
                )
                value = ", ".join(listed) if listed else "none"
            table.add_row(_cell(f"policy.{key}"), _cell(value))
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
    # `controller_url` comes from this operator's own config and `detail` from
    # `client._clean`, but both still go through `_cell`: one gate, no exceptions.
    table.add_row("controller", _cell(controller_url))
    table.add_row("reachable", "yes" if reachable else "no")
    if detail:
        table.add_row("detail", _cell(detail))
    if nodes is not None:
        total = nodes.get("count")
        table.add_row("nodes", DASH if _num(total) is None else str(total))
        for state, count in state_counts(nodes).items():
            # The state values are the agent-influenced `node.state` strings, and
            # they are row *labels* here - the indent is added outside `_cell`
            # because `_cell` strips surrounding whitespace.
            label = Text("  ")
            label.append(_cell(state))
            table.add_row(label, str(count))
        connected = sum(
            1
            for node in (nodes.get("nodes") or [])
            if isinstance(node, Mapping) and node.get("connected") is True
        )
        table.add_row("connected now", str(connected))
    live = _num(sessions)
    table.add_row("live sessions", DASH if live is None else f"{live:.0f}")
    return table


# ------------------------------------------------------------------ jobs (V0.4 G5)


def jobs_table(payload: Mapping[str, Any]) -> Table:
    """``compute jobs``: one row per job, newest first (every cell via _cell)."""
    table = _table("JOB", "NAME", "STATE", "NODE", "FAILURE", "CREATED", title="Jobs")
    jobs = payload.get("jobs")
    for job in jobs if isinstance(jobs, list) else []:
        if not isinstance(job, Mapping):
            continue
        placement = _mapping(job.get("placement")) or {}
        table.add_row(
            _cell(job.get("job_id")),
            _cell(job.get("name")),
            _cell(job.get("state")),
            _cell(
                placement.get("node_id")
                or ("auto" if placement.get("mode") == "auto" else None)
            ),
            _cell(job.get("failure_type")),
            _cell(job.get("created_at")),
        )
    return table


def job_detail_table(payload: Mapping[str, Any]) -> Table:
    """``compute get``/``compute jobs <id>``-style detail of one job."""
    table = _table("FIELD", "VALUE", title="Job")
    for key in ("job_id", "name", "state", "failure_type", "failure_detail"):
        table.add_row(_cell(key), _cell(payload.get(key)))
    attempts = payload.get("attempts")
    for attempt in attempts if isinstance(attempts, list) else []:
        if not isinstance(attempt, Mapping):
            continue
        missing = attempt.get("missing_outputs")
        missing_text = (
            ",".join(str(m) for m in missing)
            if isinstance(missing, list) and missing
            else DASH
        )
        table.add_row(
            _cell("attempt"),
            _cell(
                f"{attempt.get('attempt_id')} on {attempt.get('node_id')}: "
                f"{attempt.get('state')} exit={attempt.get('exit_code')} "
                f"outputs={attempt.get('output_digest') or DASH} "
                f"missing={missing_text}"
            ),
        )
        transfers = attempt.get("transfers")
        for row in transfers if isinstance(transfers, list) else []:
            if isinstance(row, Mapping):
                table.add_row(_cell("  transfer"), _cell(_transfer_text(row)))
    inputs = payload.get("inputs")
    for item in inputs if isinstance(inputs, list) else []:
        if isinstance(item, Mapping):
            table.add_row(
                _cell("input"),
                _cell(
                    f"{item.get('ref')} -> {item.get('mount')} ({item.get('tree_id')})"
                ),
            )
    return table


def _transfer_text(row: Mapping[str, Any]) -> str:
    """One transfer_event row (V0.5 G5): hits from the cache, or a source's share."""
    tree = str(row.get("tree_id") or "")[:19]
    source = row.get("source")
    bytes_ = _num(row.get("bytes"))
    ms = _num(row.get("duration_ms"))
    units = row.get("units")
    size = DASH if bytes_ is None else f"{bytes_ / 1024**2:.1f} MiB"
    took = DASH if ms is None else f"{ms / 1000:.2f} s"
    if source == "cache":
        return (
            f"{tree} cache hits={units} ({size}) staged in {took} "
            f"resumed_chunks={row.get('resumed_chunks')} refetched={row.get('failures')}"
        )
    dropped = row.get("dropped")
    return f"{tree} from {source}: {size} in {took}, units={units}" + (
        f" dropped={dropped}" if dropped else ""
    )
