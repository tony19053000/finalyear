"""``GET /v1/jobs/{job_id}/split`` and ``GET /v1/jobs/{job_id}/chunks`` - a split
job's progress per chunk and per node (V0.10 G7, D270; roadmap 0.10.8).

Same access rule as every ``/v1/jobs*`` read (V0.7 G4, D141): a user, who sees
only their own jobs (another user's job is 404, exactly like a missing one);
an admin sees every job. A job that is not a split job is 404 ``no such split
job``.

* ``/split`` - the summary (:class:`SplitProgressOut`): chunk counts by state,
  items done, per lane (node, chunks/items committed, current chunks, measured
  items/s), ETA, elapsed vs predicted makespan, warnings. ``?map=true`` adds
  ``chunk_map``: one character per chunk in index order (``p`` pending, ``l``
  leased, ``s`` leased with a speculative copy, ``d`` done, ``f`` failed).
* ``/chunks`` - the chunks themselves, index order, keyset pages
  (``limit``, ``cursor`` = the previous page's ``next_cursor``), filters
  ``state`` (repeatable) and ``node`` (chunks that node holds now or whose
  committed result came from it); ``start=N`` begins at chunk N (the
  dashboard's "this chunk" panel: ``?start=N&limit=1``).

Both are read **from the rows on every request** (one ``REPEATABLE READ``
snapshot for the summary): the numbers shown equal the rows (checklist 37).
The ``split.progress`` event carries the same summary without the map.
"""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Final, Literal

from fastapi import APIRouter, Path, Query

from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_job_id, validate_node_id
from meshforge.common.manifest import MAX_SPLIT_CHUNKS
from meshforge.common.states import JobState
from meshforge.controller.api.auth import CurrentUser
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.job_routes import JobApi, Jobs
from meshforge.controller.api.models import ApiModel, Utc
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.split.progress import LaneProgress, SplitProgress
from meshforge.controller.split.progress_reads import ChunkRow, ProgressReads
from meshforge.workqueue.core import ItemState

__all__ = [
    "ChunkList",
    "ChunkOut",
    "ChunkTries",
    "ChunkTryOut",
    "SplitProgressOut",
    "decode_chunk_cursor",
    "encode_chunk_cursor",
    "split_router",
]

DEFAULT_CHUNK_LIMIT: Final = 200
MAX_CHUNK_LIMIT: Final = 500
_JOB_ID_MAX: Final = 40
_CURSOR_PREFIX: Final = "v1|chunk|"
_MAX_CURSOR_CHARS: Final = 40

split_router = APIRouter(prefix="/v1/jobs", tags=["jobs"])

ChunkState = Literal["pending", "leased", "done", "failed"]
QueueState = Literal["open", "done", "failed", "cancelled"]


class ChunkCountsOut(ApiModel):
    total: int
    pending: int
    leased: int
    done: int
    failed: int


class CurrentChunkOut(ApiModel):
    chunk_index: int
    lease_seq: int
    speculative: bool
    leased_at: Utc


class LaneOut(ApiModel):
    attempt_id: str
    node_id: str
    state: str
    """The lane attempt's state (``running``, ``done``, ``lost``, ...)."""
    live: bool
    chunks_done: int
    """Chunks whose committed result came from this lane."""
    items_done: int
    failed_tries: int
    """Chunk tries of this lane that failed (counted against the chunk)."""
    current: tuple[CurrentChunkOut, ...]
    """Chunks it holds now (one running, one staging; ``speculative`` = a copy)."""
    rate_items_s: float | None
    """Measured items/s over its latest committed chunks; null = none yet."""
    started_at: Utc | None
    first_lease_at: Utc | None
    last_commit_at: Utc | None
    ended_at: Utc | None


class SplitWarningOut(ApiModel):
    code: str
    count: int


