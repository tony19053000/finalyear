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
    nothing.
  - :meth:`~CalibrationStore.load`, :meth:`~CalibrationStore.active_version`,
    :meth:`~CalibrationStore.row`, :meth:`~CalibrationStore.recent`.

* :class:`CalibrationSource` - what the dispatcher plans with, once per tick:
  ``off`` -> None; a pinned version -> that version (loaded once; startup
  refuses a pin that does not exist, :meth:`CalibrationSource.check`);
  ``auto`` -> the active version, re-read by id every tick and loaded only
  when the id changed. A failed read keeps the version it had (logged once
  per failure streak): a database hiccup never blocks or changes dispatch.

**The matched runs** (one bounded read): ``dispatched`` decisions joined to
their attempt (``done``, ``ended_at`` set), the attempt's run record (workload
family, fingerprint) and the job (fingerprint when the record is not written
yet), newest first; then their phases in one ``IN`` read. The pure
:func:`~meshforge.controller.planner.calibration.match` decides what counts.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import func, select, text
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
    Refit,
    calibration_from_json,
    calibration_json,
    match,
    refit,
)
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.calibration import (
    MAX_CALIBRATION_REASON_CHARS,
    PlannerCalibration,
)
from meshforge.controller.store.models.job import Attempt, AttemptPhaseRow, Job
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.models.telemetry import RunRecord

__all__ = [
    "MAX_RUNS_READ",
    "CalibrationMode",
    "CalibrationRow",
    "CalibrationSource",
    "CalibrationStore",
    "CalibrationStoreError",
    "RefreshResult",
    "factor_rows",
    "parse_mode",
    "row_summary",
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
    """``too_few_runs`` | ``nothing_to_fit`` (no row written)."""
    superseded: int | None = None
    """The version this one replaced as ``active``."""


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


async def _outcomes(db: AsyncSession, limit: int) -> list[DecisionOutcome]:
    query = (
        select(
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
        )
        .join(Attempt, Attempt.id == PlanDecision.attempt_id)
        .join(Job, Job.id == PlanDecision.job_id)
        .outerjoin(RunRecord, RunRecord.attempt_id == Attempt.id)
        .where(
            PlanDecision.outcome == "dispatched",
            Attempt.state == "done",
            Attempt.ended_at.is_not(None),
        )
        .order_by(Attempt.ended_at.desc(), PlanDecision.id.desc())
        .limit(limit)
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

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    async def refresh(
        self, config: CalibrationConfig, *, max_runs: int | None = None
    ) -> RefreshResult:
        """Fit, gate and store one new version (module docstring).
        :class:`CalibrationStoreError` when the database fails (nothing is
        written then: one transaction)."""
        limit = max_runs or min(MAX_RUNS_READ, config.window_runs * RUNS_PER_WINDOW)
        try:
            async with self._sessions() as db, db.begin():
                await db.execute(_LOCK)
                outcomes = await _outcomes(db, max(1, limit))
                runs = [m for o in outcomes if (m := match(o)) is not None]
                current = (
                    await db.scalars(
                        select(PlannerCalibration)
                        .where(PlannerCalibration.status == "active")
                        .with_for_update()
                    )
                ).first()
                active = None if current is None else _row(current).calibration
                result = refit(runs, config, active)
                if result is None:
                    skipped = (
                        "too_few_runs" if len(runs) < MIN_RUNS else "nothing_to_fit"
                    )
                    return RefreshResult(
                        matched_runs=len(runs), row=None, skipped=skipped
                    )
                return await self._store(db, result, current, len(runs))
        except SQLAlchemyError as exc:
            raise CalibrationStoreError(
                "the calibration refresh failed in the database",
                details={"cause": type(exc).__name__},
            ) from exc

    @staticmethod
    async def _store(
        db: AsyncSession,
        result: Refit,
        current: PlannerCalibration | None,
        matched: int,
    ) -> RefreshResult:
        accepted = result.gate.accepted
        superseded: int | None = None
        if accepted and current is not None:
            current.status = "superseded"
            superseded = int(current.version)
            await db.flush()  # the index allows one active row at a time
        reason = result.gate.reason
        item = PlannerCalibration(
            status="active" if accepted else "rejected",
            factors=calibration_json(result.calibration),
            fit_inputs=dict(result.fit_inputs),
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
        except SQLAlchemyError as exc:
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
        except SQLAlchemyError as exc:
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
        except SQLAlchemyError as exc:
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

    async def check(self) -> None:
        """Startup (D301): a pinned version must exist and be readable -
        :class:`ConfigError` otherwise, never a silent fallback."""
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
        self.current = found.calibration

    async def refresh(self) -> Calibration | None:
        """The calibration to plan this tick with (module docstring)."""
        if self.mode == "off":
            return None
        try:
            if self.mode == "pinned":
                if self.current is None and self.pinned is not None:
                    self.current = await self._store.load(self.pinned)
            else:
                version = await self._store.active_version()
                if version is None:
                    self.current = None
                elif self.current is None or self.current.version != version:
                    loaded = await self._store.load(version)
                    if loaded is None:
                        log.error(
                            "controller.planner.calibration_unreadable",
                            calibration_version=version,
                        )
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
