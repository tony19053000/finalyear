"""Residual targets per (run, term) and every exclusion counted (V0.15 G1, D333, D335).

The target of one row is ``y = ln(actual / raw)``: ``raw`` = the term's
**uncalibrated** analytical prediction as stored on the decision
(:func:`~meshforge.controller.planner.calibration.raw_prediction`), ``actual``
= what was measured (:mod:`.schema` names the measure of each term). A model
that predicts ``y = 0`` *is* the raw analytical model; the corrected term is
``raw x exp(y_hat)``.

**Matching (D333)** is the calibration's: a whole job's or a one-lane split's
observations are :func:`~meshforge.controller.planner.calibration.match`'s
(the same decision/attempt pairs the V0.14 calibration fits); this module only
names *why* an expected term did not come out (a skipped or missing phase, a
term the row does not hold). A **Mode 3 set** (chosen row ``mode = split``),
which the calibration does not fit, is matched here: each lane attempt of the
decision's round against the node's one-lane row of the same decision (the
very ``stage`` / ``env`` / rate the set was costed with), and the job's combine
attempt against the set row's ``combine`` term.

**Exclusions**, each counted by :class:`Skip` reason: a run whose replay is
missing or unreadable (once per decision; never imputed), a run the matching
refuses, and per term: a missing term, a missing or skipped phase, a
``measured`` term (not learnable: it is exact by construction), a raw or actual
value that is not a positive finite number, a raw prediction under
:data:`~meshforge.controller.planner.calibration.MIN_PREDICTED_S` (its ratio is
noise; ``chunk_rate``: the lane's predicted compute seconds for its items), a
lane that did not end ``done``, a lane with no committed chunk, a lane without
its one-lane row, a combine on another node than predicted.

Pure: no database, no clock, no logging.
"""

from __future__ import annotations

import datetime as dt
import math
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from meshforge.controller.planner.calibration import (
    CALIBRATED_TERMS,
    MIN_PREDICTED_S,
    SPLIT_TERMS,
    TERM_PHASE,
    DecisionOutcome,
    chosen_row,
    match,
    raw_prediction,
)
from meshforge.controller.planner.types import TermSource

__all__ = [
    "ChunkOutcome",
    "CombineOutcome",
    "LaneOutcome",
    "Observation",
    "RunInput",
    "Skip",
    "observations",
    "residual",
]


class Skip(StrEnum):
    """Why a run or a term gives no row (counted, never imputed)."""

    MISSING_REPLAY = "missing_replay"
    UNREADABLE_REPLAY = "unreadable_replay"
    UNMATCHED = "unmatched"
    MISSING_TERM = "missing_term"
    MISSING_PHASE = "missing_phase"
    SKIPPED_PHASE = "skipped_phase"
    MEASURED = "measured"
    RAW_NOT_POSITIVE = "raw_not_positive"
    ACTUAL_NOT_POSITIVE = "actual_not_positive"
    BELOW_MIN_PREDICTED = "below_min_predicted"
    LANE_NOT_DONE = "lane_not_done"
    MISSING_LANE_ROW = "missing_lane_row"
    NO_CHUNKS = "no_chunks"
    NODE_MISMATCH = "node_mismatch"


# ------------------------------------------------------------------ inputs


@dataclass(frozen=True, slots=True)
class ChunkOutcome:
    """One ``chunk_record`` row of a lane (the fields the rate needs)."""

    items: int
    run_ms: int | None
    outcome: str


@dataclass(frozen=True, slots=True)
class LaneOutcome:
    """One lane attempt of a split decision's round."""

    attempt_id: str
    node_id: str
    state: str
    phases_ms: Mapping[str, int | None]
    """phase -> ms; None = ``skipped``."""
    finished_at: dt.datetime | None
    chunks: tuple[ChunkOutcome, ...] = ()


