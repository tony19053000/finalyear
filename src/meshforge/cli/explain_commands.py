"""``compute explain JOB [--attempt N] [--json]`` (V0.6 G2, D112; criterion 3).

Prints, per planner decision of the job (``GET /v1/jobs/{id}/decisions``):
the policy and outcome; **every candidate** - rank, node, Local/offload, slot,
the policy's score and the predicted total, or the rejection code and reason;
the predicted terms of each ranked candidate **with their source** (and,
V0.6 G4, the node(s) or ``controller`` a transfer's bandwidth came from - one
per missing tree, D131 - with the age of the controller's transfer history)
(``measured`` / ``history`` / ``scaled`` / ``assumed`` - checklist 18: nothing
assumed reads as measured); and, once the attempt ran, predicted vs actual
per term with the ratio (V0.14 G5, D320: the API's ``actual_s`` / ``ratio``,
the calibration's own term -> phase match) and the five measured phases.

**Parity with the Plan tab (V0.14 G5, criterion 4).** Every field this command
prints is a field of the decisions answer, and the dashboard's Plan tab shows
the same set (``tests/unit/test_v014_g5_explain_contract.py`` proves it on one
shared fixture). The sentences - the margin test, a term's note - are computed
by the controller with ``common/explain_text`` and printed as received; the
decision header names the objective, calibration version, replay, snapshot and
a pinned job's shadow pick.

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
from meshforge.common.explain_text import format_seconds, margin_sentence, term_note
from meshforge.common.ml_explain import (
    decision_ml,
    ml_decision_text,
    ml_line,
    term_ml,
)

__all__ = ["decision_tables", "explain"]

_TERMS: Final = ("queue", "transfer", "env", "compute", "output")
_SPLIT_TERMS: Final = ("items_fetch", "chunk_overhead", "tail", "combine")
"""V0.10 G3: the terms only a split job's candidates have."""
_RISK_TERMS: Final = ("risk",)
"""V0.14 G3 (D296): shown when a decision's candidates carry it."""
_MAX_SET_NODES: Final = 6
_PHASES: Final = ("queue", "stage", "env", "run", "collect")
"""The five measured phases of an attempt (``actual.phases_ms``)."""
_DASH: Final = "-"


def _seconds(value: Any) -> str:
    return format_seconds(value)


def _term_cell(term: Any, name: str | None = None) -> str:
    """``<seconds> (<source>, <note>, <calibration>)``. The note is the API's
    ``note`` (``common/explain_text.term_note``, the string the Plan tab shows:
    where a transfer's bandwidth came from - D131 -, a cached environment, the
    bandwidth floor, stale probes ignored - checklist 18 -, a risk term's
    p_fail); an answer without it (a stored row read directly) gets the same
    function's result. A calibrated term names its factor (D293)."""
    if not isinstance(term, Mapping):
        return _DASH
    note = term["note"] if "note" in term else term_note(term, name=name)
    extra = f", {note}" if isinstance(note, str) and note else ""
    extra += _calibration_note(term)  # V0.14 G2 (D293)
    extra += _ml_note(term)  # V0.15 G4 (D336)
    return f"{_seconds(term.get('seconds'))} ({term.get('source')}{extra})"


def _ml_note(term: Mapping[str, Any]) -> str:
    """V0.15 G4/G5b (D336, D373): ``, <ML line>`` - what the ML model did for
    the term (``common/ml_explain.ml_line``, the text the Plan tab renders):
    ``ML <raw> -> <seconds> (<version>, x<factor>, +-<spread> %)`` when it
    corrected it, ``ML not used: <reason> (...)`` when it did not, plus a Mode
    3 lane's rate. From the answer's typed ``ml``; a stored row read directly
    (no ``ml`` key) gets the same mapping of its ``detail.ml``. Empty for a
    decision planned without a model."""
    block = term["ml"] if "ml" in term else term_ml(term)
    line = ml_line(block)
    return "" if line is None else f", {line}"


