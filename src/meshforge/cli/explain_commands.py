"""``compute explain JOB [--attempt N] [--json]`` (V0.6 G2, D112; criterion 3).

Prints, per planner decision of the job (``GET /v1/jobs/{id}/decisions``):
the policy and outcome; **every candidate** - rank, node, Local/offload, slot,
the policy's score and the predicted total, or the rejection code and reason;
the predicted terms of each ranked candidate **with their source** (and,
V0.6 G4, the node(s) or ``controller`` a transfer's bandwidth came from - one
per missing tree, D131 - with the age of the controller's transfer history)
(``measured`` / ``history`` / ``scaled`` / ``assumed`` - checklist 18: nothing
assumed reads as measured); and, once the attempt ran, predicted vs actual
(``transfer``~``stage``, ``env``~``env``, ``compute``~``run``,
``output``~``collect``, ``queue``~``queue``).

Every string from the controller goes through :func:`~.render._cell` (control
characters flattened). ``--json`` prints the API answer + ``schema_version`` (docs/CLI.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final

import typer
from rich.table import Table

from meshforge.cli import app as cli
from meshforge.cli import render

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


_MAX_ORIGINS: Final = 3


def _origin(detail: Mapping[str, Any]) -> str | None:
    """Where one bandwidth came from: ``controller`` (with the age of its
    latest transfer when known - D131: that history has no staleness rule),
    the last 8 characters of a probed holder's node id, or ``default`` for an
    assumed bandwidth."""
    from meshforge.cli.render import _num, format_age

    source = detail.get("bw_from")
    if source == "controller":
        age = _num(detail.get("last_transfer_age_s"))
        return "controller" + (
            f" (last transfer {format_age(age)} ago)" if age is not None else ""
        )
    if isinstance(source, str) and source:
        return source[-8:]
    return "default" if detail.get("bw_source") == "assumed" else None


def _origins(detail: Mapping[str, Any]) -> list[str]:
    """Every distinct source of a transfer term, in the job's tree order: the
    term's own when one source carried it all, else one per ``items`` entry
    (D131: each missing tree is costed by its own source)."""
    if isinstance(detail.get("bw_from"), str) and detail.get("bw_from"):
        one = _origin(detail)
        return [one] if one else []
    items = detail.get("items")
    found: list[str] = []
    for item in items if isinstance(items, list) else []:
        label = _origin(item) if isinstance(item, Mapping) else None
        if label and label not in found:
            found.append(label)
    return found


def _clamped(detail: Mapping[str, Any]) -> bool:
    items = detail.get("items")
    return detail.get("bw_clamped") is True or any(
        isinstance(i, Mapping) and i.get("bw_clamped") is True
        for i in (items if isinstance(items, list) else [])
    )


def _term_cell(term: Any) -> str:
    """``<seconds> (<source>)``; a transfer also names where its bandwidth came
    from (V0.6 G4: ``from <node...>`` for a probed link, ``from controller``
    with the age of its latest transfer for transfer history; several sources
    are joined with ``+``, D131), a cached environment says so, a bandwidth
    raised to the configured floor is flagged, and probed links ignored as
    stale are counted (checklist 18: a stale probe never counts as a
    measurement)."""
    if not isinstance(term, Mapping):
        return _DASH
    detail = term.get("detail")
    detail = detail if isinstance(detail, Mapping) else {}
    extra = ""
    origins = _origins(detail)
    if origins:
        shown = origins[:_MAX_ORIGINS]
        more = len(origins) - len(shown)
        extra = ", from " + " + ".join(shown) + (f" + {more} more" if more else "")
    elif detail.get("env_cached") is True:
        extra = ", cached"
    if _clamped(detail):
        extra += ", at the bandwidth floor"
    stale = detail.get("stale_links_ignored")
    if isinstance(stale, list) and stale:
        extra += f", {len(stale)} stale probe(s) ignored"
    return f"{_seconds(term.get('seconds'))} ({term.get('source')}{extra})"


def _score_cell(score: Any) -> Any:
    """The policy's score as a short number; a stored value that is not a
    number is shown as it is (through :func:`~.render._cell`), never formatted
    (tester T-10: no traceback from odd REST data)."""
    from meshforge.cli.render import _cell, _num

    if score is None:
        return _DASH
    number = _num(score)
    return _cell(score) if number is None else _cell(f"{number:g}")


def _candidates_table(decision: Mapping[str, Any]) -> Table:
    from meshforge.cli.render import _cell

    table = render.table(
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
        table.add_row(
            _cell(c.get("rank")),
            _cell(c.get("node_id")),
            _cell(c.get("mode")),
            _cell(c.get("slot")),
            _score_cell(c.get("score")),
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
    table = render.table(
        "NODE", *(t.upper() for t in _TERMS), "TOTAL", title="predicted"
    )
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
    from meshforge.cli.render import _cell, _num

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
    table = render.table(
        "TERM",
        "PREDICTED",
        "ACTUAL PHASE",
        "ACTUAL",
        title=_cell(
            f"actual: attempt {actual.get('attempt_id')} on {actual.get('node_id')} "
            f"- {actual.get('state')}"
            + (f" ({actual.get('failure_type')})" if actual.get("failure_type") else "")
        ),
    )
    for term in _TERMS:
        phase = _ACTUAL_PHASE[term]
        ms = phases.get(phase)
        number = _num(ms)
        if phase not in phases:
            shown = "not yet"
        elif ms is None:
            shown = "skipped"
        elif number is None:  # not a number: never a traceback (V0.8 G6)
            shown = _DASH
        else:
            shown = _seconds(number / 1000.0)
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
    as_json: cli.JsonOption = False,
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
        cli._echo_json(payload)
        return
    tables = decision_tables(payload)
    if not tables:
        render.line(
            "no planner decision recorded for this job"
            + (f" (attempt {attempt})" if attempt is not None else "")
            + " - jobs submitted before V0.6 have none"
        )
        return
    for table in tables:
        render.show(table)
