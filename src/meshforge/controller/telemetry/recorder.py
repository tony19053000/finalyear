"""The run-record writer: a reconciler over terminal attempts (V0.9 G1, D211/D212).

**Write path - a reconciler, not the terminal transaction.** An attempt reaches
a terminal state in many places (the agent's DONE/FAILED report, node loss,
the restart recovery, the stuck-attempt backstops, cancellation, fencing by a
retry). Building the record inside each of those transactions would put
telemetry on every hot path and one missed path would silently lose records.
Instead :class:`RunRecorder` sweeps **by state**: every terminal attempt with no
``run_record`` row is a record to write -

    SELECT ... FROM attempt
    WHERE state IN ('done','failed','lost','fenced')
      AND NOT EXISTS (SELECT 1 FROM run_record r WHERE r.attempt_id = attempt.id)

so it cannot miss one: a controller crash between the terminal commit and the
record just leaves the attempt for the next sweep (also after a restart), and a
bug in the builder never rolls back a state transition. Each batch is one
transaction: read the rows, build the records with the pure
:func:`~.record.build_record`, ``INSERT ... ON CONFLICT (attempt_id) DO
NOTHING`` (idempotent: two controllers or two sweeps cannot write a record
twice, and a written record is never changed). The cost is a delay of at most
``[controller.telemetry] record_interval_s`` (2 s), or less when
:meth:`RunRecorder.nudge` is called after a terminal report.

**One bad record never jams the sweep.** The batch INSERT runs in a savepoint;
if PostgreSQL refuses it for its *data* (SQLSTATE class 22 data exception or 23
constraint violation - the builder already bounds and scrubs untrusted values,
so this is a last line), the batch is retried row by row, each in its own
savepoint, and only the refused rows are dropped. A record that cannot be built
(a bug) or that PostgreSQL refuses is **skipped** until the controller
restarts, and made visible: counted (:attr:`RunRecorder.skipped`, ids in
:attr:`RunRecorder.skipped_attempts`), logged, and recorded as a
``telemetry_record_skipped`` audit row - an operator alert (``GET /v1/alerts``)
written in the sweep's own transaction. Any other database error (connection
lost, ...) fails the sweep as before and the next one retries everything.

The "at start" values are in the attempt's start snapshot, written at dispatch
(``jobs/spec.py``) - the sweep never reads the *current* node, cache or network
state for them.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import exists, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, SQLAlchemyError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.errors import ExitCode, FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.states import ATTEMPT_TERMINAL_STATES
from meshforge.controller.enrollment.invites import audit
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.data import TransferEvent
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.models.job import Attempt, AttemptPhaseRow, Job
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.controller.store.models.telemetry import RunRecord
from meshforge.controller.telemetry.record import (
    PhaseValue,
    RecordSource,
    TransferRow,
    build_record,
)

__all__ = ["DEFAULT_BATCH", "CampaignLookup", "RunRecorder", "TelemetryStoreError"]

log = get_logger(__name__)

DEFAULT_BATCH: Final = 200
_MAX_UNBUILDABLE: Final = 1_000
_TERMINAL: Final = tuple(sorted(s.value for s in ATTEMPT_TERMINAL_STATES))
type CampaignLookup = Callable[
    [AsyncSession, Collection[str]], Awaitable[Mapping[str, Mapping[str, Any]]]
]
"""``(db, job_ids) -> {job_id: {campaign_id, group_id, config_id, repetition}}``."""
_REFUSED_SQLSTATE_CLASSES: Final = ("22", "23")
"""PostgreSQL refused the row for its data (data exception, constraint): the
row is at fault, not the database - isolate it."""


class TelemetryStoreError(MeshForgeError):
    """The database refused or lost a telemetry read/write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR
    exit_code = ExitCode.UNAVAILABLE


