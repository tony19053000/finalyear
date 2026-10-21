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

A **split job** (V0.10 G3, D265) also gets a split table: every candidate's
node set (a Mode 3 row is named ``<fastest node> +N``), its chunking, each
node's predicted item rate with its source, and the margin test - with a
title saying in one sentence why the job was or was not split. Its predicted
terms add ``items_fetch``, ``chunk_overhead``, ``tail`` and ``combine``.

Every string from the controller goes through :func:`~.render._cell` (control
characters flattened). ``--json`` prints the API answer + ``schema_version`` (docs/CLI.md).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Annotated, Any, Final

import typer
from rich.table import Table

from meshforge.cli import render
from meshforge.cli import shared as cli

__all__ = ["decision_tables", "explain"]

_TERMS: Final = ("queue", "transfer", "env", "compute", "output")
_SPLIT_TERMS: Final = ("items_fetch", "chunk_overhead", "tail", "combine")
"""V0.10 G3: the terms only a split job's candidates have."""
_RISK_TERMS: Final = ("risk",)
"""V0.14 G3 (D296): shown when a decision's candidates carry it."""
_MAX_SET_NODES: Final = 6
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
            _cell(_node_label(c)),
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
    split = any(_node_set(c) for c in ranked)
    names = (
        *_TERMS,
        *(
            t
            for t in (*_SPLIT_TERMS, *_RISK_TERMS)
            if any(_has_term(c, t) for c in ranked)
        ),
    )
    heads = ("NODE", "MODE") if split else ("NODE",)
    table = render.table(
        *heads, *(t.upper() for t in names), "TOTAL", title="predicted"
    )
    for c in ranked:
        estimate = c["estimate"]
        terms = estimate.get("terms") if isinstance(estimate, Mapping) else None
        terms = terms if isinstance(terms, Mapping) else {}
        lead = (_cell(_node_label(c)), _cell(c.get("mode")))
        table.add_row(
            *lead[: len(heads)],
            *(_cell(_term_cell(terms.get(t))) for t in names),
            _seconds(estimate.get("total_s")),
        )
    return table


def _has_term(candidate: Mapping[str, Any], name: str) -> bool:
    estimate = candidate.get("estimate")
    terms = estimate.get("terms") if isinstance(estimate, Mapping) else None
    return isinstance(terms, Mapping) and name in terms


def _node_set(candidate: Mapping[str, Any]) -> list[str]:
    nodes = candidate.get("node_set")
    return [str(n) for n in nodes] if isinstance(nodes, list) else []


def _node_label(candidate: Mapping[str, Any]) -> Any:
    """A Mode 3 set row is named by its first (fastest) node and how many
    more (``node_a +2``); every other row by its node."""
    nodes = _node_set(candidate)
    if candidate.get("mode") == "split" and len(nodes) > 1:
        return f"{candidate.get('node_id')} +{len(nodes) - 1}"
    return candidate.get("node_id")


def _nodes_cell(candidate: Mapping[str, Any]) -> str:
    nodes = _node_set(candidate)
    shown = nodes[:_MAX_SET_NODES]
    more = len(nodes) - len(shown)
    inactive = candidate.get("inactive")
    idle = {str(n) for n in inactive} if isinstance(inactive, list) else set()
    text = ", ".join(f"{n} (idle)" if n in idle else n for n in shown)
    return text + (f" + {more} more" if more else "")


def _chunks_cell(candidate: Mapping[str, Any]) -> str:
    from meshforge.cli.render import _num

    size, count = _num(candidate.get("chunk_items")), _num(candidate.get("chunks"))
    if size is None or count is None:
        return _DASH
    return f"{count:g} x {size:g} items"


def _rate_cell(candidate: Mapping[str, Any]) -> str:
    from meshforge.cli.render import _num

    rate = _num(candidate.get("rate_items_s"))
    if rate is None:
        return _DASH
    return f"{rate:.4g}/s ({candidate.get('rate_source')})"


