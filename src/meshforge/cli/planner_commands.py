"""``compute planner calibrate`` / ``compute planner calibration`` - the planner's
calibration (V0.14 G2, D293/D301/D306).

* ``calibrate`` (**admin**, ``POST /v1/planner/calibrate``): fit a new version
  from the recorded runs now, gate it against the active one on the held-out
  runs, and print the new version (``active`` or ``rejected`` with the
  reason) and its per-term factor table - or why nothing was written (too few
  matched runs).
* ``calibration [--version N]`` (``GET /v1/planner/calibration``): one
  version's factors (default: the active one) - per term and key (node +
  family, node, global) the factor, ``n`` and the raw median ratio - and what
  the planner plans with now (``auto`` / ``off`` / ``pinned``).

``--json`` prints the API document (docs/CLI.md section 3). Every value from
the controller goes through the renderer's cell / echo scrubbing.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final

import typer
from rich.table import Table

from meshforge.cli import render
from meshforge.cli import shared as cli

__all__ = ["calibration_lines", "factor_table", "planner_app"]

planner_app = typer.Typer(
    name="planner",
    help="The planner's calibration: fit a new version, show factors (V0.14).",
    no_args_is_help=True,
    rich_markup_mode=None,
)

_SHORT: Final = 8
_DASH: Final = "-"


def _num(value: Any, digits: int = 3) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _DASH
    return f"{value:.{digits}f}"


def _key(row: Mapping[str, Any]) -> str:
    level = str(row.get("key_level"))
    node = row.get("node_id")
    family = row.get("family")
    if level == "global":
        return "global"
    short = str(node)[-_SHORT:] if node else "?"
    if level == "node_family":
        return f"{short} / {family}"
    return short


def factor_table(calibration: Mapping[str, Any]) -> Table:
    """The per-term factor table of one version (pure; tests read it)."""
    table = render.table(
        "TERM",
        "KEY",
        "LEVEL",
        "FACTOR",
        "N",
        "MEDIAN",
        title=f"calibration v{render.echo(calibration.get('version'))} factors",
    )
    for row in calibration.get("factors") or []:
        if not isinstance(row, Mapping):
            continue
        table.add_row(
            render.cell(row.get("term")),
            render.cell(_key(row)),
            render.cell(row.get("key_level")),
            f"x {_num(row.get('factor'))}",
            render.cell(row.get("n")),
            _num(row.get("median")),
        )
    return table


def _in_use(doc: Mapping[str, Any]) -> str:
    in_use = doc.get("in_use")
    if not isinstance(in_use, Mapping):
        return "planner uses: unknown"
    mode = str(in_use.get("mode"))
    version = in_use.get("in_use_version")
    if mode == "off":
        return "planner uses: no calibration ([controller.planner] calibration = off)"
    if mode == "pinned":
        return (
            f"planner uses: pinned version {render.echo(in_use.get('pinned_version'))}"
        )
    if version is None:
        return "planner uses: no calibration yet (auto: no active version)"
    return f"planner uses: version {render.echo(version)} (auto)"


def calibration_lines(calibration: Mapping[str, Any]) -> list[str]:
    """The header lines of one version (pure; tests read them)."""
    fit = calibration.get("fit_inputs")
    fit = fit if isinstance(fit, Mapping) else {}
    lines = [
        f"version {render.echo(calibration.get('version'))}: "
        f"{render.echo(calibration.get('status'))} "
        f"(created {render.echo(calibration.get('created_at'))})",
        f"fitted on {render.echo(fit.get('pairs'))} runs, "
        f"{render.echo(fit.get('held_out'))} held out; window "
        f"{render.echo(fit.get('window'))}, k {render.echo(fit.get('k'))}, "
        f"clamp {render.echo(fit.get('clamp'))}",
        f"held-out error |ln(pred/actual)|: new "
        f"{_num(calibration.get('holdout_error_new'), 4)} vs active "
        f"{_num(calibration.get('holdout_error_active'), 4)}"
        + (
            ""
            if fit.get("active_version") is None
            else f" (v{render.echo(fit.get('active_version'))})"
        ),
    ]
    if calibration.get("reason"):
        lines.append(f"rejected: {render.echo(calibration.get('reason'))}")
    if calibration.get("readable") is False:
        lines.append("its factors cannot be read: never used for planning")
    return lines


def _show(calibration: Mapping[str, Any]) -> None:
    for text in calibration_lines(calibration):
        render.line(text)
    if calibration.get("factors"):
        render.show(factor_table(calibration))
    else:
        render.line("no factors")


@planner_app.command("calibrate")
def calibrate(json_output: cli.JsonOption = False) -> None:
    """Fit a new calibration version now and gate it (admin only)."""
    settings = cli._settings()
    with cli._client(settings) as client:
        doc = cli._fetch(client.planner_calibrate)
    if json_output:
        cli._echo_json(doc)
        return
    created = doc.get("created")
    if not isinstance(created, Mapping):
        render.line(
            f"nothing written: {render.echo(doc.get('skipped'))} "
            f"({render.echo(doc.get('matched_runs'))} matched runs)"
        )
    else:
        if doc.get("superseded_version") is not None:
            render.line(
                f"supersedes version {render.echo(doc.get('superseded_version'))}"
            )
        _show(created)
    render.line(_in_use(doc))


@planner_app.command("calibration")
def calibration(
    version: Annotated[
        int | None,
        typer.Option(
            "--version", min=1, help="This version (default: the active one)."
        ),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """Show one calibration version's factors (default: the active one)."""
    settings = cli._settings()
    with cli._client(settings) as client:
        doc = cli._fetch(lambda: client.planner_calibration(version=version))
    if json_output:
        cli._echo_json(doc)
        return
    shown = doc.get("calibration")
    if isinstance(shown, Mapping):
        _show(shown)
    else:
        render.line("no active calibration version (compute planner calibrate)")
    not_calibrated = doc.get("not_calibrated")
    if isinstance(not_calibrated, Mapping) and not_calibrated:
        render.line(
            "never scaled: "
            + ", ".join(
                f"{render.echo(k)} ({render.echo(v)})"
                for k, v in sorted(not_calibrated.items())
            )
        )
    render.line(_in_use(doc))