class RunRecorder:
    """See the module docstring. One per controller process."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        interval_s: float = 2.0,
        batch: int = DEFAULT_BATCH,
        campaign_of: CampaignLookup | None = None,
    ) -> None:
        if interval_s <= 0 or batch < 1:
            raise ValueError("interval_s must be > 0 and batch >= 1")
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )
        self._interval_s = interval_s
        self._batch = batch
        self._campaign_of = campaign_of
        """V0.9 G3 hook: the batch's job ids -> campaign identity, read in the
        sweep's own transaction (a job absent from the answer is not a
        benchmark); wired to ``controller.bench.store.campaign_identities``."""
        self._wake = asyncio.Event()
        self.written = 0
        self.failed_sweeps = 0
        self.skipped = 0
        """Attempts skipped since start (unbuildable or refused by PostgreSQL)."""
        self._skip: set[str] = set()
        self._pending_skips: list[str] = []
        """Skips of the running sweep, applied when its transaction commits."""

    @property
    def skipped_attempts(self) -> frozenset[str]:
        """Ids of the attempts skipped until restart (at most 1000 kept)."""
        return frozenset(self._skip)

    async def _skipped(
        self, db: AsyncSession, source: _Skip, *, reason: str, error_type: str
    ) -> None:
        """Skip ``source`` until restart: log it and add the
        ``telemetry_record_skipped`` alert row to ``db``'s transaction. It is
        counted and excluded from later sweeps only once that transaction
        commits (:meth:`sweep`): if it rolls back, the alert row is gone too,
        so the attempt is tried - and alerted - again by the next sweep."""
        self._pending_skips.append(source.attempt_id)
        log.error(
            "controller.telemetry.record_skipped",
            attempt_id=source.attempt_id,
            job_id=source.job_id,
            reason=reason,
            error_type=error_type,
        )
        await audit(
            db,
            AuditAction.TELEMETRY_RECORD_SKIPPED,
            actor="controller",
            details={
                "attempt_id": source.attempt_id,
                "job_id": source.job_id,
                "reason": reason,
                "error_type": error_type,
            },
        )

    async def _build(
        self, db: AsyncSession, sources: Sequence[RecordSource]
    ) -> list[dict[str, Any]]:
        """Records of ``sources``; one that cannot be built (a bug, never a
        normal outcome) is skipped until the controller restarts, so it cannot
        block the sweep for every later attempt."""
        records: list[dict[str, Any]] = []
        for source in sources:
            try:
                records.append(build_record(source))
            except Exception as exc:
                log.exception(
                    "controller.telemetry.record_unbuildable",
                    attempt_id=source.attempt_id,
                    job_id=source.job_id,
                    error_type=type(exc).__name__,
                )
                await self._skipped(
                    db,
                    _Skip(source.attempt_id, source.job_id),
                    reason="unbuildable",
                    error_type=type(exc).__name__,
                )
        return records

    async def _insert(self, db: AsyncSession, records: list[dict[str, Any]]) -> int:
        """Insert ``records``; one PostgreSQL refuses is skipped alone (see
        the module docstring). The number inserted."""
        try:
            async with db.begin_nested():
                return await _insert_rows(db, records)
        except StatementError as exc:
            if not _refused(exc):
                raise
        count = 0
        for record in records:
            try:
                async with db.begin_nested():
                    count += await _insert_rows(db, [record])
            except StatementError as exc:
                if not _refused(exc):
                    raise
                await self._skipped(
                    db,
                    _Skip(str(record["attempt_id"]), str(record["job_id"])),
                    reason="refused",
                    error_type=_error_type(exc),
                )
        return count

    def nudge(self) -> None:
        """Sweep soon (an attempt just became terminal)."""
        self._wake.set()

    async def sweep(self) -> int:
        """Write the records of up to ``batch`` terminal attempts that have none;
        the number written. Raises :class:`TelemetryStoreError`."""
        self._pending_skips = []
        try:
            async with self._sessions() as db, db.begin():
                attempts = list(
                    await db.scalars(
                        select(Attempt)
                        .where(
                            Attempt.state.in_(_TERMINAL),
                            ~exists().where(RunRecord.attempt_id == Attempt.id),
                            Attempt.id.not_in(sorted(self._skip)),
                        )
                        .order_by(Attempt.created_at, Attempt.id)
                        .limit(self._batch)
                    )
                )
                if not attempts:
                    return 0
                rows = await _sources(db, attempts, self._campaign_of)
                records = await self._build(db, rows)
                count = await self._insert(db, records) if records else 0
        except SQLAlchemyError as exc:
            raise TelemetryStoreError(
                "the database refused the telemetry sweep",
                details={"cause": type(exc).__name__},
            ) from exc
        for attempt_id in self._pending_skips:  # committed with their alert rows
            if len(self._skip) < _MAX_UNBUILDABLE:
                self._skip.add(attempt_id)
            self.skipped += 1
        self._pending_skips = []
        self.written += count
        if count:
            log.info("controller.telemetry.records_written", count=count)
        return count

    async def run(self) -> None:
        """Sweep until cancelled: at once, then every ``interval_s`` or on a
        nudge; a full batch sweeps again at once (a backlog drains quickly)."""
        while True:
            try:
                written = await self.sweep()
            except TelemetryStoreError as exc:
                self.failed_sweeps += 1
                log.warning(
                    "controller.telemetry.sweep_failed",
                    cause=exc.details.get("cause"),
                    failures=self.failed_sweeps,
                )
                written = 0
            except Exception as exc:
                self.failed_sweeps += 1
                log.exception(
                    "controller.telemetry.sweep_crashed",
                    error_type=type(exc).__name__,
                    failures=self.failed_sweeps,
                )
                written = 0
            if written >= self._batch:
                continue
            self._wake.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), self._interval_s)


@dataclass(frozen=True, slots=True)
class _Skip:
    attempt_id: str
    job_id: str


async def _insert_rows(db: AsyncSession, records: list[dict[str, Any]]) -> int:
    inserted = await db.scalars(
        pg_insert(RunRecord)
        .values(records)
        .on_conflict_do_nothing(index_elements=["attempt_id"])
        .returning(RunRecord.attempt_id)
    )
    return len(list(inserted))


def _sqlstate(exc: StatementError) -> str | None:
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    return state if isinstance(state, str) else None


def _refused(exc: StatementError) -> bool:
    """PostgreSQL refused the row for its data - not a lost connection or
    another database failure. A :class:`StatementError` that is not a DBAPI
    error is a value the driver could not even bind: the row's fault too."""
    if not isinstance(exc, DBAPIError):
        return True
    if exc.connection_invalidated:
        return False
    state = _sqlstate(exc)
    return state is not None and state[:2] in _REFUSED_SQLSTATE_CLASSES


