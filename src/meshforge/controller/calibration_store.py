"""Planner calibration in PostgreSQL (V0.14 G2, D293/D301/D306; table
``planner_calibration``, migration 0031).

* :class:`CalibrationStore` - the table's one writer and its reads:

  - :meth:`~CalibrationStore.refresh` - **one transaction under an advisory
    lock**: read the matched (decision, outcome) runs (newest
    ``max_runs``), read the active version (``FOR UPDATE``), fit and gate
    (:func:`~meshforge.controller.planner.calibration.refit`, pure), then
    either supersede the active row and insert the new one as ``active``, or
    insert it as ``rejected`` with the gate's reason. Two refreshes at once
    (two controllers, the task and ``compute planner calibrate``) are
    serialised by the lock - the second gates against what the first
    activated - and the partial unique index guarantees at most one
    ``active`` row whatever happens. Too few runs, or nothing to fit, writes
    nothing; so does a refresh whose matched runs and fit settings are those
    of the newest row (``no_new_runs``: the fit is deterministic, so it would
    only duplicate that row; an unreadable newest row is refitted). The pure
    match/fit/gate runs in a worker thread (``asyncio.to_thread``) so a large
    fit never stalls the event loop while the transaction and its lock are
    held.
  - :meth:`~CalibrationStore.load`, :meth:`~CalibrationStore.active_version`,
    :meth:`~CalibrationStore.row`, :meth:`~CalibrationStore.recent`.

* :class:`CalibrationSource` - what the dispatcher plans with, once per tick:
  ``off`` -> None; a pinned version -> that version (loaded once; startup
  refuses a pin that does not exist, cannot be read or is ``rejected``,
  :meth:`CalibrationSource.check`); ``auto`` -> the active version, re-read
  by id every tick and loaded only when the id changed. A failed read keeps
  the version it had (logged once per failure streak): a database hiccup
  never blocks or changes dispatch. A version whose factors cannot be read
  is remembered (logged once, planned without calibration) and not re-read
  every tick.

**D319 (V0.14 G4): the regret check.** When the D306 error check accepts a
new version, the refresh also asks the evaluator
(:mod:`meshforge.controller.planner_eval.regret_gate`) whether the version
makes the planner *pick* worse on the same held-out slice: the labelled
benchmark groups of the slice's jobs, calibrated ``cost_model`` with the new
vs the active version, paired. Mean regret above the active one's x 1.05 ->
``rejected`` with the reason; fewer than 10 such groups -> the check is
``skipped`` and the row's ``reason`` says so (the error check alone decided).
Its numbers are in ``fit_inputs`` (``regret_check``, ``regret_groups``,
``holdout_regret_new`` / ``_active``; no new column). A database failure in
the check fails the refresh like any other (nothing written).

**The slow part runs before the lock** (G4 fix round 1, reviewer N2): the
refresh first reads the matched runs and the active version **without** the
lock, fits, gates and runs the D319 check (label reads + replays, the
expensive part). Then, under the lock, it re-reads both; when the matched-run
set, the fit settings and the active version are unchanged - the normal case
- the prepared result is stored as is. If anything changed in between
(another refresh activated a version, new runs finished), it refits and
re-checks under the lock, so the stored row is always judged against what is
active when it is written; the one-active invariant is unchanged.
Not re-checked: the D319 labels (``load_for_jobs``) may change between the
prepare and the store; they were never under the lock, so this is no new
race. The re-check path opens a second pooled session for the label reads
while the locked transaction holds its connection, so the pool needs at
least 2 connections (the default pool is larger).

**V1.0 G4 (D412, F-2): the run-set fingerprint.** A refresh first asks the
database for a cheap digest of the read window (:func:`window_fingerprint`:
count + an md5 over every field :func:`match` and the run set depend on -
decision id, chosen node, candidates, attempt node and role, every phase row -
aggregated in SQL, nothing materialised in Python). When it equals the one
this store's last refresh read its run set under (same read bound), that run
set is the current one, and the ``no_new_runs`` rule is applied to it and
the current settings against the newest row (one row read): a match is
``no_new_runs`` without reading or matching anything (the steady state of an
idle fabric: the refit used to read and match up to 4 000 runs every
interval, ~+110 MiB of allocator high-water at the cap). Under the lock the
fingerprint is taken again; equal to the prepare's -> the prepared run set is
reused instead of a second full read (only a refit forced by a changed active
version re-reads). The fingerprint is always taken **before** the outcome read
it vouches for, so a run landing in between makes it stale (a full refresh
next time), never wrong. The D306 gate and the ``no_new_runs`` rule
(:data:`_RUN_SET_KEYS` against the newest row) are unchanged; the memo is
per store instance (a new one - ``compute planner calibrate`` - takes the full
path).

**The matched runs** (one bounded read): ``dispatched`` decisions joined to
their attempt (``done``, ``ended_at`` set), the attempt's run record (workload
family, fingerprint) and the job (fingerprint when the record is not written
yet), newest first; then their phases in one ``IN`` read. The pure
:func:`~meshforge.controller.planner.calibration.match` decides what counts.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import Text, and_, cast, func, literal_column, or_, select, text
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.errors import ConfigError, ExitCode, FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.controller.planner.calibration import (
    MIN_RUNS,
    Calibration,
    CalibrationConfig,
    CalibrationError,
    DecisionOutcome,
    GateResult,
    MatchedRun,
    Refit,
    calibration_from_json,
    calibration_json,
    match,
    refit,
    split_holdout,
)

# V0.14 G4 (D319): the regret check of a new version
from meshforge.controller.planner_eval.loader import (
    EvalLoadError,
    EvalTooLargeError,
    load_for_jobs,
)
from meshforge.controller.planner_eval.regret_gate import RegretCheck, regret_check
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.calibration import (
    MAX_CALIBRATION_REASON_CHARS,
    PlannerCalibration,
)
from meshforge.controller.store.models.job import Attempt, AttemptPhaseRow, Job
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.export import MAX_EXPORT_ROWS

__all__ = [
    "MAX_RUNS_READ",
    "CalibrationMode",
    "CalibrationRow",
    "CalibrationSource",
    "CalibrationStore",
    "CalibrationStoreError",
    "RefreshResult",
    "factor_rows",
    "matched_watermark",
    "parse_mode",
    "read_outcomes",
    "row_summary",
    "window_fingerprint",
]

log = get_logger(__name__)

MAX_RUNS_READ: Final = 20_000
"""Upper bound of matched runs one refresh reads (newest first); the caller
asks for ``calibration_window_runs x`` :data:`RUNS_PER_WINDOW` below it."""
RUNS_PER_WINDOW: Final = 20
"""Runs read per window size: enough for the window on ~20 node keys (a
fabric of laptops) without reading the whole history."""
_LOCK: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('meshforge.planner_calibration', 0))"
)
_PHASE_BATCH: Final = 5_000
_DB_ERRORS: Final = (SQLAlchemyError, OSError, TimeoutError)
"""A database that refuses, drops or never answers (asyncpg raises
``OSError`` for a refused connection, outside SQLAlchemy's wrapping)."""

type CalibrationMode = Literal["auto", "off", "pinned"]


class CalibrationStoreError(MeshForgeError):
    """The database refused or lost a calibration read/write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


@dataclass(frozen=True, slots=True)
class CalibrationRow:
    """One ``planner_calibration`` row, read back."""

    version: int
    created_at: dt.datetime
    status: str
    calibration: Calibration | None
    """None = its factors could not be read (never used for planning)."""
    fit_inputs: dict[str, Any]
    holdout_error_new: float | None
    holdout_error_active: float | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class RefreshResult:
    """What one refresh did: ``row`` None = nothing written (``skipped``)."""

    matched_runs: int
    row: CalibrationRow | None
    skipped: str | None = None
    """``too_few_runs`` | ``nothing_to_fit`` | ``no_new_runs`` (no row
    written)."""
    superseded: int | None = None
    """The version this one replaced as ``active``."""


_RUN_SET_KEYS: Final = (
    "matched_runs",
    "newest_decision_id",
    "runs_digest",
    "window",
    "k",
    "clamp",
)
"""The ``fit_inputs`` keys that identify what a fit was computed from: the
matched-run set (count, newest decision id, digest of every decision id) and
the fit settings. Equal to the newest row's -> ``no_new_runs``."""


def run_set(runs: Sequence[MatchedRun]) -> dict[str, Any]:
    """The matched-run set of one refresh, as stored in ``fit_inputs``."""
    ids = sorted(run.decision_id for run in runs)
    digest = hashlib.sha256(",".join(map(str, ids)).encode()).hexdigest()[:32]
    return {
        "matched_runs": len(ids),
        "newest_decision_id": ids[-1] if ids else None,
        "runs_digest": digest,
    }


def _same_inputs(previous: Mapping[str, Any], current: Mapping[str, Any]) -> bool:
    """Same matched runs and settings (a row from before these keys never
    matches)."""
    return all(
        key in previous and previous[key] == current.get(key) for key in _RUN_SET_KEYS
    )


def _settings_of(config: CalibrationConfig) -> dict[str, Any]:
    """The fit settings :data:`_RUN_SET_KEYS` compares."""
    return {
        "window": config.window_runs,
        "k": config.shrink_k,
        "clamp": list(config.clamp),
    }


def _fit_set(runs: Sequence[MatchedRun], config: CalibrationConfig) -> dict[str, Any]:
    """The matched-run set and fit settings of one refresh (``fit_inputs``)."""
    return {**run_set(runs), **_settings_of(config)}


@dataclass(frozen=True, slots=True)
class _Prepared:
    """G4 fix round 1 (N2): a refresh's result computed before the lock."""

    inputs: dict[str, Any]
    active_version: int | None
    result: Refit | None
    note: str | None
    fingerprint: tuple[int, str | None]
    """V1.0 G4 (F-2): :func:`window_fingerprint` taken before the read."""
    runs: list[MatchedRun]


@dataclass(frozen=True, slots=True)
class _Seen:
    """V1.0 G4 (F-2): the run set this store's last refresh judged, keyed by
    the window fingerprint it was read under (module docstring)."""

    fingerprint: tuple[int, str | None]
    limit: int
    inputs: dict[str, Any]
    """:func:`_fit_set` of that run set (its settings included)."""


def _match_all(outcomes: Sequence[DecisionOutcome]) -> list[MatchedRun]:
    return [m for o in outcomes if (m := match(o)) is not None]


def _row(item: PlannerCalibration) -> CalibrationRow:
    try:
        calibration: Calibration | None = calibration_from_json(
            int(item.version), item.factors
        )
    except CalibrationError:
        calibration = None
    return CalibrationRow(
        version=int(item.version),
        created_at=item.created_at,
        status=item.status,
        calibration=calibration,
        fit_inputs=dict(item.fit_inputs),
        holdout_error_new=item.holdout_error_new,
        holdout_error_active=item.holdout_error_active,
        reason=item.reason,
    )


def _matched(query: Any) -> Any:
    """``query`` joined to the decision's attempt and cut to the matched-run
    set: ``dispatched`` decisions whose attempt ended ``done``. The one
    definition :func:`read_outcomes` and :func:`matched_watermark` share."""
    return query.join(Attempt, Attempt.id == PlanDecision.attempt_id).where(
        PlanDecision.outcome == "dispatched",
        Attempt.state == "done",
        Attempt.ended_at.is_not(None),
    )


async def matched_watermark(
    db: AsyncSession,
) -> tuple[int, dt.datetime | None, int | None]:
    """V0.15 G2 (D355): ``(count, newest ended_at, newest decision id)`` of the
    whole matched-run set - two cheap aggregates over the same join as
    :func:`read_outcomes` (the ML dataset route's cache key). Newest = the
    largest ``(ended_at, decision id)``, the read's own order."""
    count = await db.scalar(_matched(select(func.count()).select_from(PlanDecision)))
    newest = (
        await db.execute(
            _matched(select(Attempt.ended_at, PlanDecision.id))
            .order_by(Attempt.ended_at.desc(), PlanDecision.id.desc())
            .limit(1)
        )
    ).first()
    if newest is None:
        return int(count or 0), None, None
    return int(count or 0), newest[0], int(newest[1])


def _window(*columns: Any, limit: int) -> Any:
    """The read window: :func:`_matched` + the job, newest first by
    ``(ended_at, decision id)``, at most ``limit`` rows - the one row set
    :func:`read_outcomes` and :func:`window_fingerprint` share."""
    return (
        _matched(select(*columns))
        .join(Job, Job.id == PlanDecision.job_id)
        .order_by(Attempt.ended_at.desc(), PlanDecision.id.desc())
        .limit(limit)
    )


async def window_fingerprint(db: AsyncSession, limit: int) -> tuple[int, str | None]:
    """V1.0 G4 (F-2): ``(rows, md5)`` of :func:`read_outcomes`' window
    computed in the database. The digest covers every field the matched-run
    set depends on (decision id, chosen node, candidates, attempt node and
    role, every phase row of the attempt); equal fingerprints -> the same
    matched decision ids. ``md5`` is None for an empty window."""
    w = _window(
        PlanDecision.id.label("decision_id"),
        PlanDecision.chosen_node_id.label("chosen"),
        PlanDecision.candidates.label("candidates"),
        Attempt.id.label("attempt_id"),
        Attempt.node_id.label("node"),
        Attempt.role.label("role"),
        limit=limit,
    ).subquery("w")
    phase_text = func.concat(
        AttemptPhaseRow.phase,
        "=",
        AttemptPhaseRow.skipped,
        "/",
        AttemptPhaseRow.duration_ms,
    )
    phases = (
        select(
            func.string_agg(
                phase_text, aggregate_order_by(literal_column("';'"), phase_text)
            )
        )
        .where(AttemptPhaseRow.attempt_id == w.c.attempt_id)
        .scalar_subquery()
    )
    row_text = func.concat(
        w.c.decision_id,
        "|",
        w.c.chosen,
        "|",
        func.md5(cast(w.c.candidates, Text)),
        "|",
        w.c.attempt_id,
        "|",
        w.c.node,
        "|",
        w.c.role,
        "|",
        phases,
    )
    found = (
        await db.execute(
            select(
                func.count(),
                func.md5(
                    func.string_agg(
                        row_text,
                        aggregate_order_by(literal_column("','"), w.c.decision_id),
                    )
                ),
            ).select_from(w)
        )
    ).one()
    return int(found[0] or 0), None if found[1] is None else str(found[1])


async def _newest_has(db: AsyncSession, inputs: Mapping[str, Any]) -> int | None:
    """The ``no_new_runs`` rule: the newest row's version when it was fitted
    from ``inputs`` (same run set and settings) and its factors are readable
    (an unreadable newest row is refitted); None otherwise."""
    newest = (
        await db.scalars(
            select(PlannerCalibration)
            .order_by(PlannerCalibration.version.desc())
            .limit(1)
        )
    ).first()
    if (
        newest is not None
        and _same_inputs(newest.fit_inputs, inputs)
        and _row(newest).calibration is not None
    ):
        return int(newest.version)
    return None


async def read_outcomes(
    db: AsyncSession,
    limit: int,
    *,
    before: tuple[dt.datetime, int] | None = None,
) -> list[DecisionOutcome]:
    """The matched-run read (module docstring): at most ``limit``
    ``dispatched`` decisions whose attempt ended ``done``, newest first by
    ``(ended_at, decision id)``. ``before`` (V0.15 G1, D333) = keyset cursor:
    only rows strictly older than that ``(ended_at, decision id)`` - the ML
    dataset loader pages through the **same** read the calibration fits on
    (one query for both, never a copy)."""
    query = _window(
        PlanDecision.id,
        PlanDecision.chosen_node_id,
        PlanDecision.candidates,
        Attempt.id,
        Attempt.node_id,
        Attempt.state,
        Attempt.role,
        Attempt.ended_at,
        RunRecord.workload_family,
        func.coalesce(RunRecord.fingerprint, Job.fingerprint),
        limit=limit,
    ).outerjoin(RunRecord, RunRecord.attempt_id == Attempt.id)
    if before is not None:
        ended, decision_id = before
        query = query.where(
            or_(
                Attempt.ended_at < ended,
                and_(Attempt.ended_at == ended, PlanDecision.id < decision_id),
            )
        )
    rows = list((await db.execute(query)).all())
    ids = [str(row[3]) for row in rows]
    phases: dict[str, dict[str, int | None]] = {}
    for start in range(0, len(ids), _PHASE_BATCH):
        batch = ids[start : start + _PHASE_BATCH]
        for phase in await db.scalars(
            select(AttemptPhaseRow).where(AttemptPhaseRow.attempt_id.in_(batch))
        ):
            phases.setdefault(phase.attempt_id, {})[phase.phase] = (
                None if phase.skipped else int(phase.duration_ms)
            )
    return [
        DecisionOutcome(
            decision_id=int(row[0]),
            attempt_id=str(row[3]),
            chosen_node_id=row[1],
            attempt_node_id=str(row[4]),
            attempt_state=str(row[5]),
            attempt_role=str(row[6]),
            candidates=list(row[2] or ()),
            phases_ms=phases.get(str(row[3]), {}),
            finished_at=row[7],
            family=row[8],
            fingerprint=row[9],
        )
        for row in rows
    ]


class CalibrationStore:
    """See module docstring."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        regret_check: bool = True,
        max_label_rows: int = MAX_EXPORT_ROWS,
    ) -> None:
        self._engine = engine
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )
        # V0.14 G4 (D319)
        self._regret_check = regret_check
        self._max_label_rows = max_label_rows
        # V1.0 G4 (F-2): the last no_new_runs/stored refresh (module docstring)
        self._seen: _Seen | None = None
        self.full_reads = 0
        """Outcome reads a refresh made (observability for the F-2 tests)."""

    async def matched_runs(self, config: CalibrationConfig) -> list[MatchedRun]:
        """V0.14 G4 (D316): the matched runs a refresh would read (newest
        first, the same bound), for the evaluator's held-out refits.
        :class:`CalibrationStoreError` when the database fails."""
        limit = min(MAX_RUNS_READ, config.window_runs * RUNS_PER_WINDOW)
        try:
            async with self._sessions() as db:
                outcomes = await read_outcomes(db, max(1, limit))
        except _DB_ERRORS as exc:
            raise CalibrationStoreError(
                "the calibration read failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc
        return await asyncio.to_thread(_match_all, outcomes)

    async def _with_regret_check(
        self,
        result: Refit,
        runs: Sequence[MatchedRun],
        active: Calibration | None,
    ) -> tuple[Refit, str | None]:
        """V0.14 G4 (D319): ``result`` after the regret check (module
        docstring) and the note for an accepted row (the skip reason)."""
        _fit, held = split_holdout(runs)
        ids = sorted({run.decision_id for run in held})
        jobs: set[str] = set()
        if ids:
            async with self._sessions() as db:
                jobs = set(
                    await db.scalars(
                        select(PlanDecision.job_id).where(PlanDecision.id.in_(ids))
                    )
                )
        try:
            inputs = await load_for_jobs(
                self._engine, jobs, max_rows=self._max_label_rows
            )
        except EvalLoadError as exc:
            raise CalibrationStoreError(
                "the calibration regret check's read failed in the database",
                details={"cause": exc.details.get("cause")},
            ) from exc
        except EvalTooLargeError:
            check = RegretCheck(
                "skipped", 0, None, None, "regret check skipped (campaign too large)"
            )
        else:
            check = await asyncio.to_thread(
                regret_check, inputs.groups, inputs.records, result.calibration, active
            )
        checked = dataclasses.replace(
            result, fit_inputs={**result.fit_inputs, **check.fit_inputs()}
        )
        if check.status == "rejected":
            return dataclasses.replace(
                checked, gate=GateResult(False, check.reason)
            ), None
        return checked, check.reason

    async def _fit_and_check(
        self,
        runs: Sequence[MatchedRun],
        config: CalibrationConfig,
        active: Calibration | None,
    ) -> tuple[Refit | None, str | None]:
        """Fit + D306 gate (worker thread) + the D319 check when accepted."""
        result = await asyncio.to_thread(refit, runs, config, active)
        note: str | None = None
        if result is not None and result.gate.accepted and self._regret_check:
            result, note = await self._with_regret_check(result, runs, active)
        return result, note

    async def _prepare(self, config: CalibrationConfig, limit: int) -> _Prepared:
        """G4 fix round 1 (N2): the refresh's work done **without** the lock
        (module docstring)."""
        async with self._sessions() as db:
            fingerprint = await window_fingerprint(db, limit)  # before the read
            outcomes = await read_outcomes(db, limit)
            self.full_reads += 1
            current = (
                await db.scalars(
                    select(PlannerCalibration).where(
                        PlannerCalibration.status == "active"
                    )
                )
            ).first()
        runs = await asyncio.to_thread(_match_all, outcomes)
        del outcomes  # only the matched runs are kept
        active = None if current is None else _row(current).calibration
        result, note = await self._fit_and_check(runs, config, active)
        return _Prepared(
            inputs=_fit_set(runs, config),
            active_version=None if current is None else int(current.version),
            result=result,
            note=note,
            fingerprint=fingerprint,
            runs=runs,
        )

    async def _still_unchanged(
        self, config: CalibrationConfig, limit: int
    ) -> RefreshResult | None:
        """V1.0 G4 (F-2): ``no_new_runs`` from two cheap reads (the window
        fingerprint, the newest row) when the window is the one this store's
        last refresh judged and the newest row was fitted from that run set
        and these settings - the same rule the full path applies."""
        seen = self._seen
        if seen is None or seen.limit != limit:
            return None
        inputs = {**seen.inputs, **_settings_of(config)}
        async with self._sessions() as db:
            if await window_fingerprint(db, limit) != seen.fingerprint:
                return None
            if await _newest_has(db, inputs) is None:
                return None
        return RefreshResult(
            matched_runs=int(inputs["matched_runs"]), row=None, skipped="no_new_runs"
        )

    async def refresh(
        self, config: CalibrationConfig, *, max_runs: int | None = None
    ) -> RefreshResult:
        """Fit, gate and store one new version (module docstring).
        :class:`CalibrationStoreError` when the database fails (nothing is
        written then: one transaction)."""
        limit = max(
            1, max_runs or min(MAX_RUNS_READ, config.window_runs * RUNS_PER_WINDOW)
        )
        try:
            unchanged = await self._still_unchanged(config, limit)
            if unchanged is not None:
                return unchanged
            prepared = await self._prepare(config, limit)  # no lock held
            outcome, fingerprint, inputs = await self._locked(config, limit, prepared)
        except _DB_ERRORS as exc:
            raise CalibrationStoreError(
                "the calibration refresh failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc
        # committed: remember the run set judged (F-2)
        self._seen = _Seen(fingerprint=fingerprint, limit=limit, inputs=inputs)
        return outcome

    async def _read_matched(self, db: AsyncSession, limit: int) -> list[MatchedRun]:
        outcomes = await read_outcomes(db, limit)
        self.full_reads += 1
        # pure CPU work off the event loop; the lock stays held (the
        # transaction is this coroutine's), nothing else uses ``db``
        return await asyncio.to_thread(_match_all, outcomes)

    async def _locked(
        self, config: CalibrationConfig, limit: int, prepared: _Prepared
    ) -> tuple[RefreshResult, tuple[int, str | None], dict[str, Any]]:
        """The locked half of :meth:`refresh`: ``(result, fingerprint the run
        set was read under, its inputs)``; one transaction, committed on the
        ``async with`` exit."""
        async with self._sessions() as db, db.begin():
            await db.execute(_LOCK)
            fingerprint = await window_fingerprint(db, limit)  # before any read
            reused = fingerprint == prepared.fingerprint
            # F-2 (b): an unchanged window -> the prepared run set, no re-read
            runs = prepared.runs if reused else await self._read_matched(db, limit)
            inputs = _fit_set(runs, config)
            if await _newest_has(db, inputs) is not None:
                skip = RefreshResult(
                    matched_runs=len(runs), row=None, skipped="no_new_runs"
                )
                return skip, fingerprint, inputs
            current = (
                await db.scalars(
                    select(PlannerCalibration)
                    .where(PlannerCalibration.status == "active")
                    .with_for_update()
                )
            ).first()
            active_version = None if current is None else int(current.version)
            if prepared.inputs == inputs and prepared.active_version == active_version:
                result, note = prepared.result, prepared.note
            else:  # something changed while unlocked: judge it again
                if reused:  # a fresh read, as before F-2 (run records may differ)
                    runs = await self._read_matched(db, limit)
                    inputs = _fit_set(runs, config)
                active = None if current is None else _row(current).calibration
                result, note = await self._fit_and_check(runs, config, active)
            if result is None:
                skipped = "too_few_runs" if len(runs) < MIN_RUNS else "nothing_to_fit"
                none = RefreshResult(matched_runs=len(runs), row=None, skipped=skipped)
                return none, fingerprint, inputs
            stored = await self._store(
                db, result, current, len(runs), inputs, note=note
            )
            return stored, fingerprint, inputs

    @staticmethod
    async def _store(
        db: AsyncSession,
        result: Refit,
        current: PlannerCalibration | None,
        matched: int,
        inputs: Mapping[str, Any],
        *,
        note: str | None = None,
    ) -> RefreshResult:
        """``note`` (V0.14 G4, D319): why an accepted row's regret check was
        skipped - stored as its ``reason`` (a rejected row's reason is the
        gate's)."""
        accepted = result.gate.accepted
        superseded: int | None = None
        if accepted and current is not None:
            current.status = "superseded"
            superseded = int(current.version)
            await db.flush()  # the index allows one active row at a time
        reason = result.gate.reason if not accepted else note
        item = PlannerCalibration(
            status="active" if accepted else "rejected",
            factors=calibration_json(result.calibration),
            fit_inputs={**result.fit_inputs, **inputs},
            holdout_error_new=result.holdout_error_new,
            holdout_error_active=result.holdout_error_active,
            reason=None if reason is None else reason[:MAX_CALIBRATION_REASON_CHARS],
        )
        db.add(item)
        await db.flush()
        await db.refresh(item)
        return RefreshResult(
            matched_runs=matched, row=_row(item), superseded=superseded
        )

    async def _one(self, query: Any) -> PlannerCalibration | None:
        try:
            async with self._sessions() as db:
                found: PlannerCalibration | None = (await db.scalars(query)).first()
                return found
        except _DB_ERRORS as exc:
            raise CalibrationStoreError(
                "the calibration read failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc

    async def row(self, version: int) -> CalibrationRow | None:
        found = await self._one(
            select(PlannerCalibration).where(PlannerCalibration.version == version)
        )
        return None if found is None else _row(found)

    async def active_row(self) -> CalibrationRow | None:
        found = await self._one(
            select(PlannerCalibration).where(PlannerCalibration.status == "active")
        )
        return None if found is None else _row(found)

    async def active_version(self) -> int | None:
        try:
            async with self._sessions() as db:
                version = (
                    await db.scalars(
                        select(PlannerCalibration.version).where(
                            PlannerCalibration.status == "active"
                        )
                    )
                ).first()
        except _DB_ERRORS as exc:
            raise CalibrationStoreError(
                "the calibration read failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc
        return None if version is None else int(version)

    async def load(self, version: int) -> Calibration | None:
        """The calibration of ``version``; None when there is no such row or
        its factors cannot be read."""
        found = await self.row(version)
        return None if found is None else found.calibration

    async def recent(self, limit: int = 20) -> list[CalibrationRow]:
        try:
            async with self._sessions() as db:
                rows = list(
                    await db.scalars(
                        select(PlannerCalibration)
                        .order_by(PlannerCalibration.version.desc())
                        .limit(max(1, min(limit, 200)))
                    )
                )
        except _DB_ERRORS as exc:
            raise CalibrationStoreError(
                "the calibration read failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc
        return [_row(item) for item in rows]


def parse_mode(value: str) -> tuple[CalibrationMode, int | None]:
    """``[controller.planner] calibration`` -> (mode, pinned version)."""
    if value in ("auto", "off"):
        return value, None  # type: ignore[return-value]
    return "pinned", int(value)


class CalibrationSource:
    """See module docstring. Lives in the dispatcher's task."""

    def __init__(
        self,
        store: CalibrationStore,
        setting: str,
    ) -> None:
        self._store = store
        self.mode, self.pinned = parse_mode(setting)
        self.current: Calibration | None = None
        self.failures = 0
        """Reads that failed (the version in use was kept)."""
        self._failing = False
        self._unreadable: int | None = None
        """A version whose factors could not be read (not re-read per tick)."""

    async def check(self) -> None:
        """Startup (D301): a pinned version must exist, be readable and not
        be ``rejected`` (a rejected fit failed the holdout gate and is never
        used) - :class:`ConfigError` otherwise, never a silent fallback."""
        if self.mode != "pinned" or self.pinned is None:
            return
        found = await self._store.row(self.pinned)
        if found is None:
            raise ConfigError(
                f"[controller.planner] calibration = {self.pinned}: no such "
                "calibration version (see compute planner calibration)"
            )
        if found.calibration is None:
            raise ConfigError(
                f"[controller.planner] calibration = {self.pinned}: that "
                "version's factors cannot be read"
            )
        if found.status == "rejected":
            raise ConfigError(
                f"[controller.planner] calibration = {self.pinned}: that "
                "version was rejected by the holdout gate and is never used "
                "(pin an active or superseded version)"
            )
        self.current = found.calibration

    async def refresh(self) -> Calibration | None:
        """The calibration to plan this tick with (module docstring)."""
        if self.mode == "off":
            return None
        try:
            if self.mode == "pinned":
                if (
                    self.current is None
                    and self.pinned is not None
                    and self._unreadable != self.pinned
                ):
                    self.current = await self._pinned(self.pinned)
            else:
                version = await self._store.active_version()
                if version is None:
                    self.current = None
                elif version == self._unreadable:
                    self.current = None  # known bad: logged once, not re-read
                elif self.current is None or self.current.version != version:
                    loaded = await self._store.load(version)
                    if loaded is None:
                        self._mark_unreadable(version)
                    self.current = loaded
        except CalibrationStoreError as exc:
            self.failures += 1
            if not self._failing:
                log.warning(
                    "controller.planner.calibration_read_failed",
                    cause=exc.details.get("cause"),
                    kept_version=None if self.current is None else self.current.version,
                )
            self._failing = True
            return self.current
        except Exception as exc:  # a bug here must never stop the dispatch
            self.failures += 1
            if not self._failing:
                log.exception(
                    "controller.planner.calibration_read_crashed",
                    error_type=type(exc).__name__,
                )
            self._failing = True
            return self.current
        self._failing = False
        return self.current

    async def _pinned(self, version: int) -> Calibration | None:
        """The pinned version (when :meth:`check` did not load it): never a
        missing, unreadable or ``rejected`` one."""
        found = await self._store.row(version)
        if found is None or found.calibration is None or found.status == "rejected":
            self._mark_unreadable(version)
            return None
        return found.calibration

    def _mark_unreadable(self, version: int) -> None:
        if self._unreadable != version:
            log.error(
                "controller.planner.calibration_unreadable",
                calibration_version=version,
            )
        self._unreadable = version


def row_summary(row: CalibrationRow) -> dict[str, Any]:
    """A row as a log-safe summary (no factors)."""
    return {
        "calibration_version": row.version,
        "status": row.status,
        "holdout_error_new": row.holdout_error_new,
        "holdout_error_active": row.holdout_error_active,
        "reason": row.reason,
        "pairs": row.fit_inputs.get("pairs"),
    }


def factor_rows(calibration: Calibration) -> list[dict[str, Any]]:
    """Every factor as a flat row ``{term, key_level, node_id, family, factor,
    n, median}`` (the CLI's table and the API's list), sorted."""
    out: list[dict[str, Any]] = []
    for term, table in sorted(calibration.terms.items()):
        if table.global_ is not None:
            out.append(_flat(term, "global", None, None, table.global_))
        for node_id, factor in sorted(table.node.items()):
            out.append(_flat(term, "node", node_id, None, factor))
        for node_id, by in sorted(table.node_family.items()):
            for family, factor in sorted(by.items()):
                out.append(_flat(term, "node_family", node_id, family, factor))
    return out


def _flat(
    term: str,
    level: str,
    node_id: str | None,
    family: str | None,
    factor: Any,
) -> dict[str, Any]:
    return {
        "term": term,
        "key_level": level,
        "node_id": node_id,
        "family": family,
        "factor": factor.factor,
        "n": factor.n,
        "median": factor.median,
    }
