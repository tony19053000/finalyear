"""The one writer of ``job`` / ``job_transition`` / ``attempt`` / ``attempt_phase`` (D49).

**Compare-and-set.** Every state change is one ``UPDATE ... WHERE id = :id AND
state = :expected`` (attempts also ``AND fencing_token = :token``) whose target
comes from :mod:`meshforge.controller.jobs.transitions`. Two racing writers can
therefore never both win: PostgreSQL row-locks the row for the first UPDATE, and
the second re-evaluates its WHERE after the first commits, finds the state moved
and matches 0 rows - it gets :class:`StaleStateError` (``conflict``), never a
silent overwrite. The history row (``job_transition``) is inserted in the same
transaction, numbered after the winning UPDATE, so ``seq`` is gap-free.

**Idempotent create.** ``INSERT ... ON CONFLICT (idempotency_key) DO NOTHING``:
concurrent POSTs with one key produce exactly one row (the unique index decides),
and the losers read the winner's row and compare request hashes - equal is a
replay (same job), different is :class:`IdempotencyConflictError`.

**Atomic submission.** :meth:`JobStore.create_queued_job` inserts the job *and*
its whole ``SUBMITTED -> PLANNING -> QUEUED`` history in one transaction (arrows
from :func:`next_job_state`, each history row stamped with ``clock_timestamp()``
so the pass-through still has real, ordered times). A crash, dropped connection
or cancelled request anywhere inside leaves no row at all, never a job stuck in
``SUBMITTED``/``PLANNING`` that nothing will ever dispatch; and because a key
loser's ``ON CONFLICT`` waits for the winner's commit, every reader (replay or
race loser) sees the job ``QUEUED`` or later.

**Fencing tokens.** ``create_attempt`` allocates ``job.attempt_epoch + 1`` with one
``UPDATE ... RETURNING`` (atomic), so attempts of a job are numbered 1, 2, ...
and a report carrying an older token is recognisably stale
(:class:`StaleFencingTokenError`, ``fenced``).

Callers get frozen records, never ORM objects. Driver/network errors become
:class:`JobStoreError` (``network_error``, retryable). Timestamps are the
database's wall clock (``now()``, or ``clock_timestamp()`` where several history
rows share one transaction); nothing here measures durations.
"""

from __future__ import annotations

import datetime as dt
import functools
import hmac
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    AttemptPhase,
    AttemptState,
    JobState,
)
from meshforge.controller.jobs.transitions import (
    AttemptEvent,
    JobEvent,
    next_attempt_state,
    next_job_state,
)
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.data import JobInput, TransferEvent
from meshforge.controller.store.models.job import (
    MAX_DETAIL_CHARS,
    Attempt,
    AttemptPhaseRow,
    Job,
    JobTransition,
)

__all__ = [
    "AttemptRecord",
    "IdempotencyConflictError",
    "JobRecord",
    "JobScheduling",
    "JobStore",
    "JobStoreError",
    "PhaseRecord",
    "StaleFencingTokenError",
    "StaleStateError",
    "TransferRow",
    "TransitionRecord",
    "UnknownAttemptError",
    "UnknownJobError",
    "append_transition",
    "attempt_record",
    "clean_detail",
    "job_record",
    "typed_store_call",
]

log = get_logger(__name__)

_MAX_LIST: Final = 10_000


class JobStoreError(MeshForgeError):
    """The database refused or lost a read/write (retryable)."""

    default_failure_type = FailureType.NETWORK_ERROR


class UnknownJobError(MeshForgeError):
    default_failure_type = FailureType.NOT_FOUND


class UnknownAttemptError(MeshForgeError):
    default_failure_type = FailureType.NOT_FOUND


class StaleStateError(MeshForgeError):
    """Compare-and-set lost: the row is no longer in the expected state."""

    default_failure_type = FailureType.CONFLICT


class StaleFencingTokenError(MeshForgeError):
    """A write for an attempt carried a fencing token that is not the attempt's."""

    default_failure_type = FailureType.FENCED


class IdempotencyConflictError(MeshForgeError):
    """The Idempotency-Key is already used by a *different* request."""

    default_failure_type = FailureType.CONFLICT