@dataclass(frozen=True, slots=True)
class CombineOutcome:
    """The split job's combine attempt."""

    attempt_id: str
    node_id: str
    state: str
    phases_ms: Mapping[str, int | None]
    finished_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class RunInput:
    """One dispatched decision with everything its rows need (the loader's
    read; plain values only)."""

    outcome: DecisionOutcome
    job_id: str
    replay: Mapping[str, Any] | None
    lanes: tuple[LaneOutcome, ...] = ()
    """A split job's lane attempts of this decision's round (the decision's
    own attempt included); empty for a whole job."""
    combine: CombineOutcome | None = None
    """The job's ``done`` combine attempt when this is the job's last
    dispatched decision; None otherwise."""


@dataclass(frozen=True, slots=True)
class Observation:
    """One learnable (run, term) pair."""

    term: str
    node_id: str
    attempt_id: str
    finished_at: dt.datetime
    raw_s: float
    actual_s: float
    y: float
    source: str | None
    detail: Mapping[str, Any] = field(default_factory=dict)
    lanes: int = 1
    chunk_items: int | None = None


# ------------------------------------------------------------------ helpers


def residual(raw_s: float, actual_s: float) -> float:
    """``ln(actual / raw)`` (both positive finite; :func:`check` first)."""
    return math.log(actual_s / raw_s)


def _positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0.0


def check(
    raw_s: float | None,
    actual_s: float | None,
    source: str | None,
    *,
    min_check_s: float | None = None,
) -> Skip | None:
    """Why ``(raw, actual)`` gives no target; None = learnable. Order:
    measured, raw, actual, under the floor (``min_check_s`` = the seconds the
    floor applies to, default ``raw_s``)."""
    if source == TermSource.MEASURED.value:
        return Skip.MEASURED
    if not _positive(raw_s):
        return Skip.RAW_NOT_POSITIVE
    if not _positive(actual_s):
        return Skip.ACTUAL_NOT_POSITIVE
    floor = raw_s if min_check_s is None else min_check_s
    if floor is None or floor < MIN_PREDICTED_S:
        return Skip.BELOW_MIN_PREDICTED
    return None


def _terms(row: Mapping[str, Any]) -> Mapping[str, Any]:
    estimate = row.get("estimate")
    terms = estimate.get("terms") if isinstance(estimate, Mapping) else None
    return terms if isinstance(terms, Mapping) else {}


def _source(term: Mapping[str, Any]) -> str | None:
    value = term.get("source")
    return value if isinstance(value, str) else None


def _detail(term: Mapping[str, Any]) -> Mapping[str, Any]:
    detail = term.get("detail")
    return detail if isinstance(detail, Mapping) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _phase_reason(phases: Mapping[str, int | None], phase: str) -> Skip | None:
    if phase not in phases:
        return Skip.MISSING_PHASE
    value = phases[phase]
    if value is None:
        return Skip.SKIPPED_PHASE
    if isinstance(value, bool) or value < 0:
        return Skip.MISSING_PHASE
    return None


@dataclass(slots=True)
class _Out:
    rows: list[Observation] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)

    def skip(self, reason: Skip) -> None:
        self.skipped[reason.value] += 1


def _plan_shape(row: Mapping[str, Any]) -> tuple[int, int | None]:
    """(lanes, chunk items) of a chosen row."""
    node_set = row.get("node_set")
    lanes = len(node_set) if isinstance(node_set, list) and node_set else 1
    if row.get("mode") == "split":
        inactive = row.get("inactive")
        if isinstance(inactive, list):
            lanes = max(1, lanes - len(inactive))
    return lanes, _int(row.get("chunk_items"))


# ------------------------------------------------------------------ whole job


