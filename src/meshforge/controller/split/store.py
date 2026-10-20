"""The chunk protocol's database side (V0.10 G4, D262/D263/D267; ADR-0019).

Every operation is **one transaction** that first checks the lane - the
attempt exists, is a ``lane``, runs on the node whose session sent the message
(the session's mTLS identity, V0.7 G7), and carries the lane's fencing token -
then hands the chunk to :mod:`meshforge.workqueue.store` (holder = the lane's
attempt id, site = its node). Lock order (ADR-0019 addendum): the attempt row,
then the job row, then work-queue rows - the order of every other writer of
these rows, so no deadlock.

* :meth:`ChunkStore.request` - a lane asks for chunks: leases it holds but did
  not list are granted again (their grant was lost on the way), then new ones
  in chunk order; nothing left = ``NoWork`` (``drained``: the queue is done;
  ``wait``: the rest is leased elsewhere and may come back; ``stop``: the lane
  or its job ended).
* :meth:`ChunkStore.result` - a chunk's outcome: ``done`` commits it (first
  committed result wins; a later one is ``already_done``); the job's **last**
  commit writes the parts index blob and ends the job ``SUCCEEDED`` in the same
  transaction (checklist 35: commit + bookkeeping + lease release together).
  ``failed`` counts a try; the chunk failing for good (``max_attempts`` /
  ``max_failed_nodes``, or a non-retryable failure) fails the job with the
  chunk named and closes the queue - **no partial output** (checklist 32).
  ``released`` gives the lease back uncounted.
* :meth:`ChunkStore.renew` / :meth:`ChunkStore.expire` - visibility timeout.

A message that fails a lane check is ``fenced``: nothing is applied, the
caller counts and logs it (checklist 34). A **late** result (D267b) - from a
lease that expired, or from a lane the controller already ended ``LOST`` - is
still offered to the queue, which commits it only while the chunk is not done
(at-least-once execution, exactly-once commit); it must still come from the
lane's own node with the lane's own token and digest verified under its grant.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import OrderedDict
from collections.abc import AsyncIterator, Collection, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.common.logging import get_logger
from meshforge.common.manifest import DEFAULT_CHUNK_OUTPUT_BYTES
from meshforge.common.split_chunks import (
    ItemList,
    PartsIndex,
    chunk_items_digest,
    chunk_span,
    parse_item_list,
    parse_parts_index,
    parts_index_blob,
)
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    JOB_TERMINAL_STATES,
    AttemptRole,
    AttemptState,
    JobState,
)
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.execution_store import _fresh, _move_job
from meshforge.controller.jobs.store import clean_detail, typed_store_call
from meshforge.controller.jobs.transitions import JobEvent
from meshforge.controller.split.lanes import QUEUE_KIND, LiveLane, live_lanes
from meshforge.controller.split.warnings import (
    NONDETERMINISTIC_CHUNK,
    add_chunk_warning,
)
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.job import Attempt, Job
from meshforge.workqueue import store as wq
from meshforge.workqueue.core import (
    MAX_FAILURE_KIND_CHARS,
    AlreadyDone,
    Fenced,
    ItemFailed,
    Lease,
    LeaseState,
    QueueState,
    Renewed,
)
from meshforge.workqueue.tables import work_item, work_lease

__all__ = [
    "ChunkGrantOut",
    "ChunkStore",
    "FenceCause",
    "GrantOut",
    "ItemLists",
    "NoWorkOut",
    "RequestOutcome",
    "ResultKind",
    "ResultOutcome",
]

log = get_logger(__name__)

_ITEM_LIST_CACHE: Final = 8
"""Item lists kept parsed (one per running split job; bounded)."""


class FenceCause(StrEnum):
    """Why a chunk message was not applied (checklist 34)."""

    UNKNOWN_LANE = "unknown_lane"
    WRONG_NODE = "wrong_node"
    NOT_A_LANE = "not_a_lane"
    STALE_TOKEN = "stale_token"  # noqa: S105 - a fencing outcome, not a secret
    NO_QUEUE = "no_queue"


# ------------------------------------------------------------------ item lists


class ItemLists:
    """Parsed item lists by digest, read from the controller's CAS (bounded
    LRU; parsing runs in a worker thread)."""

    def __init__(self, blobs: BlobStore | None, *, entries: int = _ITEM_LIST_CACHE):
        self._blobs = blobs
        self._entries = entries
        self._cache: OrderedDict[str, tuple[ItemList, dict[int, str]]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, digest: str) -> ItemList:
        async with self._lock:
            hit = self._cache.get(digest)
            if hit is not None:
                self._cache.move_to_end(digest)
                return hit[0]
            if self._blobs is None:
                raise LookupError("no blob store: item lists cannot be read")
            path = self._blobs.open_path(Digest.parse(digest))

            def load() -> ItemList:
                data = path.read_bytes()
                if sha256_bytes(data) != Digest.parse(digest):
                    raise ValueError("the stored item list does not match its digest")
                return parse_item_list(data)

            parsed = await asyncio.to_thread(load)
            self._cache[digest] = (parsed, {})
            while len(self._cache) > self._entries:
                self._cache.popitem(last=False)
            return parsed

    def slice_digest(self, digest: str, items: ItemList, first: int, count: int) -> str:
        """The digest of one chunk's items, memoised per list."""
        memo = self._cache.get(digest, (items, {}))[1]
        found = memo.get(first)
        if found is None:
            found = str(chunk_items_digest(items.items[first : first + count]))
            memo[first] = found
        return found


