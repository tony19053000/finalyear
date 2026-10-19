"""Lane rows inside the dispatcher's transactions (V0.10 G4, D259/D267).

Called by :class:`~meshforge.controller.jobs.execution_store.ExecutionStore`
(never on its own): every function runs in the caller's transaction, with the
job and attempt rows already locked (ADR-0019 addendum: **controller rows
first, work-queue rows second**).

**Dispatch (D259).** A split job's decision
(:class:`~meshforge.controller.jobs.execution_store.DispatchLanes`) becomes, in
the dispatch transaction: the job's work queue (``kind = "mode3"``, ``owner =
job id``; one item per chunk, payload ``{"first", "count"}``) - created on the
first dispatch, **reused** by a re-plan (its chunk size is fixed for the life
of the job) - and one ``lane`` attempt per node, each with its own fencing token
(``job.attempt_epoch + 1 ...``), and ``QUEUED -> PREPARING``.

**A lane's reports.** A lane walks the ordinary attempt path (``ASSIGNED ->
STAGING -> ENV_READY -> RUNNING -> ... -> DONE / FAILED``), but it moves its
job only by starting it (the first lane at ``RUNNING`` -> job ``RUNNING``). It
never succeeds or fails the job by itself: the job succeeds with its last chunk
commit (:mod:`.store`) and fails when a chunk fails for good or when no lane is
left and no retry is allowed. The "one current attempt" rule of Mode 1 (the
job's ``attempt_epoch``) does not apply to lanes: every lane of the job's
current round is current.

**A lane's end (D267a/d/e).** In the transaction that ends it (its own terminal
report, ``LOST``, a backstop): its live leases are released
(:func:`meshforge.workqueue.store.release_holder` - never counted as a chunk
failure, the chunk goes back to ``pending`` at once). If another lane of the
job is still live, nothing else happens - the others pull what it left. If it
was the last one and the job's queue is still open:

* a cancel was requested -> the job ends ``CANCELLED``, the queue is cancelled;
* else the lane's failure is judged like a Mode 1 attempt's
  (:func:`~meshforge.controller.jobs.retry.retry_verdict`; attempts = the
  job's lane *rounds*, so ``retry.max_attempts`` bounds the re-plans) - retry
  = ``RETRYING -> PLANNING -> QUEUED`` and the dispatcher places **new lanes for
  the remaining chunks** (the queue keeps its done chunks); no retry = the job
  fails with the lane's failure and the queue is cancelled (no partial output).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.common.split_chunks import chunk_payloads
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    AttemptRole,
    AttemptState,
    JobState,
)
from meshforge.controller.jobs.retry import (
    RetryReason,
    RetryVerdict,
    exhausted_detail,
    retry_verdict,
)
from meshforge.controller.jobs.store import (
    AttemptRecord,
    attempt_record,
    note_attempt_change,
)
from meshforge.controller.jobs.transitions import JobEvent
from meshforge.controller.store.models.job import Attempt, Job, JobTransition
from meshforge.workqueue import store as wq
from meshforge.workqueue.core import QueuePolicy, QueueState

if TYPE_CHECKING:
    from meshforge.controller.jobs.execution_store import DispatchLanes, LaneStart

__all__ = [
    "QUEUE_KIND",
    "LaneEnd",
    "LiveLane",
    "MoveJob",
    "count_live_lanes",
    "create_lanes",
    "ensure_queue",
    "lane_gone",
    "live_lanes",
]

log = get_logger(__name__)

QUEUE_KIND: Final = "mode3"
"""``work_queue.kind`` of a split job's chunk queue (owner = the job id)."""
_LIVE: Final = tuple(s.value for s in AttemptState if s not in ATTEMPT_TERMINAL_STATES)


class MoveJob(Protocol):
    """The execution store's job-arrow writer (``_move_job``), passed in so this
    module does not import the store it is called by."""

    async def __call__(
        self,
        db: AsyncSession,
        row: Job,
        event: JobEvent,
        *,
        failure_type: FailureType | None = None,
        detail: str | None = None,
        soft: bool = False,
    ) -> bool: ...


@dataclass(frozen=True, slots=True)
class LiveLane:
    attempt_id: str
    node_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class LaneEnd:
    """What a lane's end did (for the caller's after-commit work)."""

    released: int
    """Leases put back."""
    verdict: RetryVerdict | None = None
    """Set when this was the job's last lane and its fate was decided."""
    job_ended: bool = False
    """The job reached FAILED / CANCELLED in this transaction."""


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ------------------------------------------------------------------ dispatch


async def ensure_queue(
    db: AsyncSession, row: Job, *, chunk_items: int, policy: QueuePolicy
) -> wq.QueueInfo:
    """The job's chunk queue: created now, or the one a previous round made
    (D267e - a re-plan keeps every committed chunk and the chunk size)."""
    found = await wq.find_queue(db, kind=QUEUE_KIND, owner_id=row.id)
    if found is not None:
        return found
    count = int(row.split_item_count or 0)
    return await wq.create_queue(
        db,
        kind=QUEUE_KIND,
        owner_id=row.id,
        payloads=chunk_payloads(count, chunk_items),
        policy=policy,
    )


async def create_lanes(
    db: AsyncSession, row: Job, lanes: Sequence[LaneStart]
) -> list[Attempt]:
    """One ``lane`` attempt per entry, tokens ``attempt_epoch + 1 ...``."""
    created: list[Attempt] = []
    for lane in lanes:
        row.attempt_epoch = int(row.attempt_epoch) + 1
        attempt = Attempt(
            id=lane.attempt_id,
            job_id=row.id,
            node_id=lane.node_id,
            fencing_token=row.attempt_epoch,
            state=AttemptState.ASSIGNED.value,
            start_snapshot=dict(lane.start_snapshot),
            slot=lane.slot,
            role=AttemptRole.LANE.value,
        )
        db.add(attempt)
        created.append(attempt)
    await db.flush()
    for attempt in created:
        await db.refresh(attempt)
        await note_attempt_change(db, attempt)
    return created