def _margin_cell(candidate: Mapping[str, Any]) -> str:
    """``pass: 120.88s <= 272.70s`` for a set row; the code of a rejected one."""
    if candidate.get("reject_code"):
        return str(candidate.get("reject_code"))
    test = candidate.get("margin_test")
    if not isinstance(test, Mapping):
        return _DASH
    verdict = "pass" if test.get("passed") is True else "fail"
    sign = "<=" if test.get("passed") is True else ">"
    return (
        f"{verdict}: {_seconds(test.get('set_s'))} {sign} "
        f"{_seconds(test.get('threshold_s'))}"
    )


def _split_verdict(rows: list[Mapping[str, Any]]) -> str:
    """One sentence: why the job was (not) split (D265 margin test)."""
    from meshforge.cli.render import _num

    chosen = next((c for c in rows if c.get("rank") == 1), None)
    tests = [
        c.get("margin_test")
        for c in rows
        if c.get("mode") == "split" and isinstance(c.get("margin_test"), Mapping)
    ]
    refused = next(
        (c for c in rows if c.get("mode") == "split" and c.get("reject_code")), None
    )

    def margin(test: Mapping[str, Any]) -> str:
        number = _num(test.get("margin"))
        return _DASH if number is None else f"{number:g}"

    if chosen is not None and chosen.get("mode") == "split":
        test = chosen.get("margin_test")
        test = test if isinstance(test, Mapping) else {}
        return (
            f"split over {len(_node_set(chosen))} nodes: "
            f"{_seconds(test.get('set_s'))} <= (1 - {margin(test)}) x "
            f"{_seconds(test.get('best_single_s'))} "
            f"(best single node {test.get('best_single_node_id')})"
        )
    if refused is not None:
        return f"not split: {refused.get('reject_text')}"
    best = tests[0] if tests else None
    if isinstance(best, Mapping) and best.get("passed") is True:
        # V0.14 G3 (D295): the margin test passed; the job's objective did not
        return (
            f"not split: the best set {_seconds(best.get('set_s'))} passed the "
            "margin test, but the job's objective ranked one node first"
        )
    if isinstance(best, Mapping):
        return (
            f"not split: the best set {_seconds(best.get('set_s'))} is above "
            f"{_seconds(best.get('threshold_s'))} = (1 - {margin(best)}) x "
            f"{_seconds(best.get('best_single_s'))} "
            f"(best single node {best.get('best_single_node_id')})"
        )
    return "not split: the job is pinned to one node"


def _pruned_sentence(rows: list[Mapping[str, Any]]) -> str:
    """V0.14 G3 (D294): which nodes no set was extended with, and why."""
    pruned = next((c.get("pruned") for c in rows if c.get("pruned")), None)
    if not isinstance(pruned, list) or not pruned:
        return ""
    parts = [
        f"{p.get('node_id')} (by {p.get('by')})"
        for p in pruned
        if isinstance(p, Mapping)
    ]
    return (
        "; left out of every set: "
        + ", ".join(parts)
        + " - the faster node would finish every item before it could start"
    )


def _split_table(decision: Mapping[str, Any]) -> Table | None:
    """V0.10 G3 (D265): a split job's candidates - node sets, chunking, each
    node's item rate and its source, the margin test."""
    from meshforge.cli.render import _cell

    candidates = decision.get("candidates")
    rows = [
        c
        for c in (candidates if isinstance(candidates, list) else [])
        if isinstance(c, Mapping) and _node_set(c)
    ]
    if not rows:
        return None
    table = render.table(
        "RANK",
        "MODE",
        "NODES",
        "CHUNKS",
        "RATE",
        "PREDICTED",
        "MARGIN TEST",
        title=_cell(_split_verdict(rows) + _pruned_sentence(rows)),
    )
    for c in rows:
        estimate = c.get("estimate")
        total = estimate.get("total_s") if isinstance(estimate, Mapping) else None
        table.add_row(
            _cell(c.get("rank")),
            _cell(c.get("mode")),
            _cell(_nodes_cell(c)),
            _cell(_chunks_cell(c)),
            _cell(_rate_cell(c)),
            _seconds(total),
            _cell(_margin_cell(c)),
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
        extras = (
            _split_table(decision),
            _terms_table(decision),
            _actual_table(decision),
        )
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