def clean_detail(text: str | None) -> str | None:
    """Printable, at most ``MAX_DETAIL_CHARS`` characters (it reaches terminals)."""
    if text is None:
        return None
    flat = "".join(ch if ch.isprintable() else " " for ch in text)
    return flat[:MAX_DETAIL_CHARS]


# ------------------------------------------------------------------ records


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    name: str
    state: JobState
    manifest: Mapping[str, Any]
    bundle_digest: str | None
    """The V0.4 bundle; None for a tree job (V0.5 G4: the tree is named only by
    ``tree_id`` - no reuse of this field for the manifest)."""
    placement: Mapping[str, Any]
    idempotency_key: str | None
    request_hash: str
    attempt_epoch: int
    failure_type: FailureType | None
    failure_detail: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    cancel_requested_at: dt.datetime | None = None
    tree_id: str | None = None
    """V0.5 D83: the project's tree manifest; None = a V0.4 bundle job."""
    scheduling: JobScheduling = field(default_factory=lambda: JobScheduling())
    """V0.6 G2 (migration 0013): the planner's per-job inputs."""
    owner_user_id: str | None = None
    """V0.7 G4 (D141, migration 0018): who submitted it; None = admin-only."""


@dataclass(frozen=True, slots=True)
class JobScheduling:
    """The planner's per-job inputs (V0.6 G2, D103-D110; migration 0013)."""

    priority: int = 0
    policy: str | None = None
    """None = the controller's ``[controller.planner] default_policy``."""
    max_attempts: int = 3
    retry_backoff_s: int = 5
    on_lost_retry: bool = True
    submitter_node_id: str | None = None
    submitter_seeded: bool = False
    not_before: dt.datetime | None = None
    queued_at: dt.datetime | None = None
    input_bytes: int | None = None
    fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class TransferRow:
    """One ``transfer_event`` row (V0.5 G5, D93; see the model)."""

    tree_id: str
    source: str
    bytes: int
    duration_ms: int
    units: int
    failures: int
    resumed_chunks: int = 0
    dropped: str | None = None


@dataclass(frozen=True, slots=True)
class TransitionRecord:
    seq: int
    from_state: JobState | None
    to_state: JobState
    at: dt.datetime


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    id: str
    job_id: str
    node_id: str
    fencing_token: int
    state: AttemptState
    failure_type: FailureType | None
    detail: str | None
    exit_code: int | None
    output_digest: str | None
    start_snapshot: Mapping[str, Any] | None
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    created_at: dt.datetime
    start_acked_at: dt.datetime | None = None
    runtime: str | None = None
    missing_outputs: tuple[str, ...] = ()
    logs: Mapping[str, Any] | None = None
    """Final logs per stream (D71): ``{"stdout": {"digest", "bytes", "truncated"}}``."""


@dataclass(frozen=True, slots=True)
class PhaseRecord:
    phase: str
    started_at: dt.datetime
    duration_ms: int
    skipped: bool = False


def job_record(row: Job) -> JobRecord:
    return JobRecord(
        id=row.id,
        name=row.name,
        state=JobState(row.state),
        manifest=dict(row.manifest),
        bundle_digest=row.bundle_digest,
        placement=dict(row.placement),
        idempotency_key=row.idempotency_key,
        request_hash=row.request_hash,
        attempt_epoch=row.attempt_epoch,
        failure_type=FailureType(row.failure_type) if row.failure_type else None,
        failure_detail=row.failure_detail,
        created_at=row.created_at,
        updated_at=row.updated_at,
        cancel_requested_at=row.cancel_requested_at,
        tree_id=row.tree_id,
        scheduling=JobScheduling(
            priority=int(row.priority),
            policy=row.policy,
            max_attempts=int(row.max_attempts),
            retry_backoff_s=int(row.retry_backoff_s),
            on_lost_retry=bool(row.on_lost_retry),
            submitter_node_id=row.submitter_node_id,
            submitter_seeded=bool(row.submitter_seeded),
            not_before=row.not_before,
            queued_at=row.queued_at,
            input_bytes=None if row.input_bytes is None else int(row.input_bytes),
            fingerprint=row.fingerprint,
        ),
        owner_user_id=row.owner_user_id,
    )


