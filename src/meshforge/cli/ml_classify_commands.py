"""``compute planner ml classify`` - the 0.15.10 classifier experiment, report
only (V0.15 G6a, D343, D375, D376).

"Why not just classify?" - trains direct classifiers (most common class,
logistic regression, LightGBM) that map a job and the cluster it meets to
the winning configuration class, on the benchmark campaigns' labelled
groups, and scores them with the evaluator's regret next to the planner's
cost regression:

* ``--campaign ID`` (repeatable; **admin**): the controller builds the
  examples (``POST /v1/planner/ml/classifier-examples``: every labelled group
  re-planned with the evaluator's held-out calibration); ``--save-examples
  FILE`` keeps that document;
* ``--examples FILE``: run on a saved document instead (offline, no
  controller).

The training runs here, in a child process (``python -m
meshforge.controller.ml.train classify``), like ``compute planner ml
compare`` - it needs the ``controller`` and ``ml`` extras on this machine
(exit 2 with the install hint otherwise). A classifier is never registered
and never used by the planner. Fewer than 10 labelled groups or 3 families
-> ``too_few_groups`` with the counts (no accuracy is printed, D376).

Writes ``classify.json`` + ``classify.md`` into ``--out`` when given;
``--json`` prints the JSON document.
"""

from __future__ import annotations

import json
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
from meshforge.cli.planner_commands import ML_EXTRA_HINT, ml_app, ml_missing_libraries

__all__ = ["classify_lines", "classify_table"]

_TRAIN_MODULE: Final = "meshforge.controller.ml.train"
CLASSIFY_TIMEOUT_S: Final = 1800.0
_HEADER: Final = (
    "model",
    "counted",
    "unmeasured",
    "mean regret %",
    "within 15 %",
    "accuracy",
    "regret - cost model [CI]",
    "verdict",
)


def _num(value: Any, digits: int = 2) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "-"
    return f"{value:.{digits}f}"


def classify_lines(section: Mapping[str, Any]) -> list[str]:
    """The section's header lines (pure; tests read them)."""
    families = section.get("families")
    count = len(families) if isinstance(families, list) else 0
    lines = [
        f"classifier experiment (report only): {render.echo(section.get('groups'))} "
        f"labelled group(s) in {count} family(ies) - "
        f"{render.echo(section.get('status'))}"
    ]
    if section.get("status") != "evaluated":
        lines.append(render.echo(section.get("reason")))
    return lines


def classify_table(section: Mapping[str, Any]) -> Table:
    """Every model next to the cost regression (accuracy next to regret,
    never instead of it)."""
    table = Table(*_HEADER)
    cost = section.get("cost_model")
    rows = list(section.get("models") or [])
    if isinstance(cost, Mapping):
        rows.append(cost)
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        paired = row.get("vs_cost_model")
        if isinstance(paired, Mapping):
            delta = (
                f"{_num(paired.get('mean_diff_pct'))} [{_num(paired.get('ci_low'))}, "
                f"{_num(paired.get('ci_high'))}]"
            )
            verdict = render.echo(paired.get("verdict"))
            accuracy = _num(row.get("accuracy"), 3)
        else:
            delta, verdict, accuracy = "(reference)", "-", "-"
        table.add_row(
            render.cell(row.get("model")),
            render.cell(row.get("counted_groups")),
            render.cell(row.get("unmeasured_groups")),
            _num(row.get("mean_regret_pct")),
            _num(row.get("within_threshold_pct"), 1),
            accuracy,
            delta,
            verdict,
        )
    return table


def _fetch_examples(campaigns: list[str]) -> Mapping[str, Any]:
    settings = cli._settings()
    with cli._client(settings) as client:
        return cli._fetch(
            lambda: client.planner_ml_classifier_examples(campaign_ids=campaigns)
        )


