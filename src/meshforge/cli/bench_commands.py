"""``compute bench workloads [--json]`` - the benchmark workload library (V0.9 G2).

Lists every workload family (``meshforge.bench.workloads``, D214) with its kind,
environment, parameters (bounds and the default) and the smallest valid
combination with its run-time hint on the reference mid laptop. ``--json``
prints the same as one document (``families[].params_schema`` is the family's
JSON Schema) plus ``schema_version`` (docs/CLI.md section 3).

Local only: nothing is sent to the controller, and nothing is run - the hint is
the family's cost model, not a measurement. Later V0.9 groups add
``compute bench plan|run|report`` to the same ``bench`` group.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any

import typer
from rich import box
from rich.table import Table
from rich.text import Text

from meshforge.cli import render
from meshforge.cli import shared as cli

__all__ = ["bench_app", "params_summary", "workloads", "workloads_document"]

bench_app = typer.Typer(
    name="bench",
    help="Benchmark workloads and campaigns (V0.9).",
    no_args_is_help=True,
    rich_markup_mode=None,
)


def _bound(spec: Mapping[str, Any]) -> str:
    if "enum" in spec:
        return "|".join(str(v) for v in spec["enum"])
    if "const" in spec:
        return str(spec["const"])
    low, high = spec.get("minimum"), spec.get("maximum")
    if low is not None and high is not None:
        return f"{low:,}..{high:,}".replace(",", "_")
    return str(spec.get("type", "?"))


def params_summary(schema: Mapping[str, Any]) -> str:
    """``n 256..4096 (2048), dtype float64|float32 (float64)`` from a JSON Schema."""
    parts = []
    for name, spec in schema.get("properties", {}).items():
        default = spec.get("default")
        shown = f" ({default:_})" if isinstance(default, int) else f" ({default})"
        parts.append(f"{name} {_bound(spec)}{shown if default is not None else ''}")
    return "\n".join(parts)


def workloads_document() -> dict[str, Any]:
    """The ``--json`` document (without ``schema_version``)."""
    from meshforge.bench.workloads import (
        MAX_RUNTIME_S,
        MID_LAPTOP,
        MIN_RUNTIME_S,
        list_families,
    )

    families = [family.describe() for family in list_families()]
    return {
        "families": families,
        "count": len(families),
        "runtime_window_s": [MIN_RUNTIME_S, MAX_RUNTIME_S],
        "reference_hw": asdict(MID_LAPTOP),
    }


def _table(document: Mapping[str, Any]) -> Table:
    table = Table(
        title="Benchmark workload families",
        box=box.ASCII,
        header_style="bold",
        pad_edge=False,
    )
    for column in ("FAMILY", "KIND", "ENV", "PARAMETERS (range, default)", "HINT"):
        table.add_column(column)
    for family in document["families"]:
        table.add_row(
            Text(family["name"]),
            Text(family["kind"]),
            Text(
                family["env_base"] + ("\n+gpu" if family["gpu"] == "required" else "")
            ),
            Text(params_summary(family["params_schema"])),
            Text(f"~{family['smallest_hint_s']:g} s"),
        )
    low, high = document["runtime_window_s"]
    table.caption = (
        f"{document['count']} families. The defaults are the smallest valid run; "
        "HINT is its rough compute time on the reference mid laptop. A run must "
        f"take {low:g}-{high:g} s there (docs/BENCHMARKS.md)."
    )
    return table


@bench_app.command()
def workloads(json_output: cli.JsonOption = False) -> None:
    """List the benchmark workload families and their parameters (local, no
    controller needed)."""
    document = workloads_document()
    if json_output:
        cli._echo_json(document)
        return
    render.show(_table(document))
