"""The dispatch-time writes of the job tables (V0.4 G4: D65, D66, D68, D69).

:class:`ExecutionStore` extends :class:`~.store.JobStore` - still the one writer of
``job`` / ``job_transition`` / ``attempt`` / ``attempt_phase`` - with the
operations the dispatcher needs. Each is **one transaction** that locks the rows
it changes (``FOR UPDATE``) and derives every arrow from :mod:`.transitions` via
:mod:`.progress`, so two writers racing on one attempt serialise on its row and
the loser sees the winner's state:

* :meth:`ExecutionStore.dispatch_planned` - ``SELECT ... WHERE state = 'queued'
  ... FOR UPDATE SKIP LOCKED`` (D62-2) in priority order (V0.6 D106), the
  tick's fabric rows (:mod:`.queue_reads`: slot use derived from live
  attempts, D105), a caller decision per job, and for each dispatched job the
  new attempt (next fencing token, its slot), ``QUEUED -> PREPARING``, its
  start snapshot and its ``plan_decision`` row (D112; in a savepoint, so a
  row the database refuses is dropped alone and never undoes the tick, D131)
  - all committed
  **before** the caller sends anything (D65: commit-before-send). Ticks of
  overlapping dispatchers are serialised by a transaction-scoped advisory
  lock taken first, so slot use read from the database is never stale; a
  queued row that cannot be read or planned is failed on its own
  (``internal_error``) and the rest of the batch goes on;
  :meth:`ExecutionStore.dispatch_queued` is the V0.4 one-node-view interface
  over it;
* :meth:`ExecutionStore.apply_status` - an agent report: fenced on the attempt's
  token, idempotent on its state, the job moved with it, and - when the attempt
  ends - all five phase rows written in the same transaction (D68);
* :meth:`ExecutionStore.end_attempt` - the controller ends an attempt itself
  (``LOST`` when its node left, ``FAILED`` by the timeout backstop);
* **retries (V0.6 G3, D110, ADR-0015):** whenever ``apply_status`` or
  ``end_attempt`` ends a job's *current* attempt with a failure or a loss,
  :func:`_fail_or_retry` decides in **the same transaction** whether the
  job fails or goes ``RETRYING -> PLANNING -> QUEUED`` with ``not_before``
  (:mod:`.retry`); a report from an attempt that is not the job's current
  one (``fencing_token != job.attempt_epoch``) never moves the job;
* :meth:`ExecutionStore.request_cancel` - D69;
* :meth:`ExecutionStore.requeue_stranded` - D115 safety net at start;
* :meth:`ExecutionStore.mark_start_acked`, :meth:`ExecutionStore.live_attempts`,
  :meth:`ExecutionStore.overdue_running` - bookkeeping and reads.

History rows written together in one transaction are stamped with
``clock_timestamp()`` so their order and gaps are real (D62-2).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    AttemptState,
    JobState,
)
from meshforge.controller.enrollment.invites import audit
from meshforge.controller.jobs.decision_store import DecisionReads
from meshforge.controller.jobs.progress import (
    AgentPhases,
    StaleReportError,
    attempt_path,
    job_event_for,
    phase_rows,
)
from meshforge.controller.jobs.queue_reads import (
    FabricRows,
    NetworkReads,
    queued_jobs,
    read_fabric,
)
from meshforge.controller.jobs.retry import (
    RetryReason,
    RetryVerdict,
    exhausted_detail,
    first_failure_detail,
    retry_verdict,
)
from meshforge.controller.jobs.store import (
    AttemptRecord,
    JobRecord,
    TransferRow,
    append_transition,
    attempt_record,
    clean_detail,
    job_record,
    note_attempt_change,
    typed_store_call,
)
from meshforge.controller.jobs.transitions import (
    AttemptEvent,
    IllegalTransitionError,
    JobEvent,
    next_attempt_state,
    next_job_state,
)
from meshforge.controller.store.models.data import TransferEvent
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.models.job import (
    Attempt,
    AttemptPhaseRow,
    Job,
    JobTransition,
)
from meshforge.controller.store.models.plan import (
    MAX_DECISION_CANDIDATES,
    PlanDecision,
)

__all__ = [
    "CancelOutcome",
    "DecisionRecord",
    "Dispatch",
    "DispatchFailure",
    "DispatchResult",
    "ExecutionStore",
    "LiveAttempt",
    "NodeView",
    "StatusKind",
    "StatusOutcome",
    "StatusReport",
    "TransferRow",
    "Wait",
]

log = get_logger(__name__)

LIVE_ATTEMPT_STATES: Final = tuple(
    s.value for s in AttemptState if s not in ATTEMPT_TERMINAL_STATES
)
_DEFAULT_AGING_S: Final = 600.0
_DISPATCH_LOCK: Final = text(
    "SELECT pg_advisory_xact_lock(hashtextextended('meshforge.dispatch', 0))"
)
"""G2 fix D-3: slot accounting is serialised across dispatchers (an old
controller process still draining while its successor ticks). Taken as the
dispatch transaction's first statement, before any row lock, so it cannot
deadlock; released at commit, after which the next holder's reads see the
attempts this tick created."""
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
class DecisionRecord:
    """One ``plan_decision`` row to write with the tick's outcome (D112)."""

    policy: str
    snapshot_digest: str
    reason_key: str
    candidates: Sequence[Mapping[str, Any]]
    chosen_node_id: str | None = None


