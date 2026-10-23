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

* ``ml dataset [--out FILE] [--summary]`` (**admin**, V0.15 G1, ``GET
  /v1/planner/ml/dataset``): the ML training dataset - one row per matched
  (decision, outcome) run and learnable term, features from the decision's
  replay, target ``ln(actual / raw)``. Without ``--out`` (or with
  ``--summary``): its summary (rows per term and family, skips by reason,
  digest). ``--out FILE``: the Parquet file (new file only, its digest
  checked) and ``FILE.json``, the sidecar - written only when the summary
  names the same digest (the dataset did not change in between).

* ``ml compare --dataset FILE [--terms a,b] [--algorithms a,b] [--with-family]
  [--ablations]
  [--min-rows N] [--min-families N] [--min-effect X] [--out DIR] [--timeout S]``
  (V0.15 G2, D353; ``--timeout`` V0.15 G3, default 3600 s -> exit 1): offline,
  no controller needed - per term, does any learned model predict the
  residual better than the calibration on held-out families? Runs ``python
  -m meshforge.controller.ml.train compare`` in a child process with this
  interpreter (the CLI never imports the controller - an import-linter
  contract; the child needs the ``controller`` and ``ml`` extras: exit 2 with
  the install hint without them), prints the per-term table and keeps
  ``report.json`` / ``report.md`` in ``--out`` (default: a temporary
  directory, removed).

``--json`` prints the API document (docs/CLI.md section 3). Every value from
the controller goes through the renderer's cell / echo scrubbing.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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
    "ml_ablation_table",
    "ml_app",
    "ml_compare_lines",
    "ml_compare_table",
    "ml_dataset_lines",
    "ml_eval_lines",
    "ml_missing_libraries",
    "planner_app",
    "reports_table",
]