class SplitProgressOut(ApiModel):
    job_id: str
    job_state: JobState
    queue_state: QueueState | None
    """null = not dispatched yet (no chunks exist)."""
    items_total: int
    chunk_items: int | None
    """Items per chunk (the last chunk may hold fewer)."""
    chunks: ChunkCountsOut
    items_done: int
    live_leases: int
    speculative_live: int
    speculated: int
    lanes: tuple[LaneOut, ...]
    lanes_total: int
    """Lane attempts the job ever had (``lanes`` lists at most 64)."""
    rate_items_s: float | None
    """Sum of the live lanes' measured rates."""
    eta_s: float | None
    """Remaining items / summed live rate; null = nothing measured yet."""
    started_at: Utc | None
    ended_at: Utc | None
    elapsed_s: float | None
    predicted_s: float | None
    """The planner's predicted makespan for the job's first dispatch."""
    combine_ms: int | None
    warnings: tuple[SplitWarningOut, ...]
    computed_at: Utc
    chunk_map: str | None = None


class ChunkLeaseOut(ApiModel):
    attempt_id: str
    node_id: str
    lease_seq: int
    speculative: bool
    leased_at: Utc
    deadline: Utc


class ChunkOut(ApiModel):
    chunk_index: int
    state: ChunkState
    first_item: int | None
    item_count: int
    attempts: int
    """Counted tries so far."""
    speculations: int
    node_id: str | None
    """Done: the node whose result was committed; leased: its holder."""
    attempt_id: str | None
    result_digest: str | None
    done_at: Utc | None
    failed_kind: str | None
    failed_nodes: tuple[str, ...]
    leases: tuple[ChunkLeaseOut, ...]


class ChunkList(ApiModel):
    job_id: str
    chunks: tuple[ChunkOut, ...]
    count: int
    next_cursor: str | None


ChunkOutcome = Literal[
    "committed", "lost", "failed", "superseded", "released", "expired"
]


class ChunkTryOut(ApiModel):
    """One recorded try of a chunk (its chunk record, V0.10 G8, D271)."""

    lease_seq: int
    attempt_id: str
    """The lane attempt that ran it."""
    node_id: str
    speculative: bool
    won: bool
    outcome: ChunkOutcome
    failure_type: str | None
    failure_class: str
    exit_code: int | None
    result_digest: str | None
    bytes_in: int | None
    bytes_out: int | None
    stage_ms: int | None
    env_ms: int | None
    run_ms: int | None
    upload_ms: int | None
    leased_at: Utc
    ended_at: Utc | None
    stderr_tail: str | None
    """The end of the chunk process's stderr (redacted of the job's secrets
    on the agent; at most ``[controller.split] chunk_stderr_tail_bytes`` for a
    failed try, ``chunk_stderr_ok_bytes`` for a done one; no terminal escape
    sequence or control character but ``\\n`` / ``\\t``); null = none kept."""


class ChunkTries(ApiModel):
    job_id: str
    chunk_index: int
    tries: tuple[ChunkTryOut, ...]
    """In lease order; the latest ``limit`` of them."""
    count: int


# ------------------------------------------------------------------ cursors


def encode_chunk_cursor(chunk_index: int) -> str:
    raw = f"{_CURSOR_PREFIX}{chunk_index}".encode("ascii")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def decode_chunk_cursor(cursor: str) -> int:
    """The last chunk index of the previous page, or 400 ``invalid_input``."""
    bad = ApiError.invalid("cursor is not a cursor from this list's next_cursor")
    if not cursor or len(cursor) > _MAX_CURSOR_CHARS:
        raise bad
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeError, binascii.Error):
        raise bad from None
    if not raw.startswith(_CURSOR_PREFIX):
        raise bad
    digits = raw.removeprefix(_CURSOR_PREFIX)
    if not digits.isascii() or not digits.isdigit() or len(digits) > 6:
        raise bad
    index = int(digits)
    if index >= MAX_SPLIT_CHUNKS:
        raise bad
    return index


# ------------------------------------------------------------------ helpers


def _reads(jobs: JobApi) -> ProgressReads:
    if jobs.split_progress is None:
        raise ApiError(FailureType.CONFIG_ERROR, "split progress is not configured")
    return jobs.split_progress


async def _split_job(user: CurrentUser, jobs: JobApi, job_id: str) -> JobRecord:
    try:
        checked = str(validate_job_id(job_id))
    except InvalidIdError:
        raise ApiError.invalid("job_id is not a canonical job_<ULID>") from None
    job = await jobs.store.get_job(checked)
    if job is None or not user.may_see_job(job.owner_user_id):
        raise ApiError.not_found("job")
    if job.split is None:
        raise ApiError.not_found("split job")
    return job


