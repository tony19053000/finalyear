"""A split job's combine attempt in the dispatcher's transactions (V0.10 G5,
D268/D283; ADR-0019).

Called by :class:`~meshforge.controller.jobs.execution_store.ExecutionStore`
(never on its own): every function runs in the caller's transaction, job and
attempt rows locked first (ADR-0019 addendum: controller rows, then work-queue
rows - this module touches no work-queue row for writing).

**When a combine is owed.** The transaction of a split job's last chunk commit
(:meth:`~.store.ChunkStore.result`) writes the parts index and moves the job
``RUNNING -> COLLECTING``. From then on a split job that is ``COLLECTING``, has
a parts index and has **no live combine attempt** owes one
(:func:`owed_jobs`) - a fact in PostgreSQL, so a controller restart, a lost
combine or a retry all lead back to it.

**Placement (D283 pin 2)** is the dispatcher's, like any attempt: owner
policies, liveness, slots and feasibility through the planner; preference:
the submitter's node, else the node holding the most committed chunk results
(:func:`result_sites`), else the planner's choice; nothing feasible -> the job
waits (it stays ``COLLECTING``). The attempt (``role = 'combine'``) gets the
next fencing token (``job.attempt_epoch + 1``: it is the job's current
attempt) and starts ``ASSIGNED``; the job moves no state.

**Its end** (:func:`combine_done`, :func:`combine_ended`), in the transaction
that ends it:

* ``DONE`` (its output digest verified under its upload grant by the
  dispatcher, D73) -> the job ``COLLECTING -> SUCCEEDED``; the combine's run
  time is kept on the job (``split_combine_ms``, for G8). The job's output is
  this attempt's output (``compute get`` takes the attempt with the highest
  token - this one).
* ``FAILED(combine_error)`` -> the job ``FAILED`` with that detail, **never
  retried** (a data error: every node would refuse the same parts).
* a cancel was requested -> ``CANCELLED``.
* anything else - the node LOST, a transfer failure, a crash - is judged like a
  Mode 1 attempt (:func:`~meshforge.controller.jobs.retry.retry_verdict`;
  attempts = the job's combine attempts, bounded by ``retry.max_attempts``):
  retry = the job stays ``COLLECTING`` with ``not_before`` = now + backoff and
  the dispatcher places a new combine (the failed node is avoided, softly);
  no retry = the job ``FAILED`` with that failure. The chunk results stay
  pinned (GC) while the job is not terminal.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import TYPE_CHECKING, Final

from sqlalchemy import DateTime, and_, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
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
from meshforge.controller.planner.retry_exclusion import PastFailure
from meshforge.controller.split.lanes import QUEUE_KIND, MoveJob
from meshforge.controller.store.models.job import Attempt, Job
from meshforge.workqueue.tables import work_item, work_lease, work_queue

if TYPE_CHECKING:
    from meshforge.controller.jobs.execution_store import CombineStart

__all__ = [
    "combine_done",
    "combine_ended",
    "combine_failures",
    "create_combine",
    "owed_jobs",
    "result_sites",
]

log = get_logger(__name__)

_LIVE: Final = tuple(s.value for s in AttemptState if s not in ATTEMPT_TERMINAL_STATES)
_MAX_SITES: Final = 1000


def _now(now: dt.datetime) -> object:
    return literal(now, DateTime(timezone=True))


# ------------------------------------------------------------------ reads


async def owed_jobs(db: AsyncSession, *, limit: int, now: dt.datetime) -> list[Job]:
    """Split jobs that owe a combine (module docstring), locked ``FOR UPDATE
    SKIP LOCKED`` in the order they entered COLLECTING (``updated_at``)."""
    live_combine = (
        select(Attempt.id)
        .where(
            Attempt.job_id == Job.id,
            Attempt.role == AttemptRole.COMBINE.value,
            Attempt.state.in_(_LIVE),
        )
        .exists()
    )
    return list(
        await db.scalars(
            select(Job)
            .where(
                Job.state == JobState.COLLECTING.value,
                Job.split.is_not(None),
                Job.split_parts_digest.is_not(None),
                or_(Job.not_before.is_(None), Job.not_before <= _now(now)),
                ~live_combine,
            )
            .order_by(Job.updated_at, Job.id)
            .limit(max(1, limit))
            .with_for_update(skip_locked=True)
        )
    )


async def result_sites(
    db: AsyncSession, job_ids: Sequence[str]
) -> dict[str, dict[str, int]]:
    """job_id -> {node_id: committed chunk results that node produced} (the
    winning lease's site of every done chunk) - the combine's cache
    affinity (D268: the node holding the most results)."""
    if not job_ids:
        return {}
    rows = await db.execute(
        select(work_queue.c.owner_id, work_lease.c.site, func.count())
        .select_from(
            work_queue.join(work_item, work_item.c.queue_id == work_queue.c.id).join(
                work_lease,
                and_(
                    work_lease.c.item_id == work_item.c.id,
                    work_lease.c.seq == work_item.c.done_lease_seq,
                ),
            )
        )
        .where(
            work_queue.c.kind == QUEUE_KIND,
            work_queue.c.owner_id.in_(list(job_ids)),
            work_item.c.state == "done",
        )
        .group_by(work_queue.c.owner_id, work_lease.c.site)
        .limit(_MAX_SITES * max(1, len(job_ids)))
    )
    out: dict[str, dict[str, int]] = {}
    for owner, site, count in rows.all():
        out.setdefault(str(owner), {})[str(site)] = int(count)
    return out


async def combine_failures(
    db: AsyncSession, job_ids: Sequence[str]
) -> dict[str, tuple[PastFailure, ...]]:
    """job_id -> its ended failed / lost **combine** attempts (where a new
    combine should not go; a lane's failure says nothing about combining)."""
    if not job_ids:
        return {}
    rows = await db.execute(
        select(Attempt.job_id, Attempt.node_id, Attempt.failure_type, Attempt.ended_at)
        .where(
            Attempt.job_id.in_(list(job_ids)),
            Attempt.role == AttemptRole.COMBINE.value,
            Attempt.state.in_((AttemptState.FAILED.value, AttemptState.LOST.value)),
            Attempt.failure_type.is_not(None),
        )
        .order_by(Attempt.job_id, Attempt.fencing_token)
        .limit(64 * len(job_ids))
    )
    out: dict[str, list[PastFailure]] = {}
    for job_id, node_id, failure_type, ended_at in rows.all():
        out.setdefault(str(job_id), []).append(
            PastFailure(str(node_id), str(failure_type), ended_at, ())
        )
    return {k: tuple(v) for k, v in out.items()}


async def _combines_used(db: AsyncSession, job_id: str) -> int:
    used = await db.scalar(
        select(func.count())
        .select_from(Attempt)
        .where(Attempt.job_id == job_id, Attempt.role == AttemptRole.COMBINE.value)
    )
    return int(used or 0)


# ------------------------------------------------------------------ create


async def create_combine(
    db: AsyncSession, row: Job, start: CombineStart
) -> AttemptRecord:
    """The combine attempt (module docstring): next fencing token, ``ASSIGNED``;
    the job stays ``COLLECTING`` and its retry wait is cleared."""
    row.attempt_epoch = int(row.attempt_epoch) + 1
    attempt = Attempt(
        id=start.attempt_id,
        job_id=row.id,
        node_id=start.node_id,
        fencing_token=row.attempt_epoch,
        state=AttemptState.ASSIGNED.value,
        start_snapshot=dict(start.start_snapshot),
        slot=start.slot,
        role=AttemptRole.COMBINE.value,
    )
    db.add(attempt)
    row.not_before = None
    await db.flush()
    await db.refresh(attempt)
    await note_attempt_change(db, attempt)
    log.info(
        "controller.split.combine_dispatched",
        job_id=row.id,
        attempt_id=attempt.id,
        node_id=attempt.node_id,
        fencing_token=int(attempt.fencing_token),
        reason=start.reason,
    )
    return attempt_record(attempt)


# ------------------------------------------------------------------ the end


async def combine_done(
    db: AsyncSession,
    job: Job,
    attempt: Attempt,
    *,
    run_s: float | None,
    move_job: MoveJob,
) -> None:
    """The combine ended ``DONE`` (output verified): the job SUCCEEDED."""
    if run_s is not None and run_s >= 0:
        job.split_combine_ms = round(run_s * 1000)
    ended = await move_job(db, job, JobEvent.COLLECTED, soft=True)
    log.info(
        "controller.split.combine_done",
        job_id=job.id,
        attempt_id=attempt.id,
        node_id=attempt.node_id,
        output_digest=attempt.output_digest,
        succeeded=ended,
    )


async def combine_ended(
    db: AsyncSession,
    job: Job,
    attempt: Attempt,
    *,
    failure_type: FailureType,
    detail: str | None,
    retryable: bool | None,
    lost: bool,
    move_job: MoveJob,
) -> RetryVerdict | None:
    """The combine ended without its output (module docstring). Returns the
    verdict when it decided the job's fate (``retry`` = a new combine)."""
    state = JobState(job.state)
    if state is not JobState.COLLECTING:
        return None  # the job already ended (a cancel, a race)
    if failure_type is FailureType.COMBINE_ERROR and not lost:
        await move_job(
            db, job, JobEvent.FAIL, failure_type=failure_type, detail=detail, soft=True
        )
        log.warning(
            "controller.split.combine_refused",
            job_id=job.id,
            attempt_id=attempt.id,
            node_id=attempt.node_id,
        )
        return RetryVerdict(RetryReason.NOT_RETRYABLE)
    used = await _combines_used(db, job.id)
    verdict = retry_verdict(
        failure_type=failure_type,
        retryable=retryable,
        lost=lost,
        attempts_used=used,
        max_attempts=int(job.max_attempts),
        base_backoff_s=float(job.retry_backoff_s),
        on_lost_retry=bool(job.on_lost_retry),
        cancel_requested=job.cancel_requested_at is not None,
    )
    if verdict.reason is RetryReason.CANCELLED:
        ended = f"{failure_type.value}: {detail}" if detail else failure_type.value
        await move_job(
            db,
            job,
            JobEvent.CANCEL,
            detail=f"cancelled by request (the combine ended {ended})",
            soft=True,
        )
        return verdict
    if verdict.retry:
        job.not_before = func.clock_timestamp() + func.make_interval(
            0, 0, 0, 0, 0, 0, verdict.backoff_s
        )
        log.warning(
            "controller.split.combine_retry",
            job_id=job.id,
            attempt_id=attempt.id,
            node_id=attempt.node_id,
            failure_type=failure_type.value,
            combines=used,
            backoff_s=verdict.backoff_s,
        )
        return verdict
    text = f"combine: {detail}" if detail else "combine failed"
    if verdict.reason is RetryReason.RETRIES_EXHAUSTED:
        text = exhausted_detail(used, text)
    await move_job(
        db, job, JobEvent.FAIL, failure_type=failure_type, detail=text, soft=True
    )
    return verdict