planner_app = typer.Typer(
    name="planner",
    help="The planner's calibration, offline evaluation (V0.14) and ML data (V0.15).",
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
    lines.extend(latency_lines(report.get("latency")))
    lines.extend(ml_eval_lines(report.get("ml")))
    return lines


def ml_eval_lines(ml: Any) -> list[str]:
    """V0.15 G5a (D369/D370): the ML criteria of a report (none for a
    version 1 report) - one line per verdict, criterion 2's families, and
    how often ML was used in the chosen plans (pure)."""
    if not isinstance(ml, Mapping):
        return []
    lines: list[str] = []
    if ml.get("status") != "evaluated":
        lines.append(
            f"ML: {render.echo(ml.get('status'))} - {render.echo(ml.get('reason'))}"
        )
    for verdict in ml.get("verdicts") or []:
        if not isinstance(verdict, Mapping):
            continue
        status = render.echo(verdict.get("status"))
        if verdict.get("would_be") is not None:
            status += f" (would be {render.echo(verdict.get('would_be'))})"
        lines.append(
            f"{render.echo(verdict.get('name'))}: {status} - "
            f"{render.echo(verdict.get('detail'))}"
        )
        for family in verdict.get("families") or []:
            if not isinstance(family, Mapping):
                continue
            lines.append(
                f"  {render.echo(family.get('family'))}: ML "
                f"{_num(family.get('mean_regret_ml_pct'), 2)} % vs calibrated "
                f"{_num(family.get('mean_regret_calibrated_pct'), 2)} % over "
                f"{render.echo(family.get('common_groups'))} groups - "
                f"{render.echo(family.get('status'))}"
            )
    fallback = ml.get("fallback")
    total = fallback.get("total") if isinstance(fallback, Mapping) else None
    if ml.get("status") == "evaluated" and isinstance(total, Mapping):
        terms = total.get("terms")
        reasons = (
            ", ".join(
                f"{render.echo(k)} {render.echo(v)}" for k, v in sorted(terms.items())
            )
            if isinstance(terms, Mapping) and terms
            else _DASH
        )
        lines.append(
            f"ML in the chosen plans: used rate {_num(total.get('used_rate'), 2)} "
            f"({reasons}); decisions without a fold model "
            f"{render.echo(total.get('no_fold_model'))}, predictor errors "
            f"{render.echo(total.get('predictor_error'))}"
        )
    return lines


def latency_lines(latency: Any) -> list[str]:
    """V0.14 G6 (D324): the report's decision-latency block (pure; none for
    a report stored before G6)."""
    if not isinstance(latency, Mapping):
        return []
    lines = [
        f"decision latency: {render.echo(latency.get('decisions'))} dispatched "
        f"decision(s) timed ({render.echo(latency.get('without_latency'))} without "
        f"a time); p50 {_num(latency.get('p50_ms'), 2)} ms, p99 "
        f"{_num(latency.get('p99_ms'), 2)} ms, max {_num(latency.get('max_ms'), 2)} "
        f"ms; {render.echo(latency.get('over_budget'))} over the "
        f"{_num(latency.get('budget_ms'), 0)} ms budget"
    ]
    for title, key in (
        ("by snapshot size", "by_node_count"),
        ("by live nodes", "by_live_node_count"),  # D332
    ):
        buckets = latency.get(key)
        if isinstance(buckets, Mapping) and buckets:
            lines.append(
                f"  {title}: "
                + "; ".join(
                    f"{render.echo(nodes)} nodes: {render.echo(b.get('decisions'))} "
                    f"decision(s), p99 {_num(b.get('p99_ms'), 2)} ms"
                    for nodes, b in buckets.items()
                    if isinstance(b, Mapping)
                )
            )
    judged = latency.get("judged")
    if isinstance(judged, Mapping):
        what = "live nodes" if latency.get("judged_on") == "live_nodes" else "nodes"
        lines.append(
            f"  criterion 3 judges snapshots of >= "
            f"{render.echo(latency.get('node_count'))} {what}: "
            f"{render.echo(judged.get('decisions'))} decision(s), p99 "
            f"{_num(judged.get('p99_ms'), 2)} ms (at least "
            f"{render.echo(latency.get('min_decisions'))} needed)"
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
        "CRITERION 3",
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
            _verdict_cell(row.get("verdicts"), 3),  # V0.14 G6 (D324)
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


# ------------------------------------------------------- V0.15 G1 (D333)

ml_app = typer.Typer(
    name="ml",
    help="The ML planner's training data (V0.15).",
    no_args_is_help=True,
    rich_markup_mode=None,
)
planner_app.add_typer(ml_app)


def ml_dataset_lines(doc: Mapping[str, Any]) -> list[str]:
    """The dataset summary as lines (pure; tests read them)."""
    counts = doc.get("counts")
    counts = counts if isinstance(counts, Mapping) else {}
    lines = [
        f"ML dataset v{render.echo(doc.get('dataset_version'))} (features "
        f"v{render.echo(doc.get('feature_version'))}): "
        f"{render.echo(doc.get('rows'))} rows from {render.echo(counts.get('runs'))} "
        f"matched runs ({render.echo(counts.get('runs_with_rows'))} with rows)",
        f"digest {render.echo(doc.get('digest'))}",
    ]
    if doc.get("truncated") is True:
        lines.append(
            f"truncated: only the newest {render.echo(doc.get('max_runs'))} runs "
            "were read ([controller.planner] ml_dataset_max_runs)"
        )
    for label, key in (
        ("rows per term", "rows_by_term"),
        ("per family", "rows_by_family"),
    ):
        part = counts.get(key)
        if isinstance(part, Mapping):
            lines.append(
                f"{label}: "
                + ", ".join(
                    f"{render.echo(k)} {render.echo(v)}" for k, v in part.items()
                )
            )
    skipped = counts.get("skipped")
    if isinstance(skipped, Mapping):
        shown = [f"{render.echo(k)} {render.echo(v)}" for k, v in skipped.items() if v]
        lines.append("skipped: " + (", ".join(shown) if shown else "none"))
    if "node_failures" in counts:  # V0.15 G2 (D354)
        lines.append(
            f"node-caused failures: {render.echo(counts.get('node_failures'))} "
            "(the events a failure classifier would learn from)"
        )
    return lines


def _sidecar_text(doc: Mapping[str, Any]) -> str:
    """The sidecar as the controller writes it (sorted keys, 2-space indent)."""
    return json.dumps(doc, sort_keys=True, indent=2, allow_nan=False) + "\n"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


@ml_app.command("dataset")
def ml_dataset(
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="Write the dataset (Parquet) to this new file and its summary "
            "to FILE.json.",
        ),
    ] = None,
    summary_only: Annotated[
        bool,
        typer.Option(
            "--summary", help="Only print the summary (the default without --out)."
        ),
    ] = False,
    json_output: cli.JsonOption = False,
) -> None:
    """The ML training dataset: matched runs -> per-term residual rows (admin)."""
    if out is not None and summary_only:
        cli._die("--out and --summary exclude each other", CliExit.USAGE_ERROR)
    settings = cli._settings()
    if out is None:
        with cli._client(settings) as client:
            doc = cli._fetch(client.planner_ml_dataset_summary)
        if json_output:
            cli._echo_json(doc)
            return
        for line in ml_dataset_lines(doc):
            render.line(line)
        return
    sidecar = out.with_name(out.name + ".json")
    for target in (out, sidecar):
        if os.path.lexists(target):  # a dangling symlink too: never followed
            cli._die(f"{target} exists; choose a new file", CliExit.USAGE_ERROR)
    with cli._client(settings) as client:
        try:
            size, header = cli._fetch(lambda: client.planner_ml_dataset(out))
        except FileExistsError:
            cli._die(f"{out} exists; choose a new file", CliExit.USAGE_ERROR)
        except OSError as exc:
            cli._die(
                f"cannot write {out}: {exc.strerror or type(exc).__name__}",
                CliExit.FAILURE,
            )
        written = _sha256_file(out)
        if header is not None and written != header:
            out.unlink(missing_ok=True)
            cli._die(
                f"{out}: the download does not match its digest; removed - retry",
                CliExit.FAILURE,
            )
        doc = cli._fetch(client.planner_ml_dataset_summary)
    if doc.get("digest") != written:
        cli._die(
            "the dataset changed between the file and its summary (new runs "
            f"finished); {out} kept, {sidecar} not written - run again",
            CliExit.FAILURE,
        )
    try:
        with sidecar.open("x", encoding="utf-8") as handle:
            handle.write(_sidecar_text(doc))
    except FileExistsError:
        cli._die(f"{sidecar} exists; choose a new file", CliExit.USAGE_ERROR)
    except OSError as exc:
        cli._die(
            f"cannot write {sidecar}: {exc.strerror or type(exc).__name__}",
            CliExit.FAILURE,
        )
    if json_output:
        cli._echo_json(
            {
                "file": str(out),
                "sidecar": str(sidecar),
                "bytes": size,
                "rows": doc.get("rows"),
                "digest": written,
            }
        )
        return
    for line in ml_dataset_lines(doc):
        render.line(line)
    render.line(f"wrote {out} ({size} bytes) and {sidecar}")