def _whole(run: RunInput, row: Mapping[str, Any], out: _Out) -> None:
    """A whole job or a one-lane split: :func:`match`'s observations."""
    outcome = run.outcome
    split = bool(row.get("node_set"))
    if (
        outcome.attempt_state != "done"
        or outcome.attempt_node_id != outcome.chosen_node_id
        or outcome.attempt_role != ("lane" if split else "run")
    ):
        out.skip(Skip.UNMATCHED)  # what match() refuses as a whole
        return
    matched = match(outcome)
    terms = _terms(row)
    lanes, chunk_items = _plan_shape(row)
    found = {} if matched is None else {obs.term: obs for obs in matched.terms}
    for name in SPLIT_TERMS if split else CALIBRATED_TERMS:
        ml_name = f"lane_{'stage' if name == 'transfer' else name}" if split else name
        term = terms.get(name)
        if not isinstance(term, Mapping):
            out.skip(Skip.MISSING_TERM)
            continue
        obs = found.get(name)
        if obs is None:  # match dropped it (or the whole run): name why
            reason = _phase_reason(outcome.phases_ms, TERM_PHASE[name])
            out.skip(reason or Skip.RAW_NOT_POSITIVE)
            continue
        _add(
            out,
            ml_name,
            node_id=outcome.attempt_node_id,
            attempt_id=outcome.attempt_id,
            finished_at=outcome.finished_at,
            raw_s=obs.predicted_s,
            actual_s=obs.actual_s,
            term=term,
            lanes=lanes,
            chunk_items=chunk_items,
        )
    if split:
        lane = next(
            (item for item in run.lanes if item.attempt_id == outcome.attempt_id),
            None,
        )
        if lane is not None:
            _chunk_rate(
                lane,
                row,
                out,
                fallback=outcome.finished_at,
                lanes=lanes,
                chunk_items=chunk_items,
            )
        _combine(run, row, out, lanes=lanes, chunk_items=chunk_items)


def _add(
    out: _Out,
    name: str,
    *,
    node_id: str,
    attempt_id: str,
    finished_at: dt.datetime,
    raw_s: float | None,
    actual_s: float | None,
    term: Mapping[str, Any],
    lanes: int,
    chunk_items: int | None,
    source: str | None = None,
    min_check_s: float | None = None,
) -> None:
    src = _source(term) if source is None else source
    reason = check(raw_s, actual_s, src, min_check_s=min_check_s)
    if reason is not None or raw_s is None or actual_s is None:
        out.skip(reason or Skip.RAW_NOT_POSITIVE)  # check() rejects None
        return
    out.rows.append(
        Observation(
            term=name,
            node_id=node_id,
            attempt_id=attempt_id,
            finished_at=finished_at,
            raw_s=raw_s,
            actual_s=actual_s,
            y=residual(raw_s, actual_s),
            source=src,
            detail=_detail(term),
            lanes=lanes,
            chunk_items=chunk_items,
        )
    )


# ------------------------------------------------------------------ Mode 3


def _one_lane_row(outcome: DecisionOutcome, node_id: str) -> Mapping[str, Any] | None:
    """The decision's one-lane row of ``node_id`` (the lane the set reused)."""
    for row in outcome.candidates:
        if not isinstance(row, Mapping) or row.get("mode") == "split":
            continue
        node_set = row.get("node_set")
        if (
            row.get("node_id") == node_id
            and isinstance(node_set, list)
            and node_set == [node_id]
            and row.get("feasible") is True
        ):
            return row
    return None


def _chunk_rate(
    lane: LaneOutcome,
    lane_row: Mapping[str, Any],
    out: _Out,
    *,
    fallback: dt.datetime,
    lanes: int,
    chunk_items: int | None,
) -> None:
    rate = lane_row.get("rate_items_s")
    rate_s = None
    if not isinstance(rate, bool) and isinstance(rate, int | float):
        rate_s = float(rate)
    raw_spi = 1.0 / rate_s if rate_s is not None and _positive(rate_s) else None
    committed = [
        chunk
        for chunk in lane.chunks
        if chunk.outcome == "committed" and chunk.run_ms is not None
    ]
    items = sum(chunk.items for chunk in committed)
    run_ms = sum(chunk.run_ms or 0 for chunk in committed)
    if not committed or items <= 0:
        out.skip(Skip.NO_CHUNKS)
        return
    actual_spi = (run_ms / 1000.0) / items
    source = lane_row.get("rate_source")
    _add(
        out,
        "chunk_rate",
        node_id=lane.node_id,
        attempt_id=lane.attempt_id,
        finished_at=lane.finished_at or fallback,
        raw_s=raw_spi,
        actual_s=actual_spi,
        term={},
        source=source if isinstance(source, str) else None,
        min_check_s=None if raw_spi is None else raw_spi * items,
        lanes=lanes,
        chunk_items=chunk_items,
    )