# ------------------------------------------------------------------ outcomes


@dataclass(frozen=True, slots=True)
class GrantOut:
    chunk_index: int
    item_id: int
    lease_seq: int
    first_item: int
    item_count: int
    items_digest: str
    speculative: bool


@dataclass(frozen=True, slots=True)
class ChunkGrantOut:
    chunks: tuple[GrantOut, ...]
    regranted: int
    """Of ``chunks``, how many the lane already held (its grant got lost)."""
    chunk_cap: int
    """The job's ``split.max_chunk_output_bytes`` (the lane's grant grows by
    it per new chunk)."""


@dataclass(frozen=True, slots=True)
class NoWorkOut:
    reason: str
    """``drained`` | ``wait`` | ``stop``."""


type RequestOutcome = ChunkGrantOut | NoWorkOut | FenceCause


class ResultKind(StrEnum):
    COMMITTED = "committed"
    ALREADY_DONE = "already_done"
    RETRIED = "retried"
    CHUNK_FAILED = "chunk_failed"
    RELEASED = "released"
    FENCED = "fenced"


@dataclass(frozen=True, slots=True)
class ResultOutcome:
    kind: ResultKind
    fence: FenceCause | str | None = None
    job_id: str | None = None
    job_ended: JobState | None = None
    """SUCCEEDED (last commit) or FAILED (a chunk failed for good) now."""
    cancel_chunks: tuple[tuple[LiveLane, int, int], ...] = ()
    """``(lane, item_id, seq)`` leases that lost to this commit: stop them."""
    cancel_lanes: tuple[LiveLane, ...] = ()
    """Lanes to stop because the job ended."""
    parts_digest: str | None = None
    late: bool = False
    combine_owed: bool = False
    """V0.10 G5: this was the last commit - the job is COLLECTING and owes a
    combine (the dispatcher places it; wake it)."""
    nondeterministic: bool = False
    """V0.10 G5 (D269): an ``already_done`` result whose digest differs from
    the committed one while ``split.deterministic`` is on - recorded as the
    job's ``nondeterministic_chunk`` warning in this transaction."""


# ------------------------------------------------------------------ the store


@dataclass(slots=True)
class _Lane:
    attempt: Attempt
    job: Job
    queue: wq.QueueInfo | None = field(default=None)