# ------------------------------------------------------- V0.15 G2 (D353)

ML_COMPARE_LIBRARIES: Final = ("pyarrow", "numpy", "sklearn", "lightgbm", "xgboost")
"""What the comparison child imports (the ``controller`` + ``ml`` extras);
checked with ``find_spec`` - nothing is imported here."""
ML_EXTRA_HINT: Final = (
    "install the controller and ml extras on this machine: "
    "`uv sync --extra controller --extra ml` (or `pip install "
    "'meshforge[controller,ml]'`)"
)
_TRAIN_MODULE: Final = "meshforge.controller.ml.train"
ML_COMPARE_TIMEOUT_S: Final = 3600.0
"""V0.15 G3 (G2 tech debt): `ml compare --timeout` default (one hour)."""
_COMPARE_HEADER: Final = (
    "term",
    "rows",
    "fam",
    "status",
    "selected",
    "MALE ml",
    "MALE cal",
    "vs cal",
    "cover",
    "usable",
)


def ml_missing_libraries() -> list[str]:
    return [m for m in ML_COMPARE_LIBRARIES if importlib.util.find_spec(m) is None]


def _pct(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _DASH
    return f"{100.0 * value:.1f}%"


def _words(value: Any) -> str:
    """``not_separable`` -> ``not separable`` (wraps by word in a narrow
    table; ``--json`` keeps the report's own value)."""
    return _DASH if value is None else render.echo(value).replace("_", " ")


def ml_compare_table(doc: Mapping[str, Any]) -> Table:
    """The per-term table of a comparison report (pure; tests read it)."""
    table = render.table(*_COMPARE_HEADER, title="ML vs calibration, held-out families")
    terms = doc.get("terms")
    for term in terms if isinstance(terms, list) else []:
        if not isinstance(term, Mapping):
            continue
        shipped = term.get("shipped")
        shipped = shipped if isinstance(shipped, Mapping) else {}
        config = shipped.get("config")
        pooled = term.get("pooled")
        pooled = pooled if isinstance(pooled, Mapping) else {}
        verdict = term.get("vs_calibration")
        verdict = verdict if isinstance(verdict, Mapping) else {}
        table.add_row(
            render.cell(term.get("term")),
            render.cell(term.get("rows")),
            render.cell(term.get("families")),
            _words(term.get("status")),
            render.cell(config.get("name") if isinstance(config, Mapping) else None),
            _num(pooled.get("male_model")),
            _num(pooled.get("male_calibration")),
            _words(verdict.get("verdict")),
            _pct(pooled.get("coverage")),
            _pct(pooled.get("usable_rate")),
        )
    return table


_ABLATION_HEADER: Final = ("term", "dropped", "fam", "dMALE", "mean [CI]", "verdict")


def ml_ablation_table(doc: Mapping[str, Any]) -> Table:
    """V0.15 G5a (D372): the feature-group ablations of a comparison report
    (pure; ``dMALE`` = MALE without the group - MALE full, > 0 = it helps)."""
    table = render.table(
        *_ABLATION_HEADER, title="Feature-group ablations (held-out families)"
    )
    block = doc.get("ablations")
    terms = block.get("terms") if isinstance(block, Mapping) else None
    for term, groups in sorted(terms.items()) if isinstance(terms, Mapping) else []:
        for row in groups if isinstance(groups, list) else []:
            if not isinstance(row, Mapping):
                continue
            evaluated = row.get("status") == "evaluated"
            table.add_row(
                render.cell(term),
                render.cell(row.get("group")),
                render.cell(row.get("families")),
                _num(row.get("delta_male_pooled")),
                f"{_num(row.get('mean'))} [{_num(row.get('low'))}, "
                f"{_num(row.get('high'))}]"
                if evaluated
                else _DASH,
                _words(row.get("verdict")) if evaluated else _words(row.get("reason")),
            )
    return table


def ml_compare_lines(doc: Mapping[str, Any]) -> list[str]:
    """The header and footer lines of a comparison report (pure)."""
    dataset = doc.get("dataset")
    dataset = dataset if isinstance(dataset, Mapping) else {}
    lines = [
        f"dataset {render.echo(dataset.get('digest'))} "
        f"({render.echo(dataset.get('rows'))} rows, dataset "
        f"v{render.echo(dataset.get('dataset_version'))}, features "
        f"v{render.echo(dataset.get('feature_version'))})",
        "held-out = leave one family out; the model is chosen on the other "
        "families only; 'vs cal' = paired family bootstrap of the log error",
    ]
    failures = doc.get("failure_classifier")
    if isinstance(failures, Mapping):
        status = failures.get("status")
        n = render.echo(failures.get("node_failures"))
        limit = render.echo(failures.get("min_events"))
        if status == "not_trained":
            lines.append(f"failure classifier: not trained, {n} < {limit} events")
        elif status == "eligible":
            lines.append(f"failure classifier: eligible ({n} >= {limit} events)")
        else:
            lines.append("failure classifier: unknown (no matching dataset sidecar)")
    return lines


@ml_app.command("compare")
def ml_compare(
    dataset: Annotated[
        Path,
        typer.Option(
            "--dataset",
            help="A dataset file from `compute planner ml dataset --out FILE` "
            "(its FILE.json sidecar is read when present).",
            dir_okay=False,
        ),
    ],
    terms: Annotated[
        str | None, typer.Option("--terms", help="Only these terms (a,b,...).")
    ] = None,
    algorithms: Annotated[
        str | None,
        typer.Option(
            "--algorithms",
            help="Only these learned algorithms (ridge, knn, tree, forest, "
            "lightgbm, xgboost); the baselines always run.",
        ),
    ] = None,
    with_family: Annotated[
        bool,
        typer.Option("--with-family", help="Ablation: add the family as a feature."),
    ] = False,
    ablations: Annotated[
        bool,
        typer.Option(
            "--ablations",
            help="Also re-run the evaluation without each feature group and "
            "report how much each one matters (about 7x the time: raise "
            "--timeout).",
        ),
    ] = False,
    min_rows: Annotated[
        int | None,
        typer.Option("--min-rows", min=1, help="Data floor per term (default 200)."),
    ] = None,
    min_families: Annotated[
        int | None,
        typer.Option(
            "--min-families", min=1, help="Family floor per term (default 3)."
        ),
    ] = None,
    min_effect: Annotated[
        float | None,
        typer.Option(
            "--min-effect",
            min=0.0,
            help="`better` needs the held-out log-error gain's CI beyond this "
            "(default 0.02, about 2 %).",
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="Keep report.json and report.md in this directory.",
            file_okay=False,
        ),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            min=1.0,
            help="Stop the comparison after this many seconds (exit 1; the "
            "child process is killed).",
        ),
    ] = ML_COMPARE_TIMEOUT_S,
    json_output: cli.JsonOption = False,
) -> None:
    """Compare learned models with the calibration per term (offline, no
    controller): held-out families, conformal intervals, OOD."""
    if not dataset.is_file():
        cli._die(f"{dataset}: no such dataset file", CliExit.USAGE_ERROR)
    missing = ml_missing_libraries()
    if missing:
        cli._die(
            f"ml compare needs {', '.join(missing)} (not installed): {ML_EXTRA_HINT}",
            CliExit.USAGE_ERROR,
        )
    with tempfile.TemporaryDirectory(prefix="meshforge-ml-") as scratch:
        target = out if out is not None else Path(scratch)
        argv = [
            sys.executable,
            "-m",
            _TRAIN_MODULE,
            "compare",
            "--dataset",
            str(dataset),
            "--out",
            str(target),
        ]
        for flag, value in (
            ("--terms", terms),
            ("--algorithms", algorithms),
            ("--min-rows", min_rows),
            ("--min-families", min_families),
            ("--min-effect", min_effect),
        ):
            if value is not None:
                argv += [flag, str(value)]
        if with_family:
            argv.append("--with-family")
        if ablations:
            argv.append("--ablations")
        try:
            # subprocess.run kills the child when the timeout expires
            result = subprocess.run(  # noqa: S603 - this interpreter, our own module
                argv, capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            cli._die(
                f"the comparison did not finish in {timeout:g} s and was stopped "
                "(use --timeout, --terms or --algorithms)",
                CliExit.FAILURE,
            )
        if result.returncode != 0:
            message = result.stderr.strip() or "the comparison failed"
            code = (
                CliExit.USAGE_ERROR
                if result.returncode == CliExit.USAGE_ERROR
                else CliExit.FAILURE
            )
            cli._die(message, code)
        try:
            doc = json.loads((target / "report.json").read_text(encoding="utf-8"))
            timings = json.loads((target / "timings.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            cli._die(f"cannot read the report: {type(exc).__name__}", CliExit.FAILURE)
    if json_output:
        cli._echo_json(doc)
        return
    for line in ml_compare_lines(doc):
        render.line(line)
    render.show(ml_compare_table(doc))
    if isinstance(doc.get("ablations"), Mapping):
        render.show(ml_ablation_table(doc))
    render.line(f"compared in {render.echo(timings.get('total_s'))} s")
    if out is not None:
        render.line(f"wrote {out / 'report.json'} and {out / 'report.md'}")