def _combine(
    run: RunInput,
    row: Mapping[str, Any],
    out: _Out,
    *,
    lanes: int,
    chunk_items: int | None,
) -> None:
    combine = run.combine
    if combine is None:
        return  # not this decision's (a later round's) or never ran
    term = _terms(row).get("combine")
    if not isinstance(term, Mapping):
        out.skip(Skip.MISSING_TERM)
        return
    predicted_node = _detail(term).get("combine_node_id")
    if predicted_node is not None and predicted_node != combine.node_id:
        out.skip(Skip.NODE_MISMATCH)
        return
    phases = combine.phases_ms
    run_reason = _phase_reason(phases, "run")
    if run_reason is not None:
        out.skip(run_reason)
        return
    total_ms = 0
    for phase in ("stage", "env", "run"):
        if phase not in phases:
            out.skip(Skip.MISSING_PHASE)
            return
        value = phases[phase]
        total_ms += 0 if value is None else max(0, int(value))  # skipped = no work
    _add(
        out,
        "combine",
        node_id=combine.node_id,
        attempt_id=combine.attempt_id,
        finished_at=combine.finished_at or run.outcome.finished_at,
        raw_s=raw_prediction(term),
        actual_s=total_ms / 1000.0,
        term=term,
        lanes=lanes,
        chunk_items=chunk_items,
    )


def _set(run: RunInput, row: Mapping[str, Any], out: _Out) -> None:
    """A Mode 3 set decision (module docstring)."""
    outcome = run.outcome
    if (
        outcome.attempt_state != "done"
        or outcome.attempt_role != "lane"
        or outcome.chosen_node_id is None
    ):
        out.skip(Skip.UNMATCHED)
        return
    lanes, chunk_items = _plan_shape(row)
    node_set = row.get("node_set")
    members = set(node_set) if isinstance(node_set, list) else set()
    for lane in sorted(run.lanes, key=lambda item: (item.node_id, item.attempt_id)):
        if lane.node_id not in members:
            continue
        if lane.state != "done":
            for _ in range(3):  # lane_stage, lane_env, chunk_rate
                out.skip(Skip.LANE_NOT_DONE)
            continue
        lane_row = _one_lane_row(outcome, lane.node_id)
        if lane_row is None:
            for _ in range(3):
                out.skip(Skip.MISSING_LANE_ROW)
            continue
        terms = _terms(lane_row)
        finished = lane.finished_at or outcome.finished_at
        for name, ml_name in (("transfer", "lane_stage"), ("env", "lane_env")):
            term = terms.get(name)
            if not isinstance(term, Mapping):
                out.skip(Skip.MISSING_TERM)
                continue
            reason = _phase_reason(lane.phases_ms, TERM_PHASE[name])
            if reason is not None:
                out.skip(reason)
                continue
            actual_ms = lane.phases_ms[TERM_PHASE[name]]
            _add(
                out,
                ml_name,
                node_id=lane.node_id,
                attempt_id=lane.attempt_id,
                finished_at=finished,
                raw_s=raw_prediction(term),
                actual_s=None if actual_ms is None else actual_ms / 1000.0,
                term=term,
                lanes=lanes,
                chunk_items=chunk_items,
            )
        _chunk_rate(
            lane,
            lane_row,
            out,
            fallback=finished,
            lanes=lanes,
            chunk_items=chunk_items,
        )
    _combine(run, row, out, lanes=lanes, chunk_items=chunk_items)


# ------------------------------------------------------------------ entry


def observations(run: RunInput) -> tuple[list[Observation], Counter[str]]:
    """The learnable (term, node) pairs of one decision and the skip counts
    (module docstring). The replay is checked by the caller (it decodes it
    for the features); this function never reads it."""
    out = _Out()
    row = chosen_row(run.outcome)
    if row is None:
        out.skip(Skip.UNMATCHED)
    elif row.get("mode") == "split":
        _set(run, row, out)
    else:
        _whole(run, row, out)
    return out.rows, out.skipped