@dataclass(frozen=True, slots=True)
class Dispatch:
    attempt_id: str
    start_snapshot: Mapping[str, Any]
    node_id: str | None = None
    """The node the attempt goes to; None = the job's pinned
    ``placement.node_id`` (the V0.4 decider interface)."""
    slot: str = "cpu"
    """D105: ``cpu`` or ``gpu``."""
    record: DecisionRecord | None = None
    """Written as ``dispatched`` with the attempt, in the same transaction."""


@dataclass(frozen=True, slots=True)
class DispatchFailure:
    failure_type: FailureType
    detail: str
    record: DecisionRecord | None = None
    """Written as ``failed`` with the job's FAIL arrow."""


@dataclass(frozen=True, slots=True)
class Wait:
    """The job stays queued; ``record`` is written as ``waiting`` only when its
    reason key differs from the job's latest decision (D104)."""

    record: DecisionRecord | None = None


type DispatchDecision = Dispatch | DispatchFailure | Wait | None
"""``None`` / :class:`Wait` = leave the job queued (try again on a later tick)."""

type Decide = Callable[[JobRecord, NodeView | None], DispatchDecision]
"""The V0.4 decider (pinned jobs, one node view)."""

type PlanDecide = Callable[[JobRecord, FabricRows], DispatchDecision]
"""The V0.6 decider: the whole tick's fabric rows (D102)."""


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
    logs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    """D71 final logs per stream; a digest only once verified under the grant."""
    transfers: tuple[TransferRow, ...] = ()
    """V0.5 G5 (D93): transfer telemetry of the terminal report."""
    env_hash: str | None = None
    """V0.6 G1 (D116): the agent's env hash (64 hex), terminal reports only."""
    output_bytes: int | None = None
    """V0.6 G4 (D108): bytes this attempt's grant stored (set by the
    dispatcher on a verified DONE; the estimator's ``out_bytes``)."""
    retryable: bool | None = None
    """V0.6 G3 (D110): the failure's own ``retryable`` flag (``Failure.
    retryable``); None = not sent -> the failure type's default (§13)."""