def attempt_record(row: Attempt) -> AttemptRecord:
    return AttemptRecord(
        id=row.id,
        job_id=row.job_id,
        node_id=row.node_id,
        fencing_token=row.fencing_token,
        state=AttemptState(row.state),
        failure_type=FailureType(row.failure_type) if row.failure_type else None,
        detail=row.detail,
        exit_code=row.exit_code,
        output_digest=row.output_digest,
        start_snapshot=dict(row.start_snapshot) if row.start_snapshot else None,
        started_at=row.started_at,
        ended_at=row.ended_at,
        created_at=row.created_at,
        start_acked_at=row.start_acked_at,
        runtime=row.runtime,
        missing_outputs=tuple(row.missing_outputs or ()),
        logs=dict(row.logs) if row.logs else None,
    )


def typed_store_call[**P, R](
    method: Callable[P, Awaitable[R]],
) -> Callable[P, Coroutine[Any, Any, R]]:
    @functools.wraps(method)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return await method(*args, **kwargs)
        except MeshForgeError:
            raise
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            raise JobStoreError(
                f"jobs.{method.__name__} failed: {type(exc).__name__}",
                details={"operation": method.__name__, "cause": type(exc).__name__},
            ) from exc

    return wrapper


async def _append_transition(
    db: AsyncSession,
    job_id: str,
    from_state: JobState | None,
    to_state: JobState,
    *,
    at: Any = None,
) -> None:
    """Next ``seq`` for the job; only called while this tx holds the job row.

    ``at`` defaults to ``now()`` (the transaction's start); pass
    ``func.clock_timestamp()`` when one transaction appends several rows.
    """
    last = await db.scalar(
        select(func.coalesce(func.max(JobTransition.seq), 0)).where(
            JobTransition.job_id == job_id
        )
    )
    db.add(
        JobTransition(
            job_id=job_id,
            seq=int(last or 0) + 1,
            from_state=from_state.value if from_state is not None else None,
            to_state=to_state.value,
            at=func.now() if at is None else at,
        )
    )
    await db.flush()


