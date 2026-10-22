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

* ``eval --campaign ID [--campaign ID ...] [--holdout FAMILY] [--allow-leak]
  [--md PATH]`` (**admin**, V0.14 G4, ``POST /v1/planner/eval-reports``):
  evaluate the campaigns' labelled groups - every policy replayed on what the
  planner saw, regret vs the best measured configuration, calibrated rows on
  leave-one-family-out refits - store the report and print the verdicts and
  the per-policy table; ``--md`` also writes the stored report's Markdown.
* ``reports [--limit N]`` (G4 fix round 1, ``GET /v1/planner/eval-reports``):
  the stored reports, newest first - id, time, campaigns, held-out family,
  leaked, evaluated groups and both verdicts.

``--json`` prints the API document (docs/CLI.md section 3). Every value from
the controller goes through the renderer's cell / echo scrubbing.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final

import typer
from rich.table import Table

from meshforge.cli import render
from meshforge.cli import shared as cli
from meshforge.cli.exits import CliExit

__all__ = [
    "calibration_lines",
    "eval_lines",
    "eval_table",
    "factor_table",
    "planner_app",
    "reports_table",
]

planner_app = typer.Typer(
    name="planner",
    help="The planner's calibration and offline evaluation (V0.14).",
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
        # V0.14 G4 (D319): an accepted row may carry a note (regret check skipped)
        label = "rejected" if calibration.get("status") == "rejected" else "note"
        lines.append(f"{label}: {render.echo(calibration.get('reason'))}")
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


# ------------------------------------------------- V0.14 G4: compute planner eval


def _ci(row: Mapping[str, Any]) -> str:
    ci = row.get("ci_pct")
    if not isinstance(ci, Mapping) or ci.get("low") is None:
        return _DASH
    return f"[{_num(ci.get('low'), 2)}, {_num(ci.get('high'), 2)}]"


def eval_table(report: Mapping[str, Any]) -> Table:
    """The per-policy regret table of one report (pure; tests read it)."""
    table = render.table(
        "POLICY",
        "COUNTED",
        "MEAN REGRET %",
        "95% CI",
        "MEAN MS",
        "WITHIN %",
        "UNMEASURED",
        "NO REPLAY",
        title="regret vs the best measured configuration",
    )
    for row in report.get("policies") or []:
        if not isinstance(row, Mapping):
            continue
        table.add_row(
            render.cell(row.get("key")),
            render.cell(row.get("counted_groups")),
            _num(row.get("mean_regret_pct"), 2),
            _ci(row),
            _num(row.get("mean_regret_ms"), 1),
            _num(row.get("within_threshold_pct"), 1),
            render.cell(row.get("unmeasured_groups")),
            render.cell(row.get("no_replay_groups")),
        )
    return table


def eval_lines(record: Mapping[str, Any]) -> list[str]:
    """The header and verdict lines of one stored report (pure)."""
    report = record.get("report")
    report = report if isinstance(report, Mapping) else {}
    counts = report.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    lines: list[str] = []
    if report.get("leaked"):
        lines.append(
            "LEAKED: --allow-leak used the live calibration, which may have seen "
            "these runs; no verdict is a claim"
        )
    campaigns = ", ".join(render.echo(c) for c in report.get("campaign_ids") or ())
    lines += [
        f"report {render.echo(record.get('id'))}: campaigns {campaigns}; "
        f"held-out family {render.echo(report.get('holdout') or 'each (leave-one-out)')}",
        f"groups {render.echo(counts.get('groups'))}: evaluated "
        f"{render.echo(counts.get('evaluated_groups'))}, no replay "
        f"{render.echo(counts.get('no_replay_groups'))}; records "
        f"{render.echo(counts.get('records_used'))} of "
        f"{render.echo(counts.get('records'))} replayed",
    ]
    for verdict in report.get("verdicts") or []:
        if not isinstance(verdict, Mapping):
            continue
        status = render.echo(verdict.get("status"))
        if verdict.get("would_be") is not None:
            status += f" (would be {render.echo(verdict.get('would_be'))})"
        lines.append(
            f"criterion {render.echo(verdict.get('criterion'))}: {status} - "
            f"{render.echo(verdict.get('detail'))}"
        )
        for c in verdict.get("comparisons") or []:  # D322: paired criterion 2
            if not isinstance(c, Mapping):
                continue
            ci = (
                _DASH
                if c.get("ci_low") is None
                else f"[{_num(c.get('ci_low'), 2)}, {_num(c.get('ci_high'), 2)}]"
            )
            lines.append(
                f"  vs {render.echo(c.get('key'))}: difference "
                f"{_num(c.get('mean_diff_pct'), 2)} % points, CI {ci} over "
                f"{render.echo(c.get('common_groups'))} common groups; "
                f"{render.echo(c.get('unmeasured_groups'))} unmeasured"
            )
    return lines


def _verdict_cell(verdicts: Any, criterion: int) -> str:
    for verdict in verdicts if isinstance(verdicts, list) else []:
        if isinstance(verdict, Mapping) and verdict.get("criterion") == criterion:
            return str(render.echo(verdict.get("status")))
    return _DASH


def reports_table(doc: Mapping[str, Any]) -> Table:
    """The stored reports, newest first (pure; tests read it)."""
    table = render.table(
        "ID",
        "CREATED",
        "CAMPAIGNS",
        "HOLDOUT",
        "LEAKED",
        "GROUPS",
        "CRITERION 1",
        "CRITERION 2",
        title="planner evaluation reports",
    )
    for row in doc.get("reports") or []:
        if not isinstance(row, Mapping):
            continue
        campaigns = row.get("campaign_ids")
        table.add_row(
            render.cell(row.get("id")),
            render.cell(row.get("created_at")),
            render.cell(
                ", ".join(str(c) for c in campaigns)
                if isinstance(campaigns, list)
                else None
            ),
            render.cell(row.get("holdout") or _DASH),
            "yes" if row.get("leaked") else "no",
            render.cell(row.get("evaluated_groups")),
            _verdict_cell(row.get("verdicts"), 1),
            _verdict_cell(row.get("verdicts"), 2),
        )
    return table


@planner_app.command("reports")
def reports(
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, max=200, help="At most this many reports."),
    ] = 50,
    json_output: cli.JsonOption = False,
) -> None:
    """List the stored planner evaluation reports, newest first."""
    settings = cli._settings()
    with cli._client(settings) as client:
        doc = cli._fetch(lambda: client.planner_eval_reports(limit=limit))
    if json_output:
        cli._echo_json(doc)
        return
    if not doc.get("reports"):
        render.line("no evaluation reports yet (compute planner eval)")
        return
    render.show(reports_table(doc))