class StatusKind(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    """Equal to or behind the stored state (a replay, a reorder): no change."""
    FENCED = "fenced"
    """Stale token, or the attempt already ended differently: dropped."""
    UNKNOWN = "unknown"
    """No such attempt."""
    WRONG_NODE = "wrong_node"
    """Reported by a node the attempt is not placed on (V0.7 G7, THREAT_MODEL
    CG-E2): dropped before the token or the state is looked at; the outcome
    carries no attempt, so nothing follows from it (no cancel, no retry)."""


@dataclass(frozen=True, slots=True)
class StatusOutcome:
    kind: StatusKind
    attempt: AttemptRecord | None = None
    job: JobRecord | None = None
    ended: bool = False
    """This call moved the attempt into a terminal state."""
    retry: RetryVerdict | None = None
    """V0.6 G3 (D110): set when the attempt's end decided the job's fate -
    ``retry`` = the job is QUEUED again (same transaction), else why not."""


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


class ExecutionStore(DecisionReads):
    """:class:`JobStore` plus the dispatch-time writes (see module docstring)."""

    def __init__(self, engine: AsyncEngine) -> None:
        super().__init__(engine)
        # job id -> reason key of a ``waiting`` row the database refused; kept
        # across ticks so the same refused row is not retried every tick
        # (``fabric.last_reason`` is re-read from the table each tick).
        self._refused_waiting: dict[str, str] = {}

    @typed_store_call
    async def dispatch_planned(
        self,
        decide: PlanDecide,
        *,
        limit: int,
        max_per_node: int,
        now: dt.datetime,
        aging_s: float,
        history_runs: int = 20,
        network: NetworkReads | None = None,
    ) -> list[DispatchResult]:
        """One pass over the queue in priority order (D106; see module
        docstring and :mod:`.queue_reads`). Per job the decider's answer is
        applied in this transaction: an attempt (+ its ``dispatched``
        decision), a FAIL arrow (+ ``failed`` decision), or nothing (+ a
        ``waiting`` decision when the reasons changed). A job that cannot be
        placed never blocks the jobs behind it (no head-of-line blocking)."""
        results: list[DispatchResult] = []
        recorded: list[tuple[str, str | None, str, DecisionRecord]] = []
        async with self._sessions() as db, db.begin():
            await db.execute(_DISPATCH_LOCK)
            jobs = await queued_jobs(db, limit=limit, now=now, aging_s=aging_s)
            if not jobs:
                return results
            fabric = await read_fabric(
                db,
                jobs,
                history_runs=history_runs,
                keys_of=_project_keys,
                network=network,
            )
            for row in jobs:
                try:
                    job = job_record(row)
                except Exception as exc:  # noqa: BLE001 - G2 fix D-1: one bad row only
                    await _fail_unreadable(db, row, exc)
                    continue
                decision = decide(job, fabric)
                if isinstance(decision, Dispatch):
                    node_id = decision.node_id or str(job.placement.get("node_id", ""))
                    if node_id not in fabric.nodes or (
                        sum(fabric.used_slots(node_id)) >= max_per_node
                    ):
                        continue  # the decider must not over-book; defensive
                    attempt = await self._create_dispatched(db, row, node_id, decision)
                    fabric.took(
                        node_id, decision.slot, _gpu_indices(decision.start_snapshot)
                    )
                    if decision.record is not None:
                        fabric.last_reason[job.id] = decision.record.reason_key
                        if await _record_decision(
                            db,
                            job.id,
                            attempt.fencing_token,
                            "dispatched",
                            decision.record,
                            attempt_id=attempt.id,
                        ):
                            recorded.append(
                                (job.id, attempt.id, "dispatched", decision.record)
                            )
                    await _fresh(db, row)
                    results.append(DispatchResult(job=job_record(row), attempt=attempt))
                elif isinstance(decision, DispatchFailure):
                    why: str | None = decision.detail
                    if int(row.attempt_epoch) > 0:  # a retried job: keep the root cause
                        why = first_failure_detail(
                            decision.failure_type,
                            await _first_failure_type(db, row.id),
                            why,
                        )
                    await _move_job(
                        db,
                        row,
                        JobEvent.FAIL,
                        failure_type=decision.failure_type,
                        detail=why,
                    )
                    if decision.record is not None and await _record_decision(
                        db,
                        job.id,
                        int(row.attempt_epoch) + 1,
                        "failed",
                        decision.record,
                    ):
                        recorded.append((job.id, None, "failed", decision.record))
                    await _fresh(db, row)
                    results.append(
                        DispatchResult(
                            job=job_record(row), attempt=None, failure=decision
                        )
                    )
                elif isinstance(decision, Wait) and decision.record is not None:
                    key = decision.record.reason_key
                    if (
                        fabric.last_reason.get(job.id) != key
                        and self._refused_waiting.get(job.id) != key
                    ):
                        fabric.last_reason[job.id] = key
                        if await _record_decision(
                            db,
                            job.id,
                            int(row.attempt_epoch) + 1,
                            "waiting",
                            decision.record,
                        ):
                            self._refused_waiting.pop(job.id, None)
                            recorded.append((job.id, None, "waiting", decision.record))
                        else:  # refused: not again until the reason changes
                            self._refused_waiting[job.id] = key
            # bounded: only jobs read in this tick keep a refused key
            seen = {str(row.id) for row in jobs}
            for job_id in [j for j in self._refused_waiting if j not in seen]:
                del self._refused_waiting[job_id]
        for job_id, attempt_id, outcome, record in recorded:  # committed now
            log.info(
                "controller.jobs.plan_decision",
                job_id=job_id,
                attempt_id=attempt_id,
                node_id=record.chosen_node_id,
                outcome=outcome,
                policy=record.policy,
                candidates=len(record.candidates),
                reason_key=record.reason_key[:12],
            )
        for result in results:
            _log_dispatch(result)
        return results

    async def dispatch_queued(
        self, decide: Decide, *, limit: int, max_per_node: int
    ) -> list[DispatchResult]:
        """The V0.4 interface: ``decide(job, view of its pinned node)``; the
        queue order is V0.6's (priority, aging, ``queued_at``)."""

        def adapt(job: JobRecord, fabric: FabricRows) -> DispatchDecision:
            node_id = str(job.placement.get("node_id") or "")
            row = fabric.nodes.get(node_id)
            view = (
                None
                if row is None
                else NodeView(
                    node_id=row.node_id,
                    state=row.state,
                    hardware=row.hardware,
                    hardware_scores=row.hardware_scores,
                    active_attempts=sum(fabric.used_slots(node_id)),
                )
            )
            decision = decide(job, view)
            if isinstance(decision, Dispatch):
                if view is None or view.active_attempts >= max_per_node:
                    return None
                return dataclasses.replace(decision, node_id=node_id)
            return decision

        return await self.dispatch_planned(
            adapt,
            limit=limit,
            max_per_node=max_per_node,
            now=dt.datetime.now(dt.UTC),
            aging_s=_DEFAULT_AGING_S,
        )

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
            slot=decision.slot,
        )
        db.add(attempt)
        await _move_job(db, row, JobEvent.DISPATCHED)
        await _fresh(db, attempt)
        await note_attempt_change(db, attempt)
        return attempt_record(attempt)

    @typed_store_call
    async def requeue_stranded(self, *, limit: int = 1000) -> list[str]:
        """D115: jobs stored ``PLANNING`` or ``RETRYING`` go back to
        ``QUEUED`` (they are only queue entries). Every writer moves a job
        through those states inside one transaction, so after a crash none
        should exist; this runs at dispatcher start as the safety net for
        rows written by another version or by hand."""
        async with self._sessions() as db, db.begin():
            rows = list(
                await db.scalars(
                    select(Job)
                    .where(
                        Job.state.in_(
                            (JobState.PLANNING.value, JobState.RETRYING.value)
                        )
                    )
                    .order_by(Job.created_at)
                    .limit(limit)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                if row.state == JobState.RETRYING.value:
                    await _move_job(db, row, JobEvent.PLAN)
                await _move_job(db, row, JobEvent.PLANNED)
            ids = [row.id for row in rows]
        for job_id in ids:
            log.warning("controller.jobs.requeued_stranded", job_id=job_id)
        return ids

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
    async def apply_status(
        self, report: StatusReport, *, node_id: str
    ) -> StatusOutcome:
        """Apply one agent report (see module docstring); never raises for a
        stale/duplicate/unknown report - that is the outcome's ``kind``.

        ``node_id`` is the reporting node, proven by its session's client
        certificate (V0.7 G3). Only the node the attempt is placed on may move
        it: any other node's report is ``WRONG_NODE`` and changes nothing
        (V0.7 G7, CG-E2) - a node that learned another node's attempt id and
        fencing token (both small or visible to a job's owner) cannot end,
        fail or "complete" an attempt it does not run."""
        async with self._sessions() as db, db.begin():
            row = await db.get(Attempt, report.attempt_id, with_for_update=True)
            if row is None:
                return StatusOutcome(StatusKind.UNKNOWN)
            if row.node_id != node_id:
                # D168 N3: the refusal is audited in this transaction (the
                # attempt row itself is untouched); it feeds D180 alerts.
                await audit(
                    db,
                    AuditAction.ATTEMPT_WRONG_NODE,
                    actor=f"node:{node_id}",
                    node_id=node_id,
                    details={
                        "attempt_id": report.attempt_id,
                        "job_id": row.job_id,
                        "placed_node_id": row.node_id,
                        "reported": report.state.value,
                    },
                )
                return StatusOutcome(StatusKind.WRONG_NODE)
            if int(row.fencing_token) != report.fencing_token:
                return StatusOutcome(StatusKind.FENCED, attempt=attempt_record(row))
            try:
                path = attempt_path(AttemptState(row.state), report.state)
            except StaleReportError:
                return StatusOutcome(StatusKind.FENCED, attempt=attempt_record(row))
            if not path:
                return StatusOutcome(StatusKind.DUPLICATE, attempt=attempt_record(row))
            job = await db.get_one(Job, row.job_id, with_for_update=True)
            if int(row.fencing_token) != int(job.attempt_epoch):
                # D110/ADR-0015: only the job's current attempt may move it. A
                # retry ends the old attempt in the transaction that creates
                # the job's next epoch, so this is defence in depth.
                return StatusOutcome(StatusKind.FENCED, attempt=attempt_record(row))
            verdict: RetryVerdict | None = None
            failed = False
            for event, _ in path:
                await _move_attempt(db, row, event, report)
                reached = AttemptState(row.state)
                job_event = job_event_for(reached, report.failure_type)
                if job_event is JobEvent.FAIL:
                    failed = True  # decided below, after the phase rows
                elif job_event is not None:
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
                # Before a retry's new QUEUED entry: the queue phase is this
                # attempt's own wait (phase_rows reads the job history).
                await _write_phases(db, row, report.phases)
                await _write_transfers(db, row, report.transfers)
            if failed:
                verdict = await _fail_or_retry(
                    db,
                    job,
                    failure_type=report.failure_type or FailureType.INTERNAL_ERROR,
                    detail=report.detail,
                    retryable=report.retryable,
                    lost=False,
                )
            await _fresh(db, row, job)
            return StatusOutcome(
                StatusKind.APPLIED,
                attempt=attempt_record(row),
                job=job_record(job),
                ended=ended,
                retry=verdict,
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
        retryable: bool | None = None,
    ) -> StatusOutcome:
        """The controller ends a live attempt itself: ``LOSE`` (node gone) or
        ``FAIL`` (backstop, upload deadline). A no-op outcome when it already
        ended. In the same transaction the job is retried or failed (D110:
        a ``LOSE`` is a loss - retried unless the job set ``on_lost:
        false``)."""
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
            await _write_phases(db, row, None)  # before a retry's new QUEUED
            verdict: RetryVerdict | None = None
            if int(row.fencing_token) == int(job.attempt_epoch):
                verdict = await _fail_or_retry(
                    db,
                    job,
                    failure_type=failure_type,
                    detail=detail,
                    retryable=retryable,
                    lost=event is AttemptEvent.LOSE,
                )
            await _fresh(db, row, job)
            return StatusOutcome(
                StatusKind.APPLIED,
                attempt=attempt_record(row),
                job=job_record(job),
                ended=True,
                retry=verdict,
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


async def _fail_unreadable(db: AsyncSession, row: Job, exc: Exception) -> None:
    """A queued row the store cannot even turn into a :class:`JobRecord`
    (corrupt stored data) is failed on its own, logged once (G2 fix D-1)."""
    log.error(
        "controller.jobs.unreadable_job", job_id=row.id, error_type=type(exc).__name__
    )
    await _move_job(
        db,
        row,
        JobEvent.FAIL,
        failure_type=FailureType.INTERNAL_ERROR,
        detail=f"the stored job could not be read ({type(exc).__name__})",
    )
    await db.flush()


def _gpu_indices(snapshot: Mapping[str, Any]) -> tuple[int, ...]:
    raw = snapshot.get("gpu_indices")
    if not isinstance(raw, list | tuple):
        return ()
    return tuple(i for i in raw if isinstance(i, int) and not isinstance(i, bool))


def _project_keys(row: Job) -> list[str]:
    key = row.tree_id or row.bundle_digest
    return [key] if key else []


async def _record_decision(
    db: AsyncSession,
    job_id: str,
    attempt_no: int,
    outcome: str,
    record: DecisionRecord,
    *,
    attempt_id: str | None = None,
) -> bool:
    """D112: one ``plan_decision`` row in the caller's transaction.

    The row is the *explanation* of a placement, never a condition of it
    (D131, tester T-1): it is written inside a ``SAVEPOINT``, so a row the
    database refuses (a value ``jsonb`` cannot hold, a constraint) is rolled
    back alone - logged, the job's dispatch / failure and the rest of the
    tick still commit. False = not recorded.
    """
    try:
        async with db.begin_nested():  # flushes what is pending first (the attempt)
            db.add(
                PlanDecision(
                    job_id=job_id,
                    attempt_no=max(1, attempt_no),
                    attempt_id=attempt_id,
                    outcome=outcome,
                    policy=record.policy,
                    chosen_node_id=record.chosen_node_id,
                    snapshot_digest=record.snapshot_digest,
                    reason_key=record.reason_key,
                    candidates=[dict(c) for c in record.candidates][
                        :MAX_DECISION_CANDIDATES
                    ],
                )
            )
    except SQLAlchemyError as exc:
        log.error(  # noqa: TRY400 - typed event; the row's values stay out of logs
            "controller.jobs.plan_decision_not_recorded",
            job_id=job_id,
            attempt_id=attempt_id,
            outcome=outcome,
            error_type=type(exc).__name__,
        )
        return False
    return True


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


async def _fail_or_retry(
    db: AsyncSession,
    job: Job,
    *,
    failure_type: FailureType,
    detail: str | None,
    retryable: bool | None,
    lost: bool,
) -> RetryVerdict:
    """D110/ADR-0015: the job's current attempt just ended with a failure (or
    was lost) **in this transaction**; decide the job's fate here too.

    Retry = ``RETRYING -> PLANNING -> QUEUED`` with ``not_before`` = now +
    backoff (the database's clock, the one ``queued_at`` uses), every arrow
    in the history - so the attempt's end and the job's next queue entry
    commit together or not at all. ``queued_at`` keeps its first value: a
    retry does not lose its place to jobs submitted after it. A cancel
    requested before the attempt ended ends the job ``CANCELLED`` (D127).
    Otherwise the job fails with the attempt's failure type (``retries_
    exhausted`` in the detail when that was the reason; the first failed
    attempt's type when it differs) - or nothing, when the job already moved
    on (e.g. it was cancelled; the soft arrow logs it).
    """
    verdict = retry_verdict(
        failure_type=failure_type,
        retryable=retryable,
        lost=lost,
        attempts_used=int(job.attempt_epoch),
        max_attempts=int(job.max_attempts),
        base_backoff_s=float(job.retry_backoff_s),
        on_lost_retry=bool(job.on_lost_retry),
        cancel_requested=job.cancel_requested_at is not None,
        pinned=bool((job.placement or {}).get("node_id")),
    )
    if verdict.retry and await _move_job(db, job, JobEvent.RETRY, soft=True):
        await _move_job(db, job, JobEvent.PLAN)
        await _move_job(db, job, JobEvent.PLANNED)
        job.not_before = func.clock_timestamp() + func.make_interval(
            0, 0, 0, 0, 0, 0, verdict.backoff_s
        )
        return verdict
    if verdict.retry:  # the job had already moved on (soft arrow skipped)
        return RetryVerdict(RetryReason.NOT_RETRYABLE)
    if verdict.reason is RetryReason.CANCELLED:
        # D127: the user asked to stop it; how the attempt ended is the detail.
        ended = f"{failure_type.value}: {detail}" if detail else failure_type.value
        await _move_job(
            db,
            job,
            JobEvent.CANCEL,
            detail=f"cancelled by request (the attempt ended {ended})",
            soft=True,
        )
        return verdict
    if verdict.reason is RetryReason.RETRIES_EXHAUSTED:
        detail = exhausted_detail(int(job.attempt_epoch), detail)
    if int(job.attempt_epoch) > 1:
        detail = first_failure_detail(
            failure_type, await _first_failure_type(db, job.id), detail
        )
    await _move_job(
        db, job, JobEvent.FAIL, failure_type=failure_type, detail=detail, soft=True
    )
    return verdict


async def _first_failure_type(db: AsyncSession, job_id: str) -> str | None:
    """The failure type of the job's first failed/lost attempt (root cause)."""
    first = await db.scalar(
        select(Attempt.failure_type)
        .where(
            Attempt.job_id == job_id,
            Attempt.state.in_((AttemptState.FAILED.value, AttemptState.LOST.value)),
            Attempt.failure_type.is_not(None),
        )
        .order_by(Attempt.fencing_token)
        .limit(1)
    )
    return None if first is None else str(first)


async def _move_attempt(
    db: AsyncSession, row: Attempt, event: AttemptEvent, report: StatusReport
) -> None:
    current = AttemptState(row.state)
    target = next_attempt_state(current, event)
    row.state = target.value
    await note_attempt_change(db, row)
    if report.runtime and row.runtime is None:
        row.runtime = report.runtime
    if target is AttemptState.RUNNING:
        row.started_at = func.clock_timestamp()
    if target in ATTEMPT_TERMINAL_STATES:
        row.ended_at = func.clock_timestamp()
        if report.exit_code is not None:
            row.exit_code = report.exit_code
    if target in ATTEMPT_TERMINAL_STATES and report.logs:
        row.logs = {k: dict(v) for k, v in report.logs.items()}
    if target in ATTEMPT_TERMINAL_STATES and report.env_hash and row.env_hash is None:
        row.env_hash = report.env_hash
    if target is AttemptState.DONE:
        row.output_digest = report.output_digest
        row.missing_outputs = list(report.missing_outputs) or None
        if report.output_bytes is not None:
            row.output_bytes = report.output_bytes
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


async def _write_transfers(
    db: AsyncSession, row: Attempt, transfers: Sequence[TransferRow]
) -> None:
    """V0.5 G5 (D93): one row per (tree, source); a replayed terminal report
    (or a duplicate key) never doubles a row (ON CONFLICT DO NOTHING)."""
    if not transfers:
        return
    statement = pg_insert(TransferEvent).values(
        [
            {
                "attempt_id": row.id,
                "job_id": row.job_id,
                "tree_id": t.tree_id,
                "source": t.source,
                "bytes": t.bytes,
                "duration_ms": t.duration_ms,
                "units": t.units,
                "failures": t.failures,
                "resumed_chunks": t.resumed_chunks,
                "dropped": t.dropped,
            }
            for t in transfers
        ]
    )
    await db.execute(
        statement.on_conflict_do_nothing(
            index_elements=["attempt_id", "tree_id", "source"]
        )
    )


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
