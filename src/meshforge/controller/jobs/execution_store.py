"""The dispatch-time writes of the job tables (V0.4 G4: D65, D66, D68, D69).

:class:`ExecutionStore` extends :class:`~.store.JobStore` - still the one writer of
``job`` / ``job_transition`` / ``attempt`` / ``attempt_phase`` - with the
operations the dispatcher needs. Each is **one transaction** that locks the rows
it changes (``FOR UPDATE``) and derives every arrow from :mod:`.transitions` via
:mod:`.progress`, so two writers racing on one attempt serialise on its row and
the loser sees the winner's state:

* :meth:`ExecutionStore.dispatch_queued` - ``SELECT ... WHERE state = 'queued'
  ... FOR UPDATE SKIP LOCKED`` (D62-2), a caller decision per job, and for each
  dispatched job the new attempt (next fencing token), ``QUEUED -> PREPARING``
  and its start snapshot - all committed **before** the caller sends anything
  (D65: commit-before-send);
* :meth:`ExecutionStore.apply_status` - an agent report: fenced on the attempt's
  token, idempotent on its state, the job moved with it, and - when the attempt
  ends - all five phase rows written in the same transaction (D68);
* :meth:`ExecutionStore.end_attempt` - the controller ends an attempt itself
  (``LOST`` when its node left, ``FAILED`` by the timeout backstop);
* :meth:`ExecutionStore.request_cancel` - D69;
* :meth:`ExecutionStore.mark_start_acked`, :meth:`ExecutionStore.live_attempts`,
  :meth:`ExecutionStore.overdue_running` - bookkeeping and reads.

History rows written together in one transaction are stamped with
``clock_timestamp()`` so their order and gaps are real (D62-2).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    AttemptState,
    JobState,
)
from meshforge.controller.jobs.progress import (
    AgentPhases,
    StaleReportError,
    attempt_path,
    job_event_for,
    phase_rows,
)
from meshforge.controller.jobs.store import (
    AttemptRecord,
    JobRecord,
    JobStore,
    append_transition,
    attempt_record,
    clean_detail,
    job_record,
    typed_store_call,
)
from meshforge.controller.jobs.transitions import (
    AttemptEvent,
    IllegalTransitionError,
    JobEvent,
    next_attempt_state,
    next_job_state,
)
from meshforge.controller.store.models.job import (
    Attempt,
    AttemptPhaseRow,
    Job,
    JobTransition,
)
from meshforge.controller.store.models.node import Node

__all__ = [
    "CancelOutcome",
    "Dispatch",
    "DispatchFailure",
    "DispatchResult",
    "ExecutionStore",
    "LiveAttempt",
    "NodeView",
    "StatusKind",
    "StatusOutcome",
    "StatusReport",
]

log = get_logger(__name__)

LIVE_ATTEMPT_STATES: Final = tuple(
    s.value for s in AttemptState if s not in ATTEMPT_TERMINAL_STATES
)
_CANCELLABLE_NOW: Final = frozenset(
    {JobState.SUBMITTED, JobState.PLANNING, JobState.QUEUED, JobState.RETRYING}
)
_CANCEL_VIA_AGENT: Final = frozenset(
    {JobState.PREPARING, JobState.RUNNING, JobState.COLLECTING}
)


# ------------------------------------------------------------------- inputs


@dataclass(frozen=True, slots=True)
class NodeView:
    """What the dispatch decision may read about the placed node's row."""

    node_id: str
    state: str
    hardware: Mapping[str, Any]
    hardware_scores: Mapping[str, Any] | None
    active_attempts: int
    """Non-terminal attempts on this node (any job), incl. this batch's."""


@dataclass(frozen=True, slots=True)
class Dispatch:
    attempt_id: str
    start_snapshot: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class DispatchFailure:
    failure_type: FailureType
    detail: str


type DispatchDecision = Dispatch | DispatchFailure | None
"""``None`` = leave the job queued (try again on a later tick)."""

type Decide = Callable[[JobRecord, NodeView | None], DispatchDecision]


@dataclass(frozen=True, slots=True)
class DispatchResult:
    job: JobRecord
    attempt: AttemptRecord | None
    """Set when an attempt was created (the caller now sends StartAttempt)."""
    failure: DispatchFailure | None = None