def _error_type(exc: StatementError) -> str:
    state = _sqlstate(exc)
    name = type(exc.orig).__name__ if exc.orig is not None else type(exc).__name__
    return f"{name} ({state})" if state else name


async def _sources(
    db: AsyncSession,
    attempts: Sequence[Attempt],
    campaign_of: CampaignLookup | None,
) -> list[RecordSource]:
    """Everything :func:`build_record` needs for ``attempts``, in four reads."""
    ids = [a.id for a in attempts]
    job_ids = sorted({a.job_id for a in attempts})
    jobs = {j.id: j for j in await db.scalars(select(Job).where(Job.id.in_(job_ids)))}
    phases: dict[str, dict[str, PhaseValue]] = defaultdict(dict)
    for row in await db.scalars(
        select(AttemptPhaseRow).where(AttemptPhaseRow.attempt_id.in_(ids))
    ):
        phases[row.attempt_id][row.phase] = PhaseValue(
            int(row.duration_ms), bool(row.skipped)
        )
    decisions = {
        d.attempt_id: d
        for d in await db.scalars(
            select(PlanDecision).where(
                PlanDecision.attempt_id.in_(ids), PlanDecision.outcome == "dispatched"
            )
        )
    }
    transfers: dict[str, list[TransferRow]] = defaultdict(list)
    for event in await db.scalars(
        select(TransferEvent).where(TransferEvent.attempt_id.in_(ids))
    ):
        transfers[event.attempt_id].append(TransferRow(event.source, int(event.bytes)))
    campaigns = {} if campaign_of is None else await campaign_of(db, job_ids)
    sources: list[RecordSource] = []
    for attempt in attempts:
        job = jobs[attempt.job_id]
        decision = decisions.get(attempt.id)
        sources.append(
            RecordSource(
                attempt_id=attempt.id,
                job_id=attempt.job_id,
                attempt_no=int(attempt.fencing_token),
                node_id=attempt.node_id,
                state=attempt.state,
                created_at=attempt.created_at,
                ended_at=attempt.ended_at,
                failure_type=attempt.failure_type,
                exit_code=attempt.exit_code,
                runtime=attempt.runtime,
                output_bytes=None
                if attempt.output_bytes is None
                else int(attempt.output_bytes),
                start_snapshot=attempt.start_snapshot,
                workload=attempt.workload,
                manifest=job.manifest,
                placement=job.placement,
                submitter_node_id=job.submitter_node_id,
                job_policy=job.policy,
                fingerprint=job.fingerprint,
                phases=phases.get(attempt.id, {}),
                decision_policy=None if decision is None else decision.policy,
                decision_chosen=None if decision is None else decision.chosen_node_id,
                decision_candidates=() if decision is None else decision.candidates,
                transfers=transfers.get(attempt.id, ()),
                decision_shadow=None
                if decision is None
                else decision.shadow_chosen_node_id,
                campaign=campaigns.get(attempt.job_id),
            )
        )
    return sources