class JobStore:
    """Async repository and single writer for jobs and attempts."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )

    # ------------------------------------------------------------------ jobs

    @typed_store_call
    async def create_job(
        self,
        *,
        job_id: str,
        name: str,
        manifest: Mapping[str, Any],
        bundle_digest: str | None,
        placement: Mapping[str, Any],
        request_hash: str,
        idempotency_key: str | None,
        through: Sequence[JobEvent] = (),
        tree_id: str | None = None,
        inputs: Sequence[tuple[str, str, str, int, str]] = (),
        scheduling: JobScheduling | None = None,
        owner_user_id: str | None = None,
    ) -> tuple[JobRecord, bool]:
        """Insert a job; ``(record, created)``. See module docstring.

        **Production code calls :meth:`create_queued_job`** (D62-1). Called with the
        default ``through=()`` this leaves a job ``SUBMITTED`` that nothing will
        ever plan or dispatch in V0.4 - that form exists for tests of the store
        and of the transition path only.

        The job starts ``SUBMITTED`` and is walked through ``through`` (each arrow
        from :func:`next_job_state`, so an illegal path raises before any write);
        the row, its final state and every history row commit together or not at
        all. Most callers want :meth:`create_queued_job`.
        """
        if (bundle_digest is None) == (tree_id is None):
            raise ValueError("give exactly one of bundle_digest and tree_id")
        path: list[tuple[JobState | None, JobState]] = [(None, JobState.SUBMITTED)]
        for event in through:
            path.append((path[-1][1], next_job_state(path[-1][1], event)))
        plan = scheduling or JobScheduling()
        values: dict[str, Any] = {
            "id": job_id,
            "name": name,
            "state": path[-1][1].value,
            "manifest": dict(manifest),
            "bundle_digest": bundle_digest,
            "tree_id": tree_id,
            "placement": dict(placement),
            "idempotency_key": idempotency_key,
            "request_hash": request_hash,
            "priority": plan.priority,
            "policy": plan.policy,
            "max_attempts": plan.max_attempts,
            "retry_backoff_s": plan.retry_backoff_s,
            "on_lost_retry": plan.on_lost_retry,
            "submitter_node_id": plan.submitter_node_id,
            "submitter_seeded": plan.submitter_seeded,
            "input_bytes": plan.input_bytes,
            "fingerprint": plan.fingerprint,
            "owner_user_id": owner_user_id,
        }
        async with self._sessions() as db, db.begin():
            statement = pg_insert(Job).values(**values)
            if idempotency_key is not None:
                statement = statement.on_conflict_do_nothing(
                    index_elements=[Job.idempotency_key]
                )
            inserted = await db.scalar(statement.returning(Job.id))
            if inserted is not None:
                # V0.5 D92: (mount, ref, name, version, tree_id) per input, in
                # the same transaction as the job row.
                for mount, ref, input_name, version, input_tree in inputs:
                    db.add(
                        JobInput(
                            job_id=job_id,
                            mount=mount,
                            ref=ref,
                            name=input_name,
                            version=version,
                            tree_id=input_tree,
                        )
                    )
                for from_state, to_state in path:
                    await _append_transition(
                        db, job_id, from_state, to_state, at=func.clock_timestamp()
                    )
                if path[-1][1] is JobState.QUEUED:
                    # D106: the queue position, stamped after the QUEUED arrow
                    # (``clock_timestamp()``), so never before it.
                    await db.execute(
                        update(Job)
                        .where(Job.id == job_id)
                        .values(queued_at=func.clock_timestamp())
                        .execution_options(synchronize_session=False)
                    )
                row = await db.get_one(Job, job_id)
                return job_record(row), True
        # Lost the key race (or a retry of an earlier request): read the winner.
        # ON CONFLICT waited for the winner's commit, so its whole path is visible.
        existing = await self.get_job_by_key(idempotency_key or "")
        if existing is None:  # pragma: no cover - the unique row cannot vanish
            raise JobStoreError("idempotency conflict without a row")
        if not hmac.compare_digest(existing.request_hash, request_hash):
            raise IdempotencyConflictError(
                "this Idempotency-Key was already used for a different request",
                details={"job_id": existing.id},
            )
        return existing, False

    async def create_queued_job(
        self,
        *,
        job_id: str,
        name: str,
        manifest: Mapping[str, Any],
        bundle_digest: str | None,
        placement: Mapping[str, Any],
        request_hash: str,
        idempotency_key: str | None,
        tree_id: str | None = None,
        inputs: Sequence[tuple[str, str, str, int, str]] = (),
        scheduling: JobScheduling | None = None,
        owner_user_id: str | None = None,
    ) -> tuple[JobRecord, bool]:
        """Atomically submit a job (D50, V0.6 D104): ``SUBMITTED -> PLANNING ->
        QUEUED`` in one transaction (``PLANNING`` stays a recorded pass-through:
        the submit-time static feasibility pass already ran, the live planner
        runs at dispatch). ``created`` is False for an idempotent replay /
        key-race loser (the existing job, already ``QUEUED`` or later).
        """
        return await self.create_job(
            job_id=job_id,
            name=name,
            manifest=manifest,
            bundle_digest=bundle_digest,
            placement=placement,
            request_hash=request_hash,
            idempotency_key=idempotency_key,
            through=(JobEvent.PLAN, JobEvent.PLANNED),
            tree_id=tree_id,
            inputs=inputs,
            scheduling=scheduling,
            owner_user_id=owner_user_id,
        )

    @typed_store_call
    async def transfers(self, job_id: str) -> list[tuple[str, TransferRow]]:
        """``(attempt_id, row)`` of every transfer_event of a job (V0.5 G5),
        oldest attempt first, the cache row first within a tree."""
        async with self._sessions() as db:
            rows = await db.scalars(
                select(TransferEvent)
                .where(TransferEvent.job_id == job_id)
                .order_by(TransferEvent.id)
                .limit(2000)
            )
            return [
                (
                    r.attempt_id,
                    TransferRow(
                        tree_id=r.tree_id,
                        source=r.source,
                        bytes=int(r.bytes),
                        duration_ms=int(r.duration_ms),
                        units=int(r.units),
                        failures=int(r.failures),
                        resumed_chunks=int(r.resumed_chunks),
                        dropped=r.dropped,
                    ),
                )
                for r in rows
            ]

    @typed_store_call
    async def get_job(self, job_id: str) -> JobRecord | None:
        async with self._sessions() as db:
            row = await db.get(Job, job_id)
            return job_record(row) if row is not None else None

    @typed_store_call
    async def get_job_by_key(self, idempotency_key: str) -> JobRecord | None:
        async with self._sessions() as db:
            row = await db.scalar(
                select(Job).where(Job.idempotency_key == idempotency_key)
            )
            return job_record(row) if row is not None else None

    @typed_store_call
    async def list_jobs(
        self,
        *,
        limit: int,
        state: JobState | None = None,
        owner_user_id: str | None = None,
    ) -> list[JobRecord]:
        """Newest first (by ``created_at``, then id), at most ``limit``;
        ``owner_user_id`` set = only that user's jobs (D141)."""
        limit = max(1, min(limit, _MAX_LIST))
        query = select(Job).order_by(Job.created_at.desc(), Job.id.desc()).limit(limit)
        if state is not None:
            query = query.where(Job.state == state.value)
        if owner_user_id is not None:
            query = query.where(Job.owner_user_id == owner_user_id)
        async with self._sessions() as db:
            return [job_record(row) for row in await db.scalars(query)]

    @typed_store_call
    async def transitions(self, job_id: str) -> list[TransitionRecord]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(JobTransition)
                .where(JobTransition.job_id == job_id)
                .order_by(JobTransition.seq)
            )
            return [
                TransitionRecord(
                    seq=r.seq,
                    from_state=JobState(r.from_state) if r.from_state else None,
                    to_state=JobState(r.to_state),
                    at=r.at,
                )
                for r in rows
            ]

    @typed_store_call
    async def transition_job(
        self,
        job_id: str,
        event: JobEvent,
        *,
        expected: JobState,
        failure_type: FailureType | None = None,
        failure_detail: str | None = None,
    ) -> JobRecord:
        """Apply ``event`` if the job is still ``expected`` (compare-and-set)."""
        target = next_job_state(expected, event)
        if (target is JobState.FAILED) != (failure_type is not None):
            raise ValueError("failure_type is required for FAILED and only for FAILED")
        values: dict[str, Any] = {"state": target.value}
        if failure_type is not None:
            values["failure_type"] = failure_type.value
            values["failure_detail"] = clean_detail(failure_detail)
        async with self._sessions() as db, db.begin():
            row = await db.scalar(
                update(Job)
                .where(Job.id == job_id, Job.state == expected.value)
                .values(**values)
                .returning(Job)
                .execution_options(synchronize_session=False)
            )
            if row is None:
                actual = await db.scalar(select(Job.state).where(Job.id == job_id))
                if actual is None:
                    raise UnknownJobError("no such job", details={"job_id": job_id})
                log.info(
                    "controller.jobs.transition_lost",
                    job_id=job_id,
                    job_event=event.value,
                    expected=expected.value,
                    actual=actual,
                )
                raise StaleStateError(
                    f"job is {actual}, not {expected.value}",
                    details={
                        "job_id": job_id,
                        "expected": expected.value,
                        "actual": actual,
                    },
                )
            record = job_record(row)
            await _append_transition(db, job_id, expected, target)
        log.info(
            "controller.jobs.transition",
            job_id=job_id,
            job_event=event.value,
            from_state=expected.value,
            to_state=target.value,
        )
        return record

    # -------------------------------------------------------------- attempts

    @typed_store_call
    async def create_attempt(
        self, job_id: str, node_id: str, *, attempt_id: str
    ) -> AttemptRecord:
        """A new ASSIGNED attempt with the job's next fencing token (1, 2, ...)."""
        async with self._sessions() as db, db.begin():
            token = await db.scalar(
                update(Job)
                .where(Job.id == job_id)
                .values(attempt_epoch=Job.attempt_epoch + 1)
                .returning(Job.attempt_epoch)
            )
            if token is None:
                raise UnknownJobError("no such job", details={"job_id": job_id})
            row = Attempt(
                id=attempt_id,
                job_id=job_id,
                node_id=node_id,
                fencing_token=int(token),
                state=AttemptState.ASSIGNED.value,
            )
            db.add(row)
            await db.flush()
            await db.refresh(row)
            return attempt_record(row)

    @typed_store_call
    async def get_attempt(self, attempt_id: str) -> AttemptRecord | None:
        async with self._sessions() as db:
            row = await db.get(Attempt, attempt_id)
            return attempt_record(row) if row is not None else None

    @typed_store_call
    async def list_attempts(self, job_id: str) -> list[AttemptRecord]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(Attempt)
                .where(Attempt.job_id == job_id)
                .order_by(Attempt.fencing_token)
            )
            return [attempt_record(r) for r in rows]

    @typed_store_call
    async def transition_attempt(
        self,
        attempt_id: str,
        event: AttemptEvent,
        *,
        expected: AttemptState,
        fencing_token: int,
        failure_type: FailureType | None = None,
        detail: str | None = None,
        exit_code: int | None = None,
        output_digest: str | None = None,
    ) -> AttemptRecord:
        """Compare-and-set on ``(id, state, fencing_token)``."""
        target = next_attempt_state(expected, event)
        values: dict[str, Any] = {"state": target.value}
        if failure_type is not None:
            values["failure_type"] = failure_type.value
        if detail is not None:
            values["detail"] = clean_detail(detail)
        if exit_code is not None:
            values["exit_code"] = exit_code
        if output_digest is not None:
            values["output_digest"] = output_digest
        if target is AttemptState.RUNNING:
            values["started_at"] = func.now()
        if target in ATTEMPT_TERMINAL_STATES:
            values["ended_at"] = func.now()
        async with self._sessions() as db, db.begin():
            row = await db.scalar(
                update(Attempt)
                .where(
                    Attempt.id == attempt_id,
                    Attempt.state == expected.value,
                    Attempt.fencing_token == fencing_token,
                )
                .values(**values)
                .returning(Attempt)
                .execution_options(synchronize_session=False)
            )
            if row is not None:
                return attempt_record(row)
            current = (
                await db.execute(
                    select(Attempt.state, Attempt.fencing_token).where(
                        Attempt.id == attempt_id
                    )
                )
            ).one_or_none()
        if current is None:
            raise UnknownAttemptError(
                "no such attempt", details={"attempt_id": attempt_id}
            )
        state, token = current
        if token != fencing_token:
            raise StaleFencingTokenError(
                "stale fencing token for this attempt",
                details={"attempt_id": attempt_id, "fencing_token": fencing_token},
            )
        raise StaleStateError(
            f"attempt is {state}, not {expected.value}",
            details={
                "attempt_id": attempt_id,
                "expected": expected.value,
                "actual": state,
            },
        )

    @typed_store_call
    async def record_phase(
        self,
        attempt_id: str,
        phase: AttemptPhase,
        *,
        started_at: dt.datetime,
        duration_ms: int,
    ) -> bool:
        """Store one phase timing; a duplicate (replayed report) is ignored (False)."""
        if duration_ms < 0:
            raise ValueError("duration_ms must be >= 0")
        async with self._sessions() as db, db.begin():
            inserted = await db.scalar(
                pg_insert(AttemptPhaseRow)
                .values(
                    attempt_id=attempt_id,
                    phase=AttemptPhase(phase).value,
                    started_at=started_at,
                    duration_ms=duration_ms,
                )
                .on_conflict_do_nothing(index_elements=["attempt_id", "phase"])
                .returning(AttemptPhaseRow.phase)
            )
            return inserted is not None

    @typed_store_call
    async def phases(self, attempt_id: str) -> Sequence[PhaseRecord]:
        async with self._sessions() as db:
            rows = await db.scalars(
                select(AttemptPhaseRow)
                .where(AttemptPhaseRow.attempt_id == attempt_id)
                .order_by(AttemptPhaseRow.started_at)
            )
            return [
                PhaseRecord(
                    phase=r.phase,
                    started_at=r.started_at,
                    duration_ms=r.duration_ms,
                    skipped=r.skipped,
                )
                for r in rows
            ]


async def append_transition(
    db: AsyncSession,
    job_id: str,
    from_state: JobState | None,
    to_state: JobState,
    *,
    at: Any = None,
) -> None:
    """Public entry to the history writer (resolved at call time, so a test that
    patches ``_append_transition`` patches every writer)."""
    await _append_transition(db, job_id, from_state, to_state, at=at)