def _lane_out(lane: LaneProgress) -> LaneOut:
    return LaneOut(
        attempt_id=lane.attempt_id,
        node_id=lane.node_id,
        state=lane.state,
        live=lane.live,
        chunks_done=lane.chunks_done,
        items_done=lane.items_done,
        failed_tries=lane.failed_tries,
        current=tuple(
            CurrentChunkOut(
                chunk_index=c.chunk_index,
                lease_seq=c.lease_seq,
                speculative=c.speculative,
                leased_at=c.leased_at,
            )
            for c in lane.current
        ),
        rate_items_s=lane.rate_items_s,
        started_at=lane.started_at,
        first_lease_at=lane.first_lease_at,
        last_commit_at=lane.last_commit_at,
        ended_at=lane.ended_at,
    )


def progress_out(p: SplitProgress) -> SplitProgressOut:
    c = p.chunks
    return SplitProgressOut(
        job_id=p.job_id,
        job_state=p.job_state,
        queue_state=p.queue_state,  # type: ignore[arg-type]  # CHECK-constrained
        items_total=p.items_total,
        chunk_items=p.chunk_items,
        chunks=ChunkCountsOut(
            total=c.total,
            pending=c.pending,
            leased=c.leased,
            done=c.done,
            failed=c.failed,
        ),
        items_done=p.items_done,
        live_leases=p.live_leases,
        speculative_live=p.speculative_live,
        speculated=p.speculated,
        lanes=tuple(_lane_out(lane) for lane in p.lanes),
        lanes_total=p.lanes_total,
        rate_items_s=p.rate_items_s,
        eta_s=p.eta_s,
        started_at=p.started_at,
        ended_at=p.ended_at,
        elapsed_s=p.elapsed_s,
        predicted_s=p.predicted_s,
        combine_ms=p.combine_ms,
        warnings=tuple(SplitWarningOut(code=k, count=n) for k, n in p.warnings),
        computed_at=p.computed_at,
        chunk_map=p.chunk_map,
    )


def _chunk_out(row: ChunkRow) -> ChunkOut:
    return ChunkOut(
        chunk_index=row.chunk_index,
        state=row.state,  # type: ignore[arg-type]  # CHECK-constrained
        first_item=row.first_item,
        item_count=row.item_count,
        attempts=row.attempts,
        speculations=row.speculations,
        node_id=row.node_id,
        attempt_id=row.attempt_id,
        result_digest=row.result_digest,
        done_at=row.done_at,
        failed_kind=row.failed_kind,
        failed_nodes=row.failed_nodes,
        leases=tuple(
            ChunkLeaseOut(
                attempt_id=le.attempt_id,
                node_id=le.node_id,
                lease_seq=le.lease_seq,
                speculative=le.speculative,
                leased_at=le.leased_at,
                deadline=le.deadline,
            )
            for le in row.leases
        ),
    )


# ------------------------------------------------------------------ routes


@split_router.get(
    "/{job_id}/split",
    response_model=SplitProgressOut,
    summary="A split job's progress: chunks by state, lanes per node, ETA",
)
async def split_progress(
    user: CurrentUser,
    jobs: Jobs,
    job_id: Annotated[str, Path(max_length=_JOB_ID_MAX)],
    map_: Annotated[
        bool,
        Query(
            alias="map",
            description="Add chunk_map: one state character per chunk "
            "(p pending, l leased, s leased + speculative copy, d done, f failed).",
        ),
    ] = False,
) -> SplitProgressOut:
    reads = _reads(jobs)
    job = await _split_job(user, jobs, job_id)
    progress = await reads.progress(job.id, with_map=map_)
    if progress is None:  # pragma: no cover - checked as a split job above
        raise ApiError.not_found("split job")
    return progress_out(progress)