@planner_app.command("eval")
def evaluate(
    campaign: Annotated[
        list[str],
        typer.Option("--campaign", help="A campaign to evaluate (repeat for several)."),
    ],
    holdout: Annotated[
        str | None,
        typer.Option(
            "--holdout", help="Evaluate only this workload family (held out)."
        ),
    ] = None,
    allow_leak: Annotated[
        bool,
        typer.Option(
            "--allow-leak",
            help="Debugging: use the live calibration (every verdict 'leaked').",
        ),
    ] = False,
    md: Annotated[
        Path | None,
        typer.Option("--md", help="Also write the report as Markdown to this file."),
    ] = None,
    json_output: cli.JsonOption = False,
) -> None:
    """Evaluate benchmark campaigns: every policy's regret vs the oracle (admin)."""
    ids = [cli._checked_campaign_id(c) for c in campaign]
    if md is not None and md.exists():
        cli._die(f"{md} exists; choose a new file", CliExit.USAGE_ERROR)
    settings = cli._settings()
    with cli._client(settings) as client:
        doc = cli._fetch(
            lambda: client.planner_eval(
                campaign_ids=ids, holdout=holdout, allow_leak=allow_leak
            )
        )
        text = None
        if md is not None:
            report_id = doc.get("id")
            if isinstance(report_id, int) and not isinstance(report_id, bool):
                text = cli._fetch(lambda: client.planner_eval_markdown(report_id))
    if md is not None and text is not None:
        try:
            with md.open("x", encoding="utf-8") as handle:
                handle.write(text)
        except FileExistsError:
            cli._die(f"{md} exists; choose a new file", CliExit.USAGE_ERROR)
        except OSError as exc:
            cli._die(
                f"cannot write {md}: {exc.strerror or type(exc).__name__}",
                CliExit.FAILURE,
            )
    if json_output:
        cli._echo_json(doc)
        return
    for line in eval_lines(doc):
        render.line(line)
    report = doc.get("report")
    if isinstance(report, Mapping):
        render.show(eval_table(report))
    if md is not None and text is not None:
        render.line(f"wrote {md}")
