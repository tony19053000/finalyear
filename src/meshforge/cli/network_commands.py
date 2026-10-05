"""``compute network [--json]`` - the measured network matrix (V0.6 G4, D111).

Prints ``GET /v1/network`` as:

* a **matrix**: one row per downloading node (``FROM``), one column per data
  source (``TO``: the node whose peer server answered); a cell is
  ``<bandwidth> <rtt> <age>``, ``(stale)`` when it is older than the
  controller's ``stale_after_s`` (the planner then ignores it), ``fail:<code>``
  for a pair whose probes failed and that has no measurement, ``-`` for a pair
  not measured yet;
* the **controller -> node** bandwidths taken from transfer history (no probe);
* one status line: probes started / measured / failed, in flight, budget use.

Node ids are shortened to their last 8 characters in the matrix (the legend
lists them in full). Every string from the controller goes through
:func:`~.render._cell`. ``--json`` prints the API answer as is.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, Final

import typer
from rich.table import Column, Table

from meshforge.cli import app as cli

__all__ = ["format_age", "format_rate", "matrix_table", "network"]

_DASH: Final = "-"
_SHORT: Final = 8


def format_rate(value: Any) -> str:
    """Bytes/s as ``12.3 MB/s`` (decimal units, like network gear)."""
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return _DASH
    for unit, size in (("GB/s", 1e9), ("MB/s", 1e6), ("kB/s", 1e3)):
        if value >= size:
            return f"{value / size:.1f} {unit}"
    return f"{value:.0f} B/s"


def format_age(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return _DASH
    if value < 120:
        return f"{value:.0f}s"
    if value < 7200:
        return f"{value / 60:.0f}m"
    return f"{value / 3600:.1f}h"


def _short(node_id: str) -> str:
    return node_id[-_SHORT:]


def matrix_table(payload: Mapping[str, Any]) -> Table:
    from meshforge.cli.render import _cell

    links = [x for x in payload.get("links") or [] if isinstance(x, Mapping)]
    failures = [x for x in payload.get("failures") or [] if isinstance(x, Mapping)]
    nodes = sorted(
        {str(x.get("src_node_id")) for x in links + failures}
        | {str(x.get("dst_node_id")) for x in links + failures}
    )
    cells: dict[tuple[str, str], str] = {}
    for link in links:
        text = (
            f"{format_rate(link.get('bw_bytes_s'))} "
            f"{link.get('rtt_ms', 0):.1f}ms {format_age(link.get('age_s'))}"
        )
        if link.get("stale"):
            text += " (stale)"
        failure = link.get("last_failure")
        if isinstance(failure, Mapping):
            text += f" fail:{failure.get('error')}"
        cells[(str(link.get("src_node_id")), str(link.get("dst_node_id")))] = text
    for failure in failures:
        key = (str(failure.get("src_node_id")), str(failure.get("dst_node_id")))
        cells.setdefault(key, f"fail:{failure.get('error')}")
    table = Table(
        "FROM \\ TO",
        *(Column(header=_cell(_short(n))) for n in nodes),
        title="measured links (FROM downloads from TO's peer server)",
        title_justify="left",
    )
    for src in nodes:
        row = [
            "x" if src == dst else _cell(cells.get((src, dst), _DASH)) for dst in nodes
        ]
        table.add_row(_cell(_short(src)), *row)
    return table


def _controller_table(payload: Mapping[str, Any]) -> Table | None:
    from meshforge.cli.render import _cell

    rows = [x for x in payload.get("controller_links") or [] if isinstance(x, Mapping)]
    if not rows:
        return None
    table = Table(
        "NODE",
        "CONTROLLER -> NODE",
        "SAMPLES",
        "LAST",
        title="controller links (from transfer history)",
        title_justify="left",
    )
    for row in rows:
        table.add_row(
            _cell(str(row.get("node_id"))),
            format_rate(row.get("bw_bytes_s")),
            _cell(str(row.get("samples"))),
            format_age(row.get("age_s")),
        )
    return table


def _status_line(payload: Mapping[str, Any]) -> str:
    prober = payload.get("prober")
    if not isinstance(prober, Mapping):
        return ""
    in_flight = prober.get("in_flight")
    return (
        f"probes: {'on' if prober.get('enabled') else 'off'} - started "
        f"{prober.get('started')}, measured {prober.get('measured')}, failed "
        f"{prober.get('failed')} (timed out {prober.get('timed_out')}), in flight "
        f"{len(in_flight) if isinstance(in_flight, list) else 0}; budget "
        f"{format_rate(prober.get('budget_used_bytes'))[:-2]}/"
        f"{format_rate(prober.get('budget_bytes_per_min'))[:-2]} per minute; stale "
        f"after {format_age(payload.get('stale_after_s'))}"
    )


def network(
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the API answer as JSON.")
    ] = False,
) -> None:
    """The measured network matrix: bandwidth, RTT and age of every probed
    node pair, controller links from transfer history, and probe status."""
    from meshforge.cli.render import _cell

    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(client.network)
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    console = cli._out()
    links = payload.get("links") or []
    if not links and not payload.get("failures"):
        console.print("no node pair measured yet (probes run every few minutes)")
    else:
        console.print(matrix_table(payload))
        nodes = sorted(
            {str(x.get("src_node_id")) for x in links if isinstance(x, Mapping)}
            | {str(x.get("dst_node_id")) for x in links if isinstance(x, Mapping)}
        )
        for node in nodes:
            console.print(_cell(f"  {_short(node)} = {node}"))
    controller = _controller_table(payload)
    if controller is not None:
        console.print(controller)
    line = _status_line(payload)
    if line:
        console.print(_cell(line))