@ml_app.command("classify")
def ml_classify(
    campaign: Annotated[
        list[str] | None,
        typer.Option("--campaign", help="A benchmark campaign (repeatable; admin)."),
    ] = None,
    examples: Annotated[
        Path | None,
        typer.Option(
            "--examples",
            help="Run on this saved examples document (no controller).",
            dir_okay=False,
        ),
    ] = None,
    save_examples: Annotated[
        Path | None,
        typer.Option(
            "--save-examples",
            help="Also write the controller's examples document to this new file.",
            dir_okay=False,
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out",
            "-o",
            help="Keep classify.json and classify.md in this directory.",
            file_okay=False,
        ),
    ] = None,
    timeout: Annotated[
        float,
        typer.Option(
            "--timeout",
            min=1.0,
            help="Stop the training after this many seconds (exit 1).",
        ),
    ] = CLASSIFY_TIMEOUT_S,
    json_output: cli.JsonOption = False,
) -> None:
    """The 0.15.10 classifier experiment: classify the winning configuration
    instead of predicting costs, scored by the same regret (report only)."""
    if (examples is None) == (not campaign):
        cli._die(
            "give --campaign ID (repeatable) or --examples FILE, not both",
            CliExit.USAGE_ERROR,
        )
    if examples is not None and save_examples is not None:
        cli._die("--save-examples needs --campaign", CliExit.USAGE_ERROR)
    if examples is not None and not examples.is_file():
        cli._die(f"{examples}: no such examples file", CliExit.USAGE_ERROR)
    if save_examples is not None and save_examples.exists():
        cli._die(f"{save_examples} exists; choose a new file", CliExit.USAGE_ERROR)
    missing = ml_missing_libraries()
    if missing:
        cli._die(
            f"ml classify needs {', '.join(missing)} (not installed): {ML_EXTRA_HINT}",
            CliExit.USAGE_ERROR,
        )
    with tempfile.TemporaryDirectory(prefix="meshforge-ml-classify-") as scratch:
        source = examples
        if source is None:
            doc = _fetch_examples(sorted(set(campaign or [])))
            source = Path(scratch) / "examples.json"
            text = json.dumps(doc, sort_keys=True)
            source.write_text(text, encoding="utf-8")
            if save_examples is not None:
                try:
                    with save_examples.open("x", encoding="utf-8") as handle:
                        handle.write(text + "\n")
                except OSError as exc:
                    cli._die(
                        f"cannot write {save_examples}: "
                        f"{exc.strerror or type(exc).__name__}",
                        CliExit.FAILURE,
                    )
        target = out if out is not None else Path(scratch) / "out"
        argv = [
            sys.executable,
            "-m",
            _TRAIN_MODULE,
            "classify",
            "--examples",
            str(source),
            "--out",
            str(target),
        ]
        try:
            # subprocess.run kills the child when the timeout expires
            result = subprocess.run(  # noqa: S603 - this interpreter, our own module
                argv, capture_output=True, text=True, check=False, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            cli._die(
                f"the classifier experiment did not finish in {timeout:g} s and "
                "was stopped (use --timeout)",
                CliExit.FAILURE,
            )
        if result.returncode != 0:
            message = result.stderr.strip() or "the classifier experiment failed"
            code = (
                CliExit.USAGE_ERROR
                if result.returncode == CliExit.USAGE_ERROR
                else CliExit.FAILURE
            )
            cli._die(message, code)
        try:
            report = json.loads((target / "classify.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            cli._die(f"cannot read the report: {type(exc).__name__}", CliExit.FAILURE)
    if json_output:
        cli._echo_json(report)
        return
    section = report.get("classifier") if isinstance(report, Mapping) else None
    section = section if isinstance(section, Mapping) else {}
    for line in classify_lines(section):
        render.line(line)
    if section.get("status") == "evaluated":
        render.show(classify_table(section))
    if out is not None:
        render.line(f"wrote {out / 'classify.json'} and {out / 'classify.md'}")