def _calibration_block(term: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The API's typed ``calibration`` (V0.14 G5); a stored row's
    ``detail.calibration`` when the answer has no such key."""
    if "calibration" in term:
        block = term.get("calibration")
    else:
        detail = term.get("detail")
        block = detail.get("calibration") if isinstance(detail, Mapping) else None
    return block if isinstance(block, Mapping) else None


def _calibration_note(term: Mapping[str, Any]) -> str:
    """V0.14 G2 (D293): ``, x1.30 calibrated (n=14, node)`` for a term the
    planner scaled by a calibration factor; empty otherwise."""
    calibration = _calibration_block(term)
    if calibration is None:
        return ""
    factor = calibration.get("factor")
    if isinstance(factor, bool) or not isinstance(factor, int | float):
        return ""
    n = calibration.get("n")
    level = calibration.get("key_level")
    return (
        f", x{factor:.2f} calibrated (n={n if isinstance(n, int) else '?'}, "
        f"{level if isinstance(level, str) else '?'})"
    )


def _score_cell(score: Any) -> Any:
    """The policy's score as a short number; a stored value that is not a
    number is shown as it is (through :func:`~.render._cell`), never formatted
    (tester T-10: no traceback from odd REST data)."""
    from meshforge.cli.render import _cell, _num

    if score is None:
        return _DASH
    number = _num(score)
    return _cell(score) if number is None else _cell(f"{number:g}")


def _objective_note(decision: Mapping[str, Any]) -> str:
    """V0.14 G3 (D295): the job's resolved objective (absent on older rows)."""
    objective = decision.get("objective")
    return f", objective {objective}" if isinstance(objective, str) else ""


def _int_text(value: Any) -> str | None:
    return None if isinstance(value, bool) or not isinstance(value, int) else str(value)


def _facts_note(decision: Mapping[str, Any]) -> str:
    """V0.14 G5 (D300): the chosen node, calibration version, replay, when,
    snapshot digest and a pinned job's shadow pick - the Plan tab's header.
    V0.14 G6 (D324): how long the decision took (absent on older rows)."""
    calibration = _int_text(decision.get("calibration_version"))
    replay = _int_text(decision.get("replay_version"))
    chosen = decision.get("chosen_node_id")
    parts = [
        f"chosen {chosen}" if isinstance(chosen, str) and chosen else "none chosen",
        f"calibration v{calibration}" if calibration else "uncalibrated",
        f"replay v{replay}" if replay else "no replay",
    ]
    took = decision.get("decision_ms")  # V0.14 G6 (D324)
    if isinstance(took, int | float) and not isinstance(took, bool):
        parts.append(f"decided in {took:.2f} ms")
    created = decision.get("created_at")
    if isinstance(created, str) and created:
        parts.append(f"at {created}")
    digest = decision.get("snapshot_digest")
    if isinstance(digest, str) and digest:
        parts.append(f"snapshot {digest[:12]}")
    shadow = decision.get("shadow_chosen_node_id")
    if isinstance(shadow, str) and shadow:
        parts.append(
            f"unpinned the planner would pick {shadow} "
            f"({_seconds(decision.get('shadow_estimate_s'))})"
        )
    return " - " + ", ".join(parts)


def _ml_header(decision: Mapping[str, Any]) -> str | None:
    """V0.15 G5b (D373): the model set the decision was planned with and a
    whole-decision fallback (D363) - the answer's ``ml_model_version`` /
    ``ml_fallback``; a stored decision without the keys reads its rows."""
    if "ml_model_version" in decision or "ml_fallback" in decision:
        version, fallback = (
            decision.get("ml_model_version"),
            decision.get("ml_fallback"),
        )
    else:
        version, fallback = decision_ml(decision.get("candidates"))
    return ml_decision_text(version, fallback)


def _ml_table(decision: Mapping[str, Any]) -> Table | None:
    """V0.15 G5b (D373): one row - the model set and a whole-decision
    fallback (its own table: the candidates title is cut at the table's
    width)."""
    from meshforge.cli.render import _cell

    text = _ml_header(decision)
    if text is None:
        return None
    table = render.table("ML")
    table.add_row(_cell(text))
    return table


def _candidates_table(decision: Mapping[str, Any]) -> Table:
    from meshforge.cli.render import _cell

    table = render.table(
        "RANK",
        "NODE",
        "MODE",
        "SLOT",
        "FEASIBLE",
        "SCORE",
        "PREDICTED",
        "REASON",
        title=_cell(
            f"decision {decision.get('id')} - attempt {decision.get('attempt_no')}: "
            f"{decision.get('outcome')} (policy {decision.get('policy')})"
            + _objective_note(decision)
            + _facts_note(decision)
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
            _cell(_yes_no(c.get("feasible"))),
            _score_cell(c.get("score")),
            _seconds(total),
            _cell(reason),
        )
    return table


def _yes_no(value: Any) -> str:
    return "yes" if value is True else "no" if value is False else _DASH


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
            *(_cell(_term_cell(terms.get(t), t)) for t in names),
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
    start = _num(candidate.get("start_s"))
    return f"{rate:.4g}/s ({candidate.get('rate_source')})" + (
        f", first chunk at {_seconds(start)}" if start is not None else ""
    )


def _margin_cell(candidate: Mapping[str, Any]) -> str:
    """``pass: 41.00s <= 46.80s = (1 - 0.1) x 52.00s (node_a)`` for a set row
    (``, forced: <why>`` when no set was compared, D265); the code of a
    rejected one."""
    from meshforge.cli.render import _num

    if candidate.get("reject_code"):
        return str(candidate.get("reject_code"))
    test = candidate.get("margin_test")
    if not isinstance(test, Mapping):
        return _DASH
    verdict = "pass" if test.get("passed") is True else "fail"
    sign = "<=" if test.get("passed") is True else ">"
    margin = _num(test.get("margin"))
    forced = test.get("forced")
    return (
        f"{verdict}: {_seconds(test.get('set_s'))} {sign} "
        f"{_seconds(test.get('threshold_s'))} = (1 - "
        f"{_DASH if margin is None else f'{margin:g}'}) x "
        f"{_seconds(test.get('best_single_s'))} ({test.get('best_single_node_id')})"
        + (f", forced: {forced}" if isinstance(forced, str) and forced else "")
    )


def _margin_title(decision: Mapping[str, Any]) -> str:
    """The API's ``margin_sentence`` (V0.14 G5: the controller computes it with
    ``common/explain_text.margin_sentence``, the string the Plan tab shows);
    an answer without the key gets the same function's result."""
    if "margin_sentence" in decision:
        sentence = decision.get("margin_sentence")
        return sentence if isinstance(sentence, str) else _DASH
    return margin_sentence(decision.get("candidates")) or _DASH


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
        title=_cell(_margin_title(decision)),
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


def _ratio(value: Any) -> str:
    from meshforge.cli.render import _num

    number = _num(value)
    return _DASH if number is None else f"x{number:.2f}"


def _actual_title(actual: Mapping[str, Any]) -> str:
    return (
        f"actual: attempt {actual.get('attempt_id')} on {actual.get('node_id')} "
        f"- {actual.get('state')}"
        + (f" ({actual.get('failure_type')})" if actual.get("failure_type") else "")
    )


def _actual_table(decision: Mapping[str, Any]) -> Table | None:
    """Predicted vs actual **per term** of the chosen candidate (V0.14 G5,
    D320): the API's ``actual_s`` and ``ratio`` - the calibration's own term
    -> phase match, ``-`` where no phase honestly measured the term (never a
    second mapping here)."""
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
    table = render.table(
        "TERM", "PREDICTED", "ACTUAL", "RATIO", title=_cell(_actual_title(actual))
    )
    for name, term in terms.items():
        mine = term if isinstance(term, Mapping) else {}
        table.add_row(
            _cell(name),
            _cell(_term_cell(term, str(name))),
            _seconds(mine.get("actual_s")),
            _ratio(mine.get("ratio")),
        )
    return table


def _phases_table(decision: Mapping[str, Any]) -> Table | None:
    """The attempt's five measured phases as the agent reported them."""
    from meshforge.cli.render import _num

    actual = decision.get("actual")
    if not isinstance(actual, Mapping):
        return None
    phases = actual.get("phases_ms")
    phases = phases if isinstance(phases, Mapping) else {}
    table = render.table("PHASE", "ACTUAL", title="measured phases")
    for phase in _PHASES:
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
        table.add_row(phase, shown)
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
            _ml_table(decision),  # V0.15 G5b (D373)
            _split_table(decision),
            _terms_table(decision),
            _actual_table(decision),
            _phases_table(decision),
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