@dataclass(frozen=True, slots=True)
class StatusReport:
    """One validated ``AttemptStatus`` from an agent."""

    attempt_id: str
    fencing_token: int
    state: AttemptState
    failure_type: FailureType | None = None
    detail: str | None = None
    exit_code: int | None = None
    runtime: str | None = None
    phases: AgentPhases | None = None
    output_digest: str | None = None
    """Only on DONE, and only once the dispatcher verified this attempt's upload
    grant stored it (D73/D74)."""
    missing_outputs: tuple[str, ...] = ()


class StatusKind(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    """Equal to or behind the stored state (a replay, a reorder): no change."""
    FENCED = "fenced"
    """Stale token, or the attempt already ended differently: dropped."""
    UNKNOWN = "unknown"
    """No such attempt."""


@dataclass(frozen=True, slots=True)
class StatusOutcome:
    kind: StatusKind
    attempt: AttemptRecord | None = None
    job: JobRecord | None = None
    ended: bool = False
    """This call moved the attempt into a terminal state."""


@dataclass(frozen=True, slots=True)
class LiveAttempt:
    attempt: AttemptRecord
    job: JobRecord


@dataclass(frozen=True, slots=True)
class CancelOutcome:
    job: JobRecord
    attempt: AttemptRecord | None
    """The live attempt the caller must send ``CancelAttempt`` for, if any."""
    cancelled_now: bool
    """The job went to CANCELLED in this call (it had no live attempt)."""


# -------------------------------------------------------------------- store


class ExecutionStore(JobStore):
    """:class:`JobStore` plus the dispatch-time writes (see module docstring)."""

    @typed_store_call
    async def dispatch_queued(
        self, decide: Decide, *, limit: int, max_per_node: int
    ) -> list[DispatchResult]:
        """One pass over queued jobs, oldest first; see module docstring."""
        results: list[DispatchResult] = []
        async with self._sessions() as db, db.begin():
            jobs = list(
                await db.scalars(
                    select(Job)
                    .where(Job.state == JobState.QUEUED.value)
                    .order_by(Job.created_at, Job.id)
                    .limit(max(1, limit))
                    .with_for_update(skip_locked=True)
                )
            )
            if not jobs:
                return results
            active = await _active_by_node(db)
            for row in jobs:
                job = job_record(row)
                node_id = str(job.placement.get("node_id", ""))
                node_row = await db.get(Node, node_id) if node_id else None
                view = (
                    None
                    if node_row is None
                    else NodeView(
                        node_id=node_row.node_id,
                        state=node_row.state,
                        hardware=dict(node_row.hardware),
                        hardware_scores=node_row.hardware_scores,
                        active_attempts=active.get(node_id, 0),
                    )
                )
                decision = decide(job, view)
                if isinstance(decision, Dispatch):
                    if view is None or view.active_attempts >= max_per_node:
                        continue  # the decider must not over-book; defensive
                    attempt = await self._create_dispatched(db, row, node_id, decision)
                    active[node_id] = active.get(node_id, 0) + 1
                    await _fresh(db, row)
                    results.append(DispatchResult(job=job_record(row), attempt=attempt))
                elif isinstance(decision, DispatchFailure):
                    await _move_job(
                        db,
                        row,
                        JobEvent.FAIL,
                        failure_type=decision.failure_type,
                        detail=decision.detail,
                    )
                    await _fresh(db, row)
                    results.append(
                        DispatchResult(
                            job=job_record(row), attempt=None, failure=decision
                        )
                    )
        for result in results:
            _log_dispatch(result)
        return results

    async def _create_dispatched(
        self, db: AsyncSession, row: Job, node_id: str, decision: Dispatch
    ) -> AttemptRecord:
        row.attempt_epoch = int(row.attempt_epoch) + 1
        attempt = Attempt(
            id=decision.attempt_id,
            job_id=row.id,
            node_id=node_id,
            fencing_token=row.attempt_epoch,
            state=AttemptState.ASSIGNED.value,
            start_snapshot=dict(decision.start_snapshot),
        )
        db.add(attempt)
        await _move_job(db, row, JobEvent.DISPATCHED)
        await _fresh(db, attempt)
        return attempt_record(attempt)

    @typed_store_call
    async def mark_start_acked(self, attempt_id: str, fencing_token: int) -> bool:
        """Record the agent's Ack of ``StartAttempt`` (first one wins)."""
        async with self._sessions() as db, db.begin():
            done = await db.scalar(
                update(Attempt)
                .where(
                    Attempt.id == attempt_id,
                    Attempt.fencing_token == fencing_token,
                    Attempt.start_acked_at.is_(None),
                )
                .values(start_acked_at=func.clock_timestamp())
                .returning(Attempt.id)
                .execution_options(synchronize_session=False)
            )
        return done is not None

    @typed_store_call
    async def apply_status(self, report: StatusReport) -> StatusOutcome:
        """Apply one agent report (see module docstring); never raises for a
        stale/duplicate/unknown report - that is the outcome's ``kind``."""
        async with self._sessions() as db, db.begin():
            row = await db.get(Attempt, report.attempt_id, with_for_update=True)
            if row is None:
                return StatusOutcome(StatusKind.UNKNOWN)
            if int(row.fencing_token) != report.fencing_token:
                return StatusOutcome(StatusKind.FENCED, attempt=attempt_record(row))
            try:
                path = attempt_path(AttemptState(row.state), report.state)
            except StaleReportError:
                return StatusOutcome(StatusKind.FENCED, attempt=attempt_record(row))
            if not path:
                return StatusOutcome(StatusKind.DUPLICATE, attempt=attempt_record(row))
            job = await db.get_one(Job, row.job_id, with_for_update=True)
            for event, _ in path:
                await _move_attempt(db, row, event, report)
                reached = AttemptState(row.state)
                job_event = job_event_for(reached, report.failure_type)
                if job_event is not None:
                    await _move_job(
                        db,
                        job,
                        job_event,
                        failure_type=report.failure_type,
                        detail=report.detail,
                        soft=True,
                    )
            ended = AttemptState(row.state) in ATTEMPT_TERMINAL_STATES
            if ended:
                await _write_phases(db, row, report.phases)
            await _fresh(db, row, job)
            return StatusOutcome(
                StatusKind.APPLIED,
                attempt=attempt_record(row),
                job=job_record(job),
                ended=ended,
            )

    @typed_store_call
    async def end_attempt(
        self,
        attempt_id: str,
        fencing_token: int,
        *,
        event: AttemptEvent,
        failure_type: FailureType,
        detail: str,
    ) -> StatusOutcome:
        """The controller ends a live attempt itself: ``LOSE`` (node gone) or
        ``FAIL`` (backstop). A no-op outcome when it already ended."""
        if event not in (AttemptEvent.LOSE, AttemptEvent.FAIL):
            raise ValueError("end_attempt takes LOSE or FAIL")
        async with self._sessions() as db, db.begin():
            row = await db.get(Attempt, attempt_id, with_for_update=True)
            if row is None:
                return StatusOutcome(StatusKind.UNKNOWN)
            if int(row.fencing_token) != fencing_token:
                return StatusOutcome(StatusKind.FENCED, attempt=attempt_record(row))
            if AttemptState(row.state) in ATTEMPT_TERMINAL_STATES:
                return StatusOutcome(StatusKind.DUPLICATE, attempt=attempt_record(row))
            job = await db.get_one(Job, row.job_id, with_for_update=True)
            report = StatusReport(
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                state=AttemptState.FAILED,
                failure_type=failure_type,
                detail=detail,
            )
            await _move_attempt(db, row, event, report)
            await _move_job(
                db,
                job,
                JobEvent.FAIL,
                failure_type=failure_type,
                detail=detail,
                soft=True,
            )
            await _write_phases(db, row, None)
            await _fresh(db, row, job)
            return StatusOutcome(
                StatusKind.APPLIED,
                attempt=attempt_record(row),
                job=job_record(job),
                ended=True,
            )

    @typed_store_call
    async def live_attempts(self, node_id: str | None = None) -> list[LiveAttempt]:
        """Non-terminal attempts (of one node, or all) with their jobs."""
        query = (
            select(Attempt, Job)
            .join(Job, Job.id == Attempt.job_id)
            .where(Attempt.state.in_(LIVE_ATTEMPT_STATES))
            .order_by(Attempt.created_at)
        )
        if node_id is not None:
            query = query.where(Attempt.node_id == node_id)
        async with self._sessions() as db:
            rows = (await db.execute(query)).all()
            return [LiveAttempt(attempt_record(a), job_record(j)) for a, j in rows]

    @typed_store_call
    async def overdue_running(self, grace_s: float) -> list[LiveAttempt]:
        """RUNNING attempts older than their job's time limit + ``grace_s`` (D69)."""
        limit_s = Job.manifest["limits"]["timeout"].as_integer()
        query = (
            select(Attempt, Job)
            .join(Job, Job.id == Attempt.job_id)
            .where(
                Attempt.state == AttemptState.RUNNING.value,
                Attempt.started_at.is_not(None),
                Attempt.started_at
                + func.make_interval(0, 0, 0, 0, 0, 0, limit_s + grace_s)
                < func.now(),
            )
        )
        async with self._sessions() as db:
            rows = (await db.execute(query)).all()
            return [LiveAttempt(attempt_record(a), job_record(j)) for a, j in rows]

    @typed_store_call
    async def request_cancel(self, job_id: str) -> CancelOutcome | None:
        """D69. ``None`` = no such job. Idempotent: a terminal job is unchanged."""
        async with self._sessions() as db, db.begin():
            job = await db.get(Job, job_id, with_for_update=True)
            if job is None:
                return None
            state = JobState(job.state)
            if state in _CANCELLABLE_NOW:
                await _move_job(db, job, JobEvent.CANCEL)
                await _fresh(db, job)
                log.info(
                    "controller.jobs.cancelled", job_id=job_id, from_state=state.value
                )
                return CancelOutcome(job_record(job), None, cancelled_now=True)
            if state in _CANCEL_VIA_AGENT:
                if job.cancel_requested_at is None:
                    job.cancel_requested_at = dt.datetime.now(dt.UTC)
                attempt = await db.scalar(
                    select(Attempt)
                    .where(
                        Attempt.job_id == job_id,
                        Attempt.state.in_(LIVE_ATTEMPT_STATES),
                    )
                    .order_by(Attempt.fencing_token.desc())
                    .limit(1)
                )
                await _fresh(db, job)
                return CancelOutcome(
                    job_record(job),
                    None if attempt is None else attempt_record(attempt),
                    cancelled_now=False,
                )
            return CancelOutcome(job_record(job), None, cancelled_now=False)

    @typed_store_call
    async def history(
        self, job_id: str
    ) -> list[tuple[JobState | None, JobState, dt.datetime]]:
        async with self._sessions() as db:
            return await _history(db, job_id)


# ------------------------------------------------------------------ helpers


async def _fresh(db: AsyncSession, *rows: Any) -> None:
    """Flush, then reload ``rows``: attributes set to SQL expressions
    (``clock_timestamp()``, ``onupdate``) are expired by the flush, and an
    implicit lazy load is not allowed under asyncio."""
    await db.flush()
    for row in rows:
        await db.refresh(row)


async def _active_by_node(db: AsyncSession) -> dict[str, int]:
    rows = await db.execute(
        select(Attempt.node_id, func.count())
        .where(Attempt.state.in_(LIVE_ATTEMPT_STATES))
        .group_by(Attempt.node_id)
    )
    return {str(node): int(count) for node, count in rows.all()}


async def _move_job(
    db: AsyncSession,
    row: Job,
    event: JobEvent,
    *,
    failure_type: FailureType | None = None,
    detail: str | None = None,
    soft: bool = False,
) -> bool:
    """Apply ``event`` to a job row this transaction holds locked.

    ``soft``: an arrow the job cannot take (it already moved on, e.g. CANCELLED
    by a race the lock serialised) is logged and skipped instead of raised - the
    attempt's own change still commits.
    """
    current = JobState(row.state)
    try:
        target = next_job_state(current, event)
    except IllegalTransitionError:
        if not soft:
            raise
        log.info(
            "controller.jobs.job_arrow_skipped",
            job_id=row.id,
            state=current.value,
            job_event=event.value,
        )
        return False
    row.state = target.value
    if target is JobState.FAILED:
        row.failure_type = (failure_type or FailureType.INTERNAL_ERROR).value
        row.failure_detail = clean_detail(detail)
    elif target is JobState.CANCELLED:
        row.failure_type = FailureType.CANCELLED.value
        row.failure_detail = clean_detail(detail or "cancelled by request")
    await append_transition(db, row.id, current, target, at=func.clock_timestamp())
    log.info(
        "controller.jobs.transition",
        job_id=row.id,
        job_event=event.value,
        from_state=current.value,
        to_state=target.value,
    )
    return True


async def _move_attempt(
    db: AsyncSession, row: Attempt, event: AttemptEvent, report: StatusReport
) -> None:
    current = AttemptState(row.state)
    target = next_attempt_state(current, event)
    row.state = target.value
    if report.runtime and row.runtime is None:
        row.runtime = report.runtime
    if target is AttemptState.RUNNING:
        row.started_at = func.clock_timestamp()
    if target in ATTEMPT_TERMINAL_STATES:
        row.ended_at = func.clock_timestamp()
        if report.exit_code is not None:
            row.exit_code = report.exit_code
    if target is AttemptState.DONE:
        row.output_digest = report.output_digest
        row.missing_outputs = list(report.missing_outputs) or None
    if target in (AttemptState.FAILED, AttemptState.LOST):
        row.failure_type = (report.failure_type or FailureType.INTERNAL_ERROR).value
        row.detail = clean_detail(report.detail)
    log.info(
        "controller.jobs.attempt_transition",
        job_id=row.job_id,
        attempt_id=row.id,
        node_id=row.node_id,
        fencing_token=int(row.fencing_token),
        attempt_event=event.value,
        from_state=current.value,
        to_state=target.value,
        failure_type=row.failure_type,
    )


async def _history(
    db: AsyncSession, job_id: str
) -> list[tuple[JobState | None, JobState, dt.datetime]]:
    rows = await db.scalars(
        select(JobTransition)
        .where(JobTransition.job_id == job_id)
        .order_by(JobTransition.seq)
    )
    return [
        (JobState(r.from_state) if r.from_state else None, JobState(r.to_state), r.at)
        for r in rows
    ]


async def _write_phases(
    db: AsyncSession, row: Attempt, agent: AgentPhases | None
) -> None:
    """All five rows, in the terminal transaction (D68). A phase already stored
    (a replay racing an earlier write) is left as it is."""
    await db.flush()  # clock_timestamp() values of the row above are needed below
    await db.refresh(row, ["created_at", "start_acked_at"])
    anchor = row.start_acked_at or row.created_at
    existing = set(
        await db.scalars(
            select(AttemptPhaseRow.phase).where(AttemptPhaseRow.attempt_id == row.id)
        )
    )
    for phase in phase_rows(
        history=await _history(db, row.job_id), anchor=anchor, agent=agent
    ):
        if phase.phase.value in existing:
            continue
        db.add(
            AttemptPhaseRow(
                attempt_id=row.id,
                phase=phase.phase.value,
                started_at=phase.started_at,
                duration_ms=phase.duration_ms,
                skipped=phase.skipped,
            )
        )


def _log_dispatch(result: DispatchResult) -> None:
    if result.attempt is not None:
        log.info(
            "controller.jobs.dispatched",
            job_id=result.job.id,
            attempt_id=result.attempt.id,
            node_id=result.attempt.node_id,
            fencing_token=result.attempt.fencing_token,
        )
    elif result.failure is not None:
        log.warning(
            "controller.jobs.dispatch_refused",
            job_id=result.job.id,
            node_id=str(result.job.placement.get("node_id", "")) or None,
            failure_type=result.failure.failure_type.value,
            detail=result.failure.detail,
        )


def terminal(records: Sequence[AttemptRecord]) -> list[AttemptRecord]:
    """The records that ended (test helper)."""
    return [r for r in records if r.state in ATTEMPT_TERMINAL_STATES]