async def dispatch(
    db: AsyncSession,
    row: Job,
    decision: DispatchLanes,
    lanes: Sequence[LaneStart],
    move_job: MoveJob,
) -> list[AttemptRecord]:
    """Queue (create or reuse) + lanes + ``QUEUED -> PREPARING``."""
    queue = await ensure_queue(
        db, row, chunk_items=decision.chunk_items, policy=decision.queue_policy
    )
    if queue.state is not QueueState.OPEN:
        raise ValueError(f"job {row.id}: its chunk queue is {queue.state.value}")
    attempts = await create_lanes(db, row, lanes)
    await move_job(db, row, JobEvent.DISPATCHED)
    log.info(
        "controller.split.lanes_dispatched",
        job_id=row.id,
        lanes=len(attempts),
        nodes=[a.node_id for a in attempts],
        chunk_items=decision.chunk_items,
        queue_id=queue.id,
        chunks=queue.item_count,
        done=queue.done_count,
    )
    return [attempt_record(a) for a in attempts]


# ------------------------------------------------------------------ reads


async def live_lanes(
    db: AsyncSession, job_id: str, *, exclude: str | None = None
) -> list[LiveLane]:
    query = select(Attempt.id, Attempt.node_id, Attempt.fencing_token).where(
        Attempt.job_id == job_id,
        Attempt.role == AttemptRole.LANE.value,
        Attempt.state.in_(_LIVE),
    )
    if exclude is not None:
        query = query.where(Attempt.id != exclude)
    rows = (await db.execute(query.order_by(Attempt.fencing_token))).all()
    return [LiveLane(r.id, r.node_id, int(r.fencing_token)) for r in rows]


async def count_live_lanes(db: AsyncSession, job_id: str) -> int:
    return len(await live_lanes(db, job_id))


async def _rounds(db: AsyncSession, job_id: str) -> int:
    """Lane rounds the job had: 1 + its re-plans (RETRYING arrows)."""
    retried = await db.scalar(
        select(func.count())
        .select_from(JobTransition)
        .where(
            JobTransition.job_id == job_id,
            JobTransition.to_state == JobState.RETRYING.value,
        )
    )
    return 1 + int(retried or 0)


# ------------------------------------------------------------------ the end


async def lane_gone(
    db: AsyncSession,
    job: Job,
    lane: Attempt,
    *,
    failure_type: FailureType | None,
    detail: str | None,
    retryable: bool | None,
    lost: bool,
    move_job: MoveJob,
    now: dt.datetime | None = None,
) -> LaneEnd:
    """``lane`` just reached a terminal state in this transaction (module
    docstring). ``failure_type`` None = it ended DONE."""
    now = now or now_utc()
    released = await wq.release_holder(db, lane.id, now=now)
    if released:
        log.info(
            "controller.split.lane_leases_released",
            job_id=job.id,
            attempt_id=lane.id,
            node_id=lane.node_id,
            released=len(released),
        )
    if JobState(job.state) in (JobState.SUCCEEDED, JobState.FAILED, JobState.CANCELLED):
        return LaneEnd(len(released))
    if await live_lanes(db, job.id, exclude=lane.id):
        return LaneEnd(len(released))
    queue = await wq.find_queue(db, kind=QUEUE_KIND, owner_id=job.id)
    if queue is None or queue.state is not QueueState.OPEN:
        return LaneEnd(len(released))
    # The last lane is gone and chunks remain.
    kind = failure_type or FailureType.NODE_LOST
    why = detail or "the lane ended with chunks left"
    if job.cancel_requested_at is not None:
        await wq.cancel(db, queue.id, now=now)
        await move_job(
            db,
            job,
            JobEvent.CANCEL,
            detail=f"cancelled by request (the last lane ended {kind.value})",
            soft=True,
        )
        return LaneEnd(len(released), RetryVerdict(RetryReason.CANCELLED), True)
    rounds = await _rounds(db, job.id)
    verdict = retry_verdict(
        failure_type=kind,
        retryable=retryable,
        lost=lost or failure_type is None,
        attempts_used=rounds,
        max_attempts=int(job.max_attempts),
        base_backoff_s=float(job.retry_backoff_s),
        on_lost_retry=bool(job.on_lost_retry),
        cancel_requested=False,
    )
    left = queue.item_count - queue.done_count
    if verdict.retry and await move_job(db, job, JobEvent.RETRY, soft=True):
        await move_job(db, job, JobEvent.PLAN)
        await move_job(db, job, JobEvent.PLANNED)
        job.not_before = func.clock_timestamp() + func.make_interval(
            0, 0, 0, 0, 0, 0, verdict.backoff_s
        )
        log.warning(
            "controller.split.replan",
            job_id=job.id,
            attempt_id=lane.id,
            node_id=lane.node_id,
            chunks_left=left,
            round=rounds + 1,
            failure_type=kind.value,
        )
        return LaneEnd(len(released), verdict)
    await wq.cancel(db, queue.id, now=now)
    text = f"no lane left with {left} of {queue.item_count} chunks to run: {why}"
    if verdict.reason is RetryReason.RETRIES_EXHAUSTED:
        text = exhausted_detail(rounds, text)
    ended = await move_job(
        db, job, JobEvent.FAIL, failure_type=kind, detail=text, soft=True
    )
    return LaneEnd(
        len(released),
        verdict if verdict.reason is not RetryReason.RETRY else None,
        ended,
    )