@split_router.get(
    "/{job_id}/chunks",
    response_model=ChunkList,
    summary="A split job's chunks in index order - keyset pages and filters",
)
async def split_chunks(
    user: CurrentUser,
    jobs: Jobs,
    job_id: Annotated[str, Path(max_length=_JOB_ID_MAX)],
    limit: Annotated[
        int, Query(ge=1, le=MAX_CHUNK_LIMIT, description="Chunks per page.")
    ] = DEFAULT_CHUNK_LIMIT,
    state: Annotated[
        list[ChunkState] | None,
        Query(description="Only chunks in this state (repeat for several)."),
    ] = None,
    node: Annotated[
        str | None,
        Query(
            max_length=40,
            description="Only chunks this node holds now or whose committed "
            "result came from it.",
        ),
    ] = None,
    cursor: Annotated[
        str | None,
        Query(
            max_length=_MAX_CURSOR_CHARS,
            description="The previous page's next_cursor.",
        ),
    ] = None,
    start: Annotated[
        int | None,
        Query(
            ge=0,
            lt=MAX_SPLIT_CHUNKS,
            description="Start at this chunk index (instead of a cursor): "
            "one chunk is ?start=N&limit=1.",
        ),
    ] = None,
) -> ChunkList:
    reads = _reads(jobs)
    try:
        node_id = None if node is None else str(validate_node_id(node))
    except InvalidIdError:
        raise ApiError.invalid("node is not a canonical node_<ULID>") from None
    if cursor is not None and start is not None:
        raise ApiError.invalid("give cursor or start, not both")
    after = None if cursor is None else decode_chunk_cursor(cursor)
    if start is not None:
        after = start - 1
    job = await _split_job(user, jobs, job_id)
    rows = await reads.chunk_page(
        job.id,
        limit=limit + 1,  # one more: is there a next page?
        after_idx=after,
        states=None if not state else [ItemState(s) for s in state],
        node_id=node_id,
    )
    if rows is None:
        return ChunkList(job_id=job.id, chunks=(), count=0, next_cursor=None)
    page, more = rows[:limit], len(rows) > limit
    chunks = tuple(_chunk_out(r) for r in page)
    return ChunkList(
        job_id=job.id,
        chunks=chunks,
        count=len(chunks),
        next_cursor=encode_chunk_cursor(page[-1].chunk_index)
        if page and more
        else None,
    )


DEFAULT_TRY_LIMIT: Final = 20
MAX_TRY_LIMIT: Final = 100


@split_router.get(
    "/{job_id}/chunks/{chunk_index}/tries",
    response_model=ChunkTries,
    summary="One chunk's recorded tries: node, outcome, timings, stderr tail",
)
async def split_chunk_tries(
    user: CurrentUser,
    jobs: Jobs,
    job_id: Annotated[str, Path(max_length=_JOB_ID_MAX)],
    chunk_index: Annotated[int, Path(ge=0, lt=MAX_SPLIT_CHUNKS)],
    limit: Annotated[
        int,
        Query(ge=1, le=MAX_TRY_LIMIT, description="The latest this many tries."),
    ] = DEFAULT_TRY_LIMIT,
) -> ChunkTries:
    """V0.10 G8 (D261/D271): the chunk records of one chunk - every try that
    ended with a result, plus the other leases of a committed chunk (a lost
    speculative copy, an expired try) - with each try's stderr tail. A chunk
    with no recorded try (not run yet, or still running) has none."""
    reads = _reads(jobs)
    job = await _split_job(user, jobs, job_id)
    rows = await reads.chunk_tries(job.id, chunk_index, limit=limit)
    tries = tuple(
        ChunkTryOut(
            lease_seq=int(r.lease_seq),
            attempt_id=r.lane_attempt_id,
            node_id=r.node_id,
            speculative=bool(r.speculative),
            won=bool(r.won),
            outcome=r.outcome,  # type: ignore[arg-type]  # CHECK-constrained
            failure_type=r.failure_type,
            failure_class=r.failure_class,
            exit_code=r.exit_code,
            result_digest=r.result_digest,
            bytes_in=r.bytes_in,
            bytes_out=r.bytes_out,
            stage_ms=r.stage_ms,
            env_ms=r.env_ms,
            run_ms=r.run_ms,
            upload_ms=r.upload_ms,
            leased_at=r.leased_at,
            ended_at=r.ended_at,
            stderr_tail=r.stderr_tail,
        )
        for r in rows
    )
    return ChunkTries(
        job_id=job.id, chunk_index=chunk_index, tries=tries, count=len(tries)
    )
