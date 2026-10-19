"""``compute bench labels CAMPAIGN_ID [-o FILE] [--json]`` - benchmark labels (V0.9 G5, D218).

Asks ``GET /v1/bench/campaigns/{id}/labels`` (admin) and prints one line per
group: the group, its family and condition, the label (``best`` / ``tie`` /
``insufficient`` / ``pending``), the best configuration (strategy @ node) or
the tied set / the reason, and the margin - how much slower the runner-up's
median total time is, in %. ``--json`` prints the API's document plus
``schema_version`` (docs/CLI.md section 3). ``-o FILE`` downloads the labels
table as Parquet instead (created, never overwritten); with ``--json`` it
prints ``{"file", "bytes", "rows", "labels_version"}``.

How a label is decided: docs/TELEMETRY.md "Labels".
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.bench_commands import bench_app
from meshforge.cli.exits import CliExit

__all__ = ["best_cell", "margin_cell"]

LABELS_VERSION: Final = 1
_COLUMNS: Final = ("GROUP", "FAMILY", "CONDITION", "LABEL", "BEST", "MARGIN %")


def margin_cell(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "-"
    return f"{value:.1f}"


def best_cell(group: Mapping[str, Any]) -> str:
    """``mode1 @ node_x`` for best, the tied configs for a tie, else the reason."""
    label = group.get("label")
    if label == "best":
        return f"{group.get('best_strategy')} @ {group.get('best_node_id')}"
    if label == "tie":
        configs = {c.get("config_id"): c for c in group.get("configs") or []}
        tied = []
        for cid in group.get("tied_config_ids") or []:
            c = configs.get(cid) or {}
            tied.append(f"{c.get('strategy', '?')} @ {c.get('node_id', cid)}")
        return "tie: " + ", ".join(tied)
    return str(group.get("reason") or "-")


def _download(campaign_id: str, output: Path, json_output: bool) -> None:
    if os.path.lexists(output):  # a dangling symlink too: never followed
        cli._die(f"{output} exists; choose a new file", CliExit.USAGE_ERROR)
    settings = cli._settings()
    with cli._client(settings) as client:
        try:
            size, rows = cli._fetch(
                lambda: client.bench_labels_parquet(campaign_id, output)
            )
        except FileExistsError:
            cli._die(f"{output} exists; choose a new file", CliExit.USAGE_ERROR)
        except OSError as exc:
            cli._die(
                f"cannot write {output}: {exc.strerror or type(exc).__name__}",
                CliExit.FAILURE,
            )
    if json_output:
        cli._echo_json(
            {
                "file": str(output),
                "bytes": size,
                "rows": rows,
                "labels_version": LABELS_VERSION,
            }
        )
        return
    render.line(
        f"wrote {output}: {rows if rows is not None else '?'} label rows, {size} bytes"
    )


@bench_app.command()
def labels(
    campaign_id: Annotated[str, typer.Argument(help="The campaign (camp_...).")],
    output: Annotated[
        Path | None,
        typer.Option(
            "-o", "--output", help="Download the labels as this Parquet file."
        ),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """Per-group labels of a campaign: best configuration, tie, insufficient or
    pending (admin)."""
    checked = cli._checked_campaign_id(campaign_id)
    if output is not None:
        _download(checked, output, json_output)
        return
    settings = cli._settings()
    with cli._client(settings) as client:
        document = cli._fetch(lambda: client.bench_labels(checked))
    if json_output:
        cli._echo_json(document)
        return
    groups = document.get("groups") or []
    table = render.table(*_COLUMNS, title=f"Campaign {checked}: labels")
    for group in groups:
        table.add_row(
            render.cell(group.get("group_id")),
            render.cell(group.get("family")),
            render.cell(group.get("condition")),
            render.cell(group.get("label")),
            render.cell(best_cell(group)),
            render.cell(margin_cell(group.get("margin_pct"))),
        )
    render.show(table)
    counts = document.get("counts") or {}
    render.line(
        f"{len(groups)} groups: "
        + ", ".join(
            f"{kind} {counts.get(kind, 0)}"
            for kind in ("best", "tie", "insufficient", "pending")
        )
        + f" (from {document.get('rows', 0)} run records)"
    )
