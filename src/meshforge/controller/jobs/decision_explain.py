"""What the decisions answer adds to a stored decision (V0.14 G5, D300, D320).

``plan_decision.candidates`` stays exactly what the planner wrote; this module
derives, at read time, the fields the Plan tab and ``compute explain`` both
show (one function each, so they cannot drift - criterion 4):

* per term: ``calibration`` (the factor the planner scaled it by, lifted from
  ``detail.calibration``: ``factor``, ``n``, ``key_level``, ``version``,
  ``raw_s``), ``note`` (:func:`meshforge.common.explain_text.term_note`), and
  on the **chosen** candidate of a decision whose attempt ran, ``actual_s`` +
  ``ratio`` (actual / predicted);
* per decision: ``margin_sentence``
  (:func:`meshforge.common.explain_text.margin_sentence`).

**Actual per term = calibration's match** (D320): the term -> phase mapping is
:func:`meshforge.controller.planner.calibration.match` - the very pairs the
calibration is fitted on - never a second mapping. So a term has an actual
only where a measured phase is honestly the same work: a whole job's
``transfer``/``env``/``compute``/``output`` (``stage``/``env``/``run``/
``collect``), a one-lane split job's ``transfer``/``env``; nothing for a Mode 3
set (its lanes' phases mix several terms), a skipped phase, an attempt that
did not end ``done`` or ran on another node. Everything else is ``null`` -
"no honest phase", never 0. The raw phase timings stay in ``actual.phases_ms``.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any, Final

from meshforge.common.explain_text import margin_sentence, term_note
from meshforge.controller.jobs.decision_store import DecisionRecordOut
from meshforge.controller.planner.calibration import DecisionOutcome, match

__all__ = [
    "explain_candidates",
    "margin_sentence_of",
    "term_actuals",
]

_ROUND: Final = 6


def _finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def term_actuals(record: DecisionRecordOut) -> dict[str, float]:
    """``term -> actual seconds`` of the decision's chosen candidate (module
    docstring); empty when nothing is honestly comparable (yet)."""
    actual = record.actual
    if (
        actual is None
        or record.attempt_id is None
        or actual.ended_at is None
        or actual.role is None
    ):
        return {}
    matched = match(
        DecisionOutcome(
            decision_id=record.id,
            attempt_id=record.attempt_id,
            chosen_node_id=record.chosen_node_id,
            attempt_node_id=actual.node_id,
            attempt_state=actual.state,
            attempt_role=actual.role,
            candidates=record.candidates,
            phases_ms=actual.phases_ms,
            finished_at=actual.ended_at,
        )
    )
    if matched is None:
        return {}
    return {t.term: round(t.actual_s, _ROUND) for t in matched.terms}


def _calibration(detail: object) -> dict[str, Any] | None:
    """``detail.calibration`` as the typed block, or None (absent / unreadable:
    an uncalibrated term, or a row from before V0.14 G2)."""
    if not isinstance(detail, Mapping):
        return None
    block = detail.get("calibration")
    if not isinstance(block, Mapping):
        return None
    factor = _finite(block.get("factor"))
    if factor is None:
        return None
    level = block.get("key_level")
    return {
        "factor": factor,
        "n": _count(block.get("n")),
        "key_level": level if isinstance(level, str) else None,
        "version": _count(block.get("version")),
        "raw_s": _finite(block.get("raw_s")),
    }


def _term(name: str, term: Mapping[str, Any], actual_s: float | None) -> Any:
    seconds = _finite(term.get("seconds"))
    ratio = (
        None
        if actual_s is None or seconds is None or seconds <= 0.0
        else round(actual_s / seconds, _ROUND)
    )
    return {
        **term,
        "calibration": _calibration(term.get("detail")),
        "note": term_note(term, name=name),
        "actual_s": actual_s,
        "ratio": ratio,
    }


def _is_chosen(row: Mapping[str, Any], chosen_node_id: str | None) -> bool:
    """The row :func:`~meshforge.controller.planner.calibration.match` reads."""
    return row.get("rank") == 1 and row.get("node_id") == chosen_node_id


def explain_candidates(
    candidates: Sequence[Any],
    *,
    chosen_node_id: str | None,
    actuals: Mapping[str, float],
) -> list[Any]:
    """The stored rows with every term's derived fields (module docstring).
    A row that is not an object, or an estimate without terms, is passed on
    unchanged (the answer model then decides)."""
    out: list[Any] = []
    for row in candidates:
        if not isinstance(row, Mapping):
            out.append(row)
            continue
        estimate = row.get("estimate")
        if not isinstance(estimate, Mapping):
            out.append(row)
            continue
        terms = estimate.get("terms")
        if not isinstance(terms, Mapping):
            out.append(row)
            continue
        mine = actuals if _is_chosen(row, chosen_node_id) else {}
        out.append(
            {
                **row,
                "estimate": {
                    **estimate,
                    "terms": {
                        str(name): _term(str(name), term, mine.get(str(name)))
                        if isinstance(term, Mapping)
                        else term
                        for name, term in terms.items()
                    },
                },
            }
        )
    return out


def margin_sentence_of(record: DecisionRecordOut) -> str | None:
    """The decision's margin-test sentence (``None`` for a whole job)."""
    return margin_sentence(list(record.candidates))
