"""``compute telemetry export`` - the telemetry dataset as one Parquet file (V0.9 G1).

``compute telemetry export [--campaign ID] [--since TIME] -o FILE.parquet``
downloads ``GET /v1/telemetry/export`` (admin only) into ``FILE`` - created,
never overwritten (an existing file is a usage error). The file is the run
records of telemetry schema v1 (``docs/TELEMETRY.md``); the same records give
the same bytes (D222), so two exports of a finished campaign compare equal.
``--json`` prints ``{"file", "bytes", "rows", "schema_version"}``.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from pathlib import Path
from typing import Annotated

import typer

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit

__all__ = ["telemetry_app"]

telemetry_app = typer.Typer(
    name="telemetry",
    help="Export the telemetry dataset (run records) as Parquet (V0.9; admin).",
    no_args_is_help=True,
    rich_markup_mode=None,
)


def _since(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError:
        cli._die(
            "--since: not an ISO-8601 time (e.g. 2026-10-17T00:00:00Z)",
            CliExit.USAGE_ERROR,
        )
    if parsed.tzinfo is None:
        cli._die(
            "--since: give a UTC offset (e.g. 2026-10-17T00:00:00Z)",
            CliExit.USAGE_ERROR,
        )
    return parsed.isoformat()


@telemetry_app.command("export")
def export(
    output: Annotated[
        Path, typer.Option("-o", "--output", help="Parquet file to create.")
    ],
    campaign: Annotated[
        str | None, typer.Option("--campaign", help="Only this campaign's records.")
    ] = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="Only attempts created at or after (ISO 8601)."),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """Download the run records as one Parquet file (admin only)."""
    when = _since(since)
    if output.exists():
        cli._die(f"{output} exists; choose a new file", CliExit.USAGE_ERROR)
    settings = cli._settings()
    with cli._client(settings) as client:
        try:
            size, rows = cli._fetch(
                lambda: client.export_telemetry(output, campaign=campaign, since=when)
            )
        except BaseException:
            with contextlib.suppress(OSError):
                output.unlink()  # never leave a half-written dataset behind
            raise
    if json_output:
        cli._echo_json(
            {"file": str(output), "bytes": size, "rows": rows, "schema_version": 1}
        )
        return
    render.line(
        f"wrote {output}: {rows if rows is not None else '?'} records, {size} bytes"
    )
