"""``compute explain JOB [--attempt N] [--json]`` (V0.6 G2, D112; criterion 3).

Prints, per planner decision of the job (``GET /v1/jobs/{id}/decisions``):
the policy and outcome; **every candidate** - rank, node, Local/offload, slot,
the policy's score and the predicted total, or the rejection code and reason;
the predicted terms of each ranked candidate **with their source**
(``measured`` / ``history`` / ``scaled`` / ``assumed`` - checklist 18: nothing
assumed reads as measured); and, once the attempt ran, predicted vs actual
(``transfer``~``stage``, ``env``~``env``, ``compute``~``run``,
``output``~``collect``, ``queue``~``queue``).

Every string from the controller goes through :func:`~.render._cell` (control
characters flattened). ``--json`` prints the API answer as is.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Annotated, Any, Final

import typer
from rich.table import Table

from meshforge.cli import app as cli

__all__ = ["decision_tables", "explain"]

_TERMS: Final = ("queue", "transfer", "env", "compute", "output")
_ACTUAL_PHASE: Final = {
    "queue": "queue",
    "transfer": "stage",
    "env": "env",
    "compute": "run",
    "output": "collect",
}
_DASH: Final = "-"


def _seconds(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return _DASH
    return f"{value:.2f}s"


def _term_cell(term: Any) -> str:
    if not isinstance(term, Mapping):
        return _DASH
    return f"{_seconds(term.get('seconds'))} ({term.get('source')})"


def _candidates_table(decision: Mapping[str, Any]) -> Table:
    from meshforge.cli.render import _cell

    table = Table(
        "RANK",
        "NODE",
        "MODE",
        "SLOT",
        "SCORE",
        "PREDICTED",
        "REASON",
        title=_cell(
            f"decision {decision.get('id')} - attempt {decision.get('attempt_no')}: "
            f"{decision.get('outcome')} (policy {decision.get('policy')})"
        ),
        title_justify="left",
    )
    candidates = decision.get("candidates")
    for c in candidates if isinstance(candidates, list) else []:
        if not isinstance(c, Mapping):
            continue
        estimate = c.get("estimate")
        total = estimate.get("total_s") if isinstance(estimate, Mapping) else None
        reason = (
            f"{c.get('reject_code')}: {c.get('reject_text')}"
            if c.get("reject_code")
            else "chosen"
            if c.get("rank") == 1
            else "feasible"
        )
        score = c.get("score")
        table.add_row(
            _cell(c.get("rank")),
            _cell(c.get("node_id")),
            _cell(c.get("mode")),
            _cell(c.get("slot")),
            _DASH if score is None else _cell(f"{score:g}"),
            _seconds(total),
            _cell(reason),
        )
    return table


def _terms_table(decision: Mapping[str, Any]) -> Table | None:
    from meshforge.cli.render import _cell

    candidates = decision.get("candidates")
    ranked = [
        c
        for c in (candidates if isinstance(candidates, list) else [])
        if isinstance(c, Mapping) and isinstance(c.get("estimate"), Mapping)
    ]
    if not ranked:
        return None
    table = Table("NODE", *(t.upper() for t in _TERMS), "TOTAL", title="predicted")
    for c in ranked:
        estimate = c["estimate"]
        terms = estimate.get("terms") if isinstance(estimate, Mapping) else None
        terms = terms if isinstance(terms, Mapping) else {}
        table.add_row(
            _cell(c.get("node_id")),
            *(_cell(_term_cell(terms.get(t))) for t in _TERMS),
            _seconds(estimate.get("total_s")),
        )
    return table


def _actual_table(decision: Mapping[str, Any]) -> Table | None:
    from meshforge.cli.render import _cell

    actual = decision.get("actual")
    if not isinstance(actual, Mapping):
        return None
    chosen = next(
        (
            c
            for c in decision.get("candidates") or []
            if isinstance(c, Mapping) and c.get("rank") == 1
        ),
        None,
    )
    estimate = chosen.get("estimate") if isinstance(chosen, Mapping) else None
    terms = estimate.get("terms") if isinstance(estimate, Mapping) else None
    terms = terms if isinstance(terms, Mapping) else {}
    phases = actual.get("phases_ms")
    phases = phases if isinstance(phases, Mapping) else {}
    table = Table(
        "TERM",
        "PREDICTED",
        "ACTUAL PHASE",
        "ACTUAL",
        title=_cell(
            f"actual: attempt {actual.get('attempt_id')} on {actual.get('node_id')} "
            f"- {actual.get('state')}"
            + (f" ({actual.get('failure_type')})" if actual.get("failure_type") else "")
        ),
        title_justify="left",
    )
    for term in _TERMS:
        phase = _ACTUAL_PHASE[term]
        ms = phases.get(phase)
        if phase not in phases:
            shown = "not yet"
        elif ms is None:
            shown = "skipped"
        else:
            shown = _seconds(int(ms) / 1000.0)
        table.add_row(term, _cell(_term_cell(terms.get(term))), phase, shown)
    return table


def decision_tables(payload: Mapping[str, Any]) -> list[Table]:
    """Every table of ``compute explain`` for one decisions answer."""
    tables: list[Table] = []
    decisions = payload.get("decisions")
    for decision in decisions if isinstance(decisions, list) else []:
        if not isinstance(decision, Mapping):
            continue
        tables.append(_candidates_table(decision))
        extras = (_terms_table(decision), _actual_table(decision))
        tables.extend(t for t in extras if t is not None)
    return tables


def explain(
    job_id: Annotated[str, typer.Argument(help="The job to explain.")],
    attempt: Annotated[
        int | None,
        typer.Option("--attempt", min=1, help="Only the decisions of attempt N."),
    ] = None,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the API answer as JSON.")
    ] = False,
) -> None:
    """Why the planner placed (or has not placed) a job: every candidate,
    rejections with reasons, predicted times with their source, and the actual
    phase times once it ran."""
    from meshforge.cli.job_commands import _checked_job_id

    checked = _checked_job_id(job_id)
    settings = cli._settings()
    with cli._client(settings) as client:
        payload = cli._fetch(lambda: client.decisions(checked, attempt=attempt))
    if as_json:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        return
    tables = decision_tables(payload)
    console = cli._out()
    if not tables:
        console.print(
            "no planner decision recorded for this job"
            + (f" (attempt {attempt})" if attempt is not None else "")
            + " - jobs submitted before V0.6 have none"
        )
        return
    for table in tables:
        console.print(table)