class ChunkStore:
    """See module docstring."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        blobs: BlobStore | None,
        items: ItemLists | None = None,
    ) -> None:
        self._sessions: async_sessionmaker[AsyncSession] = create_session_factory(
            engine
        )
        self._blobs = blobs
        self.items = items or ItemLists(blobs)
        self._parts: OrderedDict[str, PartsIndex] = OrderedDict()

    async def _lane(
        self,
        db: AsyncSession,
        *,
        node_id: str,
        attempt_id: str,
        token: int,
        lock_job: bool,
    ) -> _Lane | FenceCause:
        attempt = await db.get(Attempt, attempt_id, with_for_update={"read": True})
        if attempt is None:
            return FenceCause.UNKNOWN_LANE
        if attempt.node_id != node_id:
            return FenceCause.WRONG_NODE
        if attempt.role != AttemptRole.LANE.value:
            return FenceCause.NOT_A_LANE
        if int(attempt.fencing_token) != token:
            return FenceCause.STALE_TOKEN
        job = await db.get_one(
            Job, attempt.job_id, with_for_update=True if lock_job else {"read": True}
        )
        queue = await wq.find_queue(db, kind=QUEUE_KIND, owner_id=job.id)
        return _Lane(attempt, job, queue)

    # ------------------------------------------------------------ request

    @typed_store_call
    async def request(
        self,
        *,
        node_id: str,
        attempt_id: str,
        token: int,
        want: int,
        held: Collection[tuple[int, int]],
        now: dt.datetime,
        avoid: Collection[int] = (),
    ) -> RequestOutcome:
        """``avoid``: chunk item ids this lane must not get now (a cool-down
        after its unverified result); they are left pending for other lanes."""
        async with self._sessions() as db, db.begin():
            lane = await self._lane(
                db, node_id=node_id, attempt_id=attempt_id, token=token, lock_job=False
            )
            if isinstance(lane, FenceCause):
                return lane
            attempt, job, queue = lane.attempt, lane.job, lane.queue
            if AttemptState(attempt.state) in ATTEMPT_TERMINAL_STATES:
                return NoWorkOut("stop")
            if queue is None:
                return FenceCause.NO_QUEUE
            if queue.state is QueueState.DONE:
                return NoWorkOut("drained")  # every chunk committed: end DONE
            if (
                queue.state is not QueueState.OPEN
                or JobState(job.state) in JOB_TERMINAL_STATES
                or job.cancel_requested_at is not None
            ):
                return NoWorkOut("stop")
            if job.split_items_digest is None:  # pragma: no cover - CHECK
                return FenceCause.NO_QUEUE
            digest = job.split_items_digest
            items = await self.items.get(digest)
            held_set = set(held)
            # A live lease the lane did not list: its grant was lost - again.
            lost = [
                row
                for row in await _live_leases_of(db, queue.id, attempt_id)
                if (row.item_id, row.seq) not in held_set
            ]
            k = max(0, min(want, wq.MAX_LEASES_PER_PULL) - len(lost))
            skip = set(avoid)
            pulled = (
                await wq.lease_next(
                    db,
                    queue.id,
                    holder=attempt_id,
                    site=node_id,
                    k=min(wq.MAX_LEASES_PER_PULL, k + len(skip)),
                    now=now,
                )
                if k > 0
                else []
            )
            fresh = [g for g in pulled if g.item_id not in skip][:k]
            for grant in pulled:
                if grant not in fresh:  # cooling down for this lane, or extra
                    await wq.release(
                        db,
                        item_id=grant.item_id,
                        seq=grant.seq,
                        holder=attempt_id,
                        now=now,
                    )
            out = [
                self._grant_out(
                    digest,
                    items,
                    idx=row.idx,
                    item_id=row.item_id,
                    seq=row.seq,
                    payload=row.payload,
                    speculative=row.speculative,
                )
                for row in [*lost, *fresh]
            ]
            regranted = len(lost)
            if out:
                return ChunkGrantOut(tuple(out), regranted, _chunk_cap(job))
            counts = await wq.counts(db, queue.id)
            if counts.pending == 0 and counts.leased == 0:
                return NoWorkOut("drained")
            return NoWorkOut("wait")

    def _grant_out(
        self,
        digest: str,
        items: ItemList,
        *,
        idx: int,
        item_id: int,
        seq: int,
        payload: Any,
        speculative: bool,
    ) -> GrantOut:
        span = chunk_span(payload, items.count)
        return GrantOut(
            chunk_index=idx,
            item_id=item_id,
            lease_seq=seq,
            first_item=span.first,
            item_count=span.count,
            items_digest=self.items.slice_digest(digest, items, span.first, span.count),
            speculative=speculative,
        )

    # ------------------------------------------------------------ results

    @typed_store_call
    async def result(
        self,
        *,
        node_id: str,
        attempt_id: str,
        token: int,
        item_id: int,
        seq: int,
        outcome: str,
        digest: str | None,
        failure: tuple[FailureType, str, bool] | None,
        now: dt.datetime,
        size: int | None = None,
    ) -> ResultOutcome:
        """``outcome`` = ``done`` (``digest`` verified by the caller under the
        lane's grant, ``size`` = the bytes that grant stored for it),
        ``failed`` (``failure`` = type, detail, retryable) or ``released``. A
        done chunk over ``split.max_chunk_output_bytes`` fails ``output_error``
        (not retried: it would be as large anywhere)."""
        async with self._sessions() as db, db.begin():
            lane = await self._lane(
                db, node_id=node_id, attempt_id=attempt_id, token=token, lock_job=True
            )
            if isinstance(lane, FenceCause):
                return ResultOutcome(ResultKind.FENCED, fence=lane)
            job, queue = lane.job, lane.queue
            if queue is None:
                return ResultOutcome(ResultKind.FENCED, FenceCause.NO_QUEUE, job.id)
            if outcome == "released":
                out = await wq.release(
                    db, item_id=item_id, seq=seq, holder=attempt_id, now=now
                )
                return ResultOutcome(
                    ResultKind.RELEASED if out is not None else ResultKind.FENCED,
                    None if out is not None else "lease_not_live",
                    job.id,
                )
            cap = _chunk_cap(job)
            if outcome == "done" and size is not None and size > cap:
                outcome, failure = (
                    "failed",
                    (
                        FailureType.OUTPUT_ERROR,
                        f"output_limit: the chunk's packed output is {size} bytes, "
                        f"more than split.max_chunk_output_bytes ({cap})",
                        False,
                    ),
                )
            if outcome == "failed":
                return await self._failed(db, lane, item_id, seq, failure, now)
            if digest is None:  # pragma: no cover - the caller checked
                raise ValueError("a done result needs a digest")
            done = await wq.complete(
                db, item_id=item_id, seq=seq, holder=attempt_id, digest=digest, now=now
            )
            got = done.outcome
            if isinstance(got, Fenced):
                return ResultOutcome(ResultKind.FENCED, got.reason.value, job.id)
            if isinstance(got, AlreadyDone):
                mismatch = await _check_determinism(db, job, got, digest)
                return ResultOutcome(
                    ResultKind.ALREADY_DONE, None, job.id, nondeterministic=mismatch
                )
            cancel = await _lanes_of(db, got.superseded)
            if not done.queue_done:
                return ResultOutcome(
                    ResultKind.COMMITTED,
                    job_id=job.id,
                    cancel_chunks=cancel,
                    late=got.late,
                )
            parts = await self._finish(db, job, queue.id)
            return ResultOutcome(
                ResultKind.COMMITTED,
                job_id=job.id,
                cancel_chunks=cancel,
                cancel_lanes=tuple(await live_lanes(db, job.id)),
                parts_digest=parts,
                late=got.late,
                combine_owed=JobState(job.state) is JobState.COLLECTING,
            )

    async def _failed(
        self,
        db: AsyncSession,
        lane: _Lane,
        item_id: int,
        seq: int,
        failure: tuple[FailureType, str, bool] | None,
        now: dt.datetime,
    ) -> ResultOutcome:
        kind, text, retryable = failure or (
            FailureType.INTERNAL_ERROR,
            "the chunk failed",
            False,
        )
        job = lane.job
        res = await wq.fail(
            db,
            item_id=item_id,
            seq=seq,
            holder=lane.attempt.id,
            kind=kind.value[:MAX_FAILURE_KIND_CHARS],
            now=now,
            retryable=retryable,
        )
        got = res.outcome
        if isinstance(got, Fenced):
            return ResultOutcome(ResultKind.FENCED, got.reason.value, job.id)
        if isinstance(got, AlreadyDone):
            return ResultOutcome(ResultKind.ALREADY_DONE, None, job.id)
        if not isinstance(got, ItemFailed):
            return ResultOutcome(ResultKind.RETRIED, None, job.id)
        # D267c: the chunk failed for good -> the job fails, no partial output
        nodes = await _failed_sites(db, item_id)
        detail = clean_detail(
            f"chunk_failed: chunk {got.idx} failed on {len(nodes)} node(s) "
            f"({', '.join(nodes[:4])}): {kind.value}: {text}"
        )
        if JobState(job.state) is JobState.PREPARING:
            await _move_job(db, job, JobEvent.STARTED, soft=True)
        await _move_job(
            db, job, JobEvent.FAIL, failure_type=kind, detail=detail, soft=True
        )
        await _fresh(db, job)
        log.warning(
            "controller.split.chunk_failed",
            job_id=job.id,
            chunk=got.idx,
            nodes=nodes,
            failure_type=kind.value,
            released=len(res.released),
        )
        return ResultOutcome(
            ResultKind.CHUNK_FAILED,
            None,
            job.id,
            job_ended=JobState(job.state),
            cancel_lanes=tuple(await live_lanes(db, job.id)),
        )

    async def _finish(self, db: AsyncSession, job: Job, queue_id: int) -> str | None:
        """The last chunk committed: write the parts index (chunk order - the
        combine's ordered input) and move the job ``RUNNING -> COLLECTING``,
        in the committing transaction (V0.10 G5, D283 pin 1). A COLLECTING
        split job with a parts index and no live combine attempt **owes a
        combine**: the dispatcher places it on its next tick (woken by the
        caller) - the durable fact is this commit, so a controller crash right
        after it loses nothing (:mod:`.combine`)."""
        rows = (
            await db.execute(
                select(work_item.c.idx, work_item.c.payload, work_item.c.result_digest)
                .where(work_item.c.queue_id == queue_id)
                .order_by(work_item.c.idx)
            )
        ).all()
        if job.split_items_digest is None or any(
            r.result_digest is None for r in rows
        ):  # pragma: no cover - the queue said every chunk is done
            raise ValueError(
                f"job {job.id}: a done queue with a chunk lacking a result"
            )
        count = int(job.split_item_count or 0)
        spans = [chunk_span(r.payload, count) for r in rows]
        blob = parts_index_blob(
            items_digest=job.split_items_digest,
            chunk_items=spans[0].count,
            chunks=[
                (s.first, s.count, str(r.result_digest))
                for s, r in zip(spans, rows, strict=True)
            ],
        )
        digest = sha256_bytes(blob)
        if self._blobs is None:
            raise LookupError("no blob store: the parts index cannot be stored")
        await self._blobs.put(digest, _one(blob), declared_size=len(blob))
        job.split_parts_digest = str(digest)
        state = JobState(job.state)
        if state is JobState.PREPARING:
            await _move_job(db, job, JobEvent.STARTED, soft=True)
        await _move_job(db, job, JobEvent.EXITED, soft=True)
        await _fresh(db, job)
        log.info(
            "controller.split.chunks_done",
            job_id=job.id,
            chunks=len(rows),
            parts_digest=str(digest),
            state=job.state,
        )
        return str(digest)

    # ------------------------------------------------------------ combine

    async def parts_index(self, digest: str) -> PartsIndex:
        """V0.10 G5: a job's parts index from the controller's CAS (verified,
        parsed in a worker thread; a few kept). Raises ``LookupError`` /
        ``ValueError`` when it is missing or broken."""
        hit = self._parts.get(digest)
        if hit is not None:
            self._parts.move_to_end(digest)
            return hit
        if self._blobs is None:
            raise LookupError("no blob store: the parts index cannot be read")
        path = self._blobs.open_path(Digest.parse(digest))

        def load() -> PartsIndex:
            data = path.read_bytes()
            if sha256_bytes(data) != Digest.parse(digest):
                raise ValueError("the stored parts index does not match its digest")
            return parse_parts_index(data)

        parsed = await asyncio.to_thread(load)
        self._parts[digest] = parsed
        while len(self._parts) > _ITEM_LIST_CACHE:
            self._parts.popitem(last=False)
        return parsed

    # ------------------------------------------------------------ renew/expire

    @typed_store_call
    async def renew(
        self,
        *,
        node_id: str,
        attempt_id: str,
        token: int,
        leases: Sequence[tuple[int, int]],
        timeout_s: float | None,
        now: dt.datetime,
    ) -> tuple[int, int] | FenceCause:
        """``(renewed, refused)``; a lane check failure renews nothing."""
        async with self._sessions() as db, db.begin():
            lane = await self._lane(
                db, node_id=node_id, attempt_id=attempt_id, token=token, lock_job=False
            )
            if isinstance(lane, FenceCause):
                return lane
            if AttemptState(lane.attempt.state) in ATTEMPT_TERMINAL_STATES:
                return (0, len(leases))
            outs = await wq.renew(
                db, holder=attempt_id, leases=leases, now=now, timeout_s=timeout_s
            )
            renewed = sum(1 for o in outs if isinstance(o, Renewed))
            return (renewed, len(outs) - renewed)

    @typed_store_call
    async def expire(self, *, now: dt.datetime, limit: int = 500) -> int:
        """Expire due leases (their chunks go back to pending)."""
        async with self._sessions() as db, db.begin():
            result = await wq.expire_due(db, now=now, limit=limit)
        return sum(len(e.leases) for e in result.expired)

    @typed_store_call
    async def held_leases(self, attempt_id: str) -> int:
        """Live leases of one lane (tests, metrics)."""
        async with self._sessions() as db:
            found = await db.execute(
                select(work_lease.c.id).where(
                    work_lease.c.holder == attempt_id,
                    work_lease.c.state == LeaseState.LIVE.value,
                )
            )
            return len(found.all())


# ------------------------------------------------------------------ helpers


async def _one(data: bytes) -> AsyncIterator[bytes]:
    yield data


def _chunk_cap(job: Job) -> int:
    """The job's ``split.max_chunk_output_bytes`` (the canonical spec holds
    it; an older stored spec without it gets the default)."""
    value = (job.split or {}).get("max_chunk_output_bytes")
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return DEFAULT_CHUNK_OUTPUT_BYTES


async def _live_leases_of(db: AsyncSession, queue_id: int, holder: str) -> list[Any]:
    return list(
        (
            await db.execute(
                select(
                    work_lease.c.item_id,
                    work_lease.c.seq,
                    work_lease.c.speculative,
                    work_item.c.idx,
                    work_item.c.payload,
                )
                .join(work_item, work_item.c.id == work_lease.c.item_id)
                .where(
                    work_item.c.queue_id == queue_id,
                    work_lease.c.holder == holder,
                    work_lease.c.state == LeaseState.LIVE.value,
                )
                .order_by(work_item.c.idx)
            )
        ).all()
    )


async def _check_determinism(
    db: AsyncSession, job: Job, done: AlreadyDone, digest: str
) -> bool:
    """D269 hook (V0.10 G5): a second, *verified* result of a committed chunk
    with another digest, while the job asked for deterministic chunks ->
    the job's ``nondeterministic_chunk`` warning (the job row is locked by the
    caller). A replay of the winner's own report is not a second result."""
    if done.own or done.winner_digest == digest:
        return False
    if not bool((job.split or {}).get("deterministic", True)):
        return False
    idx = await db.scalar(select(work_item.c.idx).where(work_item.c.id == done.item_id))
    chunk = int(idx) if idx is not None else -1
    job.warnings, _fresh_chunk = add_chunk_warning(
        job.warnings, NONDETERMINISTIC_CHUNK, max(chunk, 0)
    )
    log.warning(
        "controller.split.nondeterministic_chunk",
        job_id=job.id,
        chunk=chunk,
        winner_seq=done.winner_seq,
        lease_seq=done.seq,
    )
    return True


async def _failed_sites(db: AsyncSession, item_id: int) -> list[str]:
    found = await db.scalar(
        select(work_item.c.failed_sites).where(work_item.c.id == item_id)
    )
    return [str(s) for s in (found or ())]


async def _lanes_of(
    db: AsyncSession, leases: Sequence[Lease]
) -> tuple[tuple[LiveLane, int, int], ...]:
    if not leases:
        return ()
    rows = (
        await db.execute(
            select(Attempt.id, Attempt.node_id, Attempt.fencing_token).where(
                Attempt.id.in_({x.holder for x in leases})
            )
        )
    ).all()
    by_id = {r.id: LiveLane(r.id, r.node_id, int(r.fencing_token)) for r in rows}
    return tuple(
        (by_id[x.holder], x.item_id, x.seq) for x in leases if x.holder in by_id
    )
