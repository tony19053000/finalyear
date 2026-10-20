"""A split job's progress, read from its rows (V0.10 G7, D270; roadmap 0.10.8).

**Numbers are rows (checklist 37).** Every figure here is computed from the
database in one ``REPEATABLE READ`` snapshot - never counted along in memory
and never guessed: chunk counts by state are ``work_item`` rows, a lane's
committed chunks are the items whose winning lease (``done_lease_seq``) it
held, items are the chunks' payload sizes, current chunks are live
``work_lease`` rows. A restart therefore "rebuilds" nothing: the next read is
already right. :mod:`.progress_events` decides *when* to read and publish;
:mod:`.progress_reads` is the session-owning reader (REST routes, tracker).

What one read gives (:class:`SplitProgress`):

* the job's state, its queue's state, item / chunk totals, chunks pending /
  leased / done / failed, items done, live and speculative leases;
* per lane (``role = 'lane'`` attempts of the job, live first, at most
  :data:`MAX_LANES` - a job re-planned many times keeps its newest): node,
  attempt state, chunks and items committed, counted failed tries, the chunks
  it holds now, its measured rate and its last commit;
* rate and ETA: a lane's rate is the items of its last ``rate_window``
  committed chunks over the time between the first and the last of those
  commits (one commit: since its first lease); the ETA is the remaining items
  over the **live** lanes' summed rates - ``None`` until a live lane has
  committed (nothing measured = nothing claimed);
* elapsed (first ``running`` arrow to now or the job's end) and the predicted
  makespan (the rank-1 candidate of the job's first dispatched decision);
* the job's warnings (``nondeterministic_chunk``) by code and count;
* optionally a **chunk map**: one character per chunk in index order
  (:data:`MAP_CHARS`) for the dashboard's grid (at most 10 000 characters).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Final

from sqlalchemy import Integer, and_, case, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    JOB_TERMINAL_STATES,
    AttemptRole,
    AttemptState,
    JobState,
)
from meshforge.controller.split.lanes import QUEUE_KIND
from meshforge.controller.split.warnings import parse_warnings
from meshforge.controller.store.models.job import Attempt, Job, JobTransition
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.workqueue.core import ItemState, LeaseState
from meshforge.workqueue.tables import work_item, work_lease, work_queue

__all__ = [
    "MAP_CHARS",
    "MAX_LANES",
    "MAX_LIVE_LEASES",
    "ChunkCounts",
    "LaneProgress",
    "SplitProgress",
    "eta_seconds",
    "lane_rate",
    "payload_items",
    "read_progress",
]

MAX_LANES: Final = 64
"""Most lanes one progress read lists (live ones first, then the newest)."""
MAX_LIVE_LEASES: Final = 1_000
"""Most live leases one read looks at (a job holds about 2 per lane)."""
MAP_CHARS: Final = {
    ItemState.PENDING: "p",
    ItemState.LEASED: "l",
    ItemState.DONE: "d",
    ItemState.FAILED: "f",
}
"""Chunk map characters; a leased chunk with a speculative copy is ``s``."""
SPECULATIVE_CHAR: Final = "s"
_LIVE_ATTEMPT: Final = tuple(
    s.value for s in AttemptState if s not in ATTEMPT_TERMINAL_STATES
)


@dataclass(frozen=True, slots=True)
class ChunkCounts:
    total: int = 0
    pending: int = 0
    leased: int = 0
    done: int = 0
    failed: int = 0


@dataclass(frozen=True, slots=True)
class CurrentChunk:
    chunk_index: int
    lease_seq: int
    speculative: bool
    leased_at: dt.datetime


@dataclass(frozen=True, slots=True)
class LaneProgress:
    attempt_id: str
    node_id: str
    state: str
    live: bool
    chunks_done: int
    items_done: int
    failed_tries: int
    current: tuple[CurrentChunk, ...]
    rate_items_s: float | None
    started_at: dt.datetime | None
    """The attempt's ``started_at`` (its process ran), else None."""
    first_lease_at: dt.datetime | None
    last_commit_at: dt.datetime | None
    ended_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class SplitProgress:
    job_id: str
    owner_user_id: str | None
    job_state: JobState
    queue_state: str | None
    """``open`` / ``done`` / ``failed`` / ``cancelled``; None = not dispatched
    yet (no queue: no chunks exist)."""
    items_total: int
    chunk_items: int | None
    chunks: ChunkCounts
    items_done: int
    live_leases: int
    speculative_live: int
    speculated: int
    """Chunks that ever got a speculative copy (the queue's counter)."""
    lanes: tuple[LaneProgress, ...]
    lanes_total: int
    """Lane attempts the job ever had (``lanes`` may list fewer)."""
    rate_items_s: float | None
    eta_s: float | None
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    elapsed_s: float | None
    predicted_s: float | None
    combine_ms: int | None
    warnings: tuple[tuple[str, int], ...]
    computed_at: dt.datetime
    chunk_map: str | None = field(default=None)

    @property
    def terminal(self) -> bool:
        return self.job_state in JOB_TERMINAL_STATES

    def comparable(self) -> tuple[Any, ...]:
        """What makes two reads "the same progress" (time-only fields left
        out: an unchanged job publishes no new event)."""
        return (
            self.job_state,
            self.queue_state,
            self.chunks,
            self.items_done,
            self.live_leases,
            self.speculative_live,
            self.speculated,
            self.lanes,
            self.lanes_total,
            self.eta_s,
            self.ended_at,
            self.combine_ms,
            self.warnings,
        )

    def to_data(self) -> dict[str, Any]:
        """JSON-ready (the event's ``data`` and the REST body share it)."""
        out = {
            "job_id": self.job_id,
            "job_state": self.job_state.value,
            "queue_state": self.queue_state,
            "items_total": self.items_total,
            "chunk_items": self.chunk_items,
            "chunks": asdict(self.chunks),
            "items_done": self.items_done,
            "live_leases": self.live_leases,
            "speculative_live": self.speculative_live,
            "speculated": self.speculated,
            "lanes": [_lane_data(lane) for lane in self.lanes],
            "lanes_total": self.lanes_total,
            "rate_items_s": self.rate_items_s,
            "eta_s": self.eta_s,
            "started_at": _iso(self.started_at),
            "ended_at": _iso(self.ended_at),
            "elapsed_s": self.elapsed_s,
            "predicted_s": self.predicted_s,
            "combine_ms": self.combine_ms,
            "warnings": [{"code": c, "count": n} for c, n in self.warnings],
            "computed_at": _iso(self.computed_at),
        }
        if self.chunk_map is not None:
            out["chunk_map"] = self.chunk_map
        return out


def _iso(value: dt.datetime | None) -> str | None:
    return None if value is None else value.astimezone(dt.UTC).isoformat()


def _lane_data(lane: LaneProgress) -> dict[str, Any]:
    return {
        "attempt_id": lane.attempt_id,
        "node_id": lane.node_id,
        "state": lane.state,
        "live": lane.live,
        "chunks_done": lane.chunks_done,
        "items_done": lane.items_done,
        "failed_tries": lane.failed_tries,
        "current": [
            {
                "chunk_index": c.chunk_index,
                "lease_seq": c.lease_seq,
                "speculative": c.speculative,
                "leased_at": _iso(c.leased_at),
            }
            for c in lane.current
        ],
        "rate_items_s": lane.rate_items_s,
        "started_at": _iso(lane.started_at),
        "first_lease_at": _iso(lane.first_lease_at),
        "last_commit_at": _iso(lane.last_commit_at),
        "ended_at": _iso(lane.ended_at),
    }


# ------------------------------------------------------------------ pure


def lane_rate(
    commits: Sequence[tuple[dt.datetime, int]], *, first_lease_at: dt.datetime | None
) -> float | None:
    """Items/s of a lane from its latest commits ``(done_at, items)`` (any
    order): with two or more, the items of all but the earliest over the time
    from the earliest to the latest commit (steady state, staging excluded);
    with one, its items over the time since the lane's first lease. ``None``
    when nothing is measurable (no commit, zero time)."""
    if not commits:
        return None
    ordered = sorted(commits, key=lambda c: c[0])
    if len(ordered) >= 2:
        span = (ordered[-1][0] - ordered[0][0]).total_seconds()
        if span > 0:
            return round(sum(n for _, n in ordered[1:]) / span, 6)
    if first_lease_at is None:
        return None
    span = (ordered[-1][0] - first_lease_at).total_seconds()
    if span <= 0:
        return None
    return round(sum(n for _, n in ordered) / span, 6)


def eta_seconds(
    *, remaining_items: int, live_rates: Sequence[float | None], queue_open: bool
) -> float | None:
    """Remaining items over the live lanes' summed measured rates; 0 when
    nothing remains; None when the queue is closed otherwise or no live lane
    has a rate (an ETA is a measurement, never an assumption)."""
    if remaining_items <= 0:
        return 0.0
    if not queue_open:
        return None
    total = sum(r for r in live_rates if r is not None and r > 0)
    if total <= 0:
        return None
    return round(remaining_items / total, 3)


def payload_items(payload: Any) -> int:
    """A chunk payload's item count (``{"first", "count"}``; 0 if odd)."""
    if isinstance(payload, Mapping):
        count = payload.get("count")
        if isinstance(count, int) and not isinstance(count, bool) and count > 0:
            return count
    return 0


def _predicted(candidates: Any) -> float | None:
    if not isinstance(candidates, list):
        return None
    for cand in candidates:
        if isinstance(cand, Mapping) and cand.get("rank") == 1:
            estimate = cand.get("estimate")
            total = estimate.get("total_s") if isinstance(estimate, Mapping) else None
            if isinstance(total, int | float) and not isinstance(total, bool):
                return round(float(total), 3)
    return None


# ------------------------------------------------------------------ reads

_ITEMS = cast(work_item.c.payload["count"].astext, Integer)
_WON = and_(
    work_item.c.state == ItemState.DONE.value,
    work_lease.c.seq == work_item.c.done_lease_seq,
)


async def read_progress(
    db: AsyncSession,
    job_id: str,
    *,
    now: dt.datetime,
    rate_window: int = 8,
    with_map: bool = False,
) -> SplitProgress | None:
    """The progress of split job ``job_id`` from its rows, or None when there
    is no such job or it is not a split job. Runs every read in one
    ``REPEATABLE READ`` snapshot and rolls back (read only); the caller passes
    a fresh session."""
    await db.connection(execution_options={"isolation_level": "REPEATABLE READ"})
    try:
        return await _read(
            db, job_id, now=now, rate_window=rate_window, with_map=with_map
        )
    finally:
        await db.rollback()


async def _read(
    db: AsyncSession,
    job_id: str,
    *,
    now: dt.datetime,
    rate_window: int,
    with_map: bool,
) -> SplitProgress | None:
    job = (
        await db.execute(
            select(
                Job.id,
                Job.state,
                Job.owner_user_id,
                Job.split,
                Job.split_item_count,
                Job.split_combine_ms,
                Job.warnings,
                Job.updated_at,
            ).where(Job.id == job_id)
        )
    ).one_or_none()
    if job is None or job.split is None:
        return None
    job_state = JobState(job.state)
    times = (
        await db.execute(
            select(
                func.min(JobTransition.at)
                .filter(JobTransition.to_state == JobState.RUNNING.value)
                .label("started"),
                func.max(JobTransition.at)
                .filter(
                    JobTransition.to_state.in_([s.value for s in JOB_TERMINAL_STATES])
                )
                .label("ended"),
            ).where(JobTransition.job_id == job_id)
        )
    ).one()
    started, ended = (
        times.started,
        (times.ended if job_state in JOB_TERMINAL_STATES else None),
    )
    elapsed = None
    if started is not None:
        elapsed = round(max(0.0, ((ended or now) - started).total_seconds()), 3)
    decision = await db.scalar(
        select(PlanDecision.candidates)
        .where(PlanDecision.job_id == job_id, PlanDecision.outcome == "dispatched")
        .order_by(PlanDecision.id)
        .limit(1)
    )
    warnings = tuple(
        (str(w["code"]), int(w["count"])) for w in parse_warnings(job.warnings)
    )
    queue = (
        await db.execute(
            select(work_queue).where(
                work_queue.c.kind == QUEUE_KIND, work_queue.c.owner_id == job_id
            )
        )
    ).one_or_none()
    lanes, lanes_total = await _lane_rows(db, job_id)
    base = {
        "job_id": job_id,
        "owner_user_id": job.owner_user_id,
        "job_state": job_state,
        "items_total": int(job.split_item_count or 0),
        "started_at": started,
        "ended_at": ended,
        "elapsed_s": elapsed,
        "predicted_s": _predicted(decision),
        "combine_ms": job.split_combine_ms,
        "warnings": warnings,
        "computed_at": now,
        "lanes_total": lanes_total,
    }
    if queue is None:
        return SplitProgress(
            **base,
            queue_state=None,
            chunk_items=None,
            chunks=ChunkCounts(),
            items_done=0,
            live_leases=0,
            speculative_live=0,
            speculated=0,
            lanes=tuple(_lane(a, None, ()) for a in lanes),
            rate_items_s=None,
            eta_s=None,
            chunk_map="" if with_map else None,
        )
    counts, items_done, chunk_items = await _counts(db, queue.id)
    per_lane = await _per_holder(db, queue.id)
    recent = await _recent_commits(db, queue.id, rate_window)
    live = await _live_leases(db, queue.id)
    out_lanes = tuple(
        _lane(a, per_lane.get(a.id), recent.get(a.id, ()), live.get(a.id, ()))
        for a in lanes
    )
    live_rates = [lane.rate_items_s for lane in out_lanes if lane.live]
    measured = [r for r in live_rates if r is not None]
    queue_open = queue.state == "open"
    chunk_map = None
    if with_map:
        chunk_map = await _chunk_map(db, queue.id, live)
    return SplitProgress(
        **base,
        queue_state=str(queue.state),
        chunk_items=chunk_items,
        chunks=counts,
        items_done=items_done,
        live_leases=sum(len(v) for v in live.values()),
        speculative_live=sum(1 for v in live.values() for c in v if c.speculative),
        speculated=int(queue.speculated_count),
        lanes=out_lanes,
        rate_items_s=round(sum(measured), 6) if measured else None,
        eta_s=eta_seconds(
            remaining_items=base["items_total"] - items_done,
            live_rates=live_rates,
            queue_open=queue_open,
        ),
        chunk_map=chunk_map,
    )


async def _lane_rows(db: AsyncSession, job_id: str) -> tuple[list[Any], int]:
    is_live = case((Attempt.state.in_(_LIVE_ATTEMPT), 0), else_=1)
    rows = (
        await db.execute(
            select(
                Attempt.id,
                Attempt.node_id,
                Attempt.state,
                Attempt.started_at,
                Attempt.ended_at,
                Attempt.fencing_token,
                func.count().over().label("total"),
            )
            .where(Attempt.job_id == job_id, Attempt.role == AttemptRole.LANE.value)
            .order_by(is_live, Attempt.fencing_token.desc())
            .limit(MAX_LANES)
        )
    ).all()
    total = int(rows[0].total) if rows else 0
    # shown in token order (the order the planner created them)
    return sorted(rows, key=lambda r: int(r.fencing_token)), total


async def _counts(
    db: AsyncSession, queue_id: int
) -> tuple[ChunkCounts, int, int | None]:
    rows = (
        await db.execute(
            select(
                work_item.c.state,
                func.count().label("n"),
                func.coalesce(func.sum(_ITEMS), 0).label("items"),
                func.max(_ITEMS).label("biggest"),
            )
            .where(work_item.c.queue_id == queue_id)
            .group_by(work_item.c.state)
        )
    ).all()
    by = {r.state: r for r in rows}

    def n(state: ItemState) -> int:
        row = by.get(state.value)
        return 0 if row is None else int(row.n)

    done = by.get(ItemState.DONE.value)
    biggest = max((int(r.biggest) for r in rows if r.biggest is not None), default=0)
    return (
        ChunkCounts(
            total=sum(int(r.n) for r in rows),
            pending=n(ItemState.PENDING),
            leased=n(ItemState.LEASED),
            done=n(ItemState.DONE),
            failed=n(ItemState.FAILED),
        ),
        0 if done is None else int(done.items),
        biggest or None,
    )


async def _per_holder(db: AsyncSession, queue_id: int) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(
                work_lease.c.holder,
                func.count().filter(_WON).label("chunks_done"),
                func.coalesce(func.sum(_ITEMS).filter(_WON), 0).label("items_done"),
                func.count()
                .filter(work_lease.c.state == LeaseState.FAILED.value)
                .label("failed"),
                func.min(work_lease.c.leased_at).label("first"),
                func.max(work_item.c.done_at).filter(_WON).label("last_commit"),
            )
            .select_from(work_lease.join(work_item))
            .where(work_item.c.queue_id == queue_id)
            .group_by(work_lease.c.holder)
        )
    ).all()
    return {r.holder: r for r in rows}


async def _recent_commits(
    db: AsyncSession, queue_id: int, window: int
) -> dict[str, list[tuple[dt.datetime, int]]]:
    ranked = (
        select(
            work_lease.c.holder,
            work_item.c.done_at,
            _ITEMS.label("n_items"),
            func.row_number()
            .over(
                partition_by=work_lease.c.holder,
                order_by=(work_item.c.done_at.desc(), work_item.c.idx.desc()),
            )
            .label("rn"),
        )
        .select_from(work_lease.join(work_item))
        .where(work_item.c.queue_id == queue_id, _WON)
        .subquery()
    )
    rows = (
        await db.execute(
            select(ranked.c.holder, ranked.c.done_at, ranked.c.n_items).where(
                ranked.c.rn <= window
            )
        )
    ).all()
    out: dict[str, list[tuple[dt.datetime, int]]] = {}
    for r in rows:
        if r.done_at is not None:
            out.setdefault(r.holder, []).append((r.done_at, int(r.n_items or 0)))
    return out


async def _live_leases(
    db: AsyncSession, queue_id: int
) -> dict[str, list[CurrentChunk]]:
    rows = (
        await db.execute(
            select(
                work_lease.c.holder,
                work_item.c.idx,
                work_lease.c.seq,
                work_lease.c.speculative,
                work_lease.c.leased_at,
            )
            .select_from(work_lease.join(work_item))
            .where(
                work_item.c.queue_id == queue_id,
                work_lease.c.state == LeaseState.LIVE.value,
            )
            .order_by(work_item.c.idx, work_lease.c.seq)
            .limit(MAX_LIVE_LEASES)
        )
    ).all()
    out: dict[str, list[CurrentChunk]] = {}
    for r in rows:
        out.setdefault(r.holder, []).append(
            CurrentChunk(int(r.idx), int(r.seq), bool(r.speculative), r.leased_at)
        )
    return out


async def _chunk_map(
    db: AsyncSession, queue_id: int, live: Mapping[str, Sequence[CurrentChunk]]
) -> str:
    rows = (
        await db.execute(
            select(work_item.c.idx, work_item.c.state)
            .where(work_item.c.queue_id == queue_id)
            .order_by(work_item.c.idx)
        )
    ).all()
    copied = {c.chunk_index for v in live.values() for c in v if c.speculative}
    chars = []
    for r in rows:
        state = ItemState(r.state)
        if state is ItemState.LEASED and int(r.idx) in copied:
            chars.append(SPECULATIVE_CHAR)
        else:
            chars.append(MAP_CHARS[state])
    return "".join(chars)


def _lane(
    attempt: Any,
    totals: Any | None,
    recent: Sequence[tuple[dt.datetime, int]],
    current: Sequence[CurrentChunk] = (),
) -> LaneProgress:
    first = None if totals is None else totals.first
    return LaneProgress(
        attempt_id=attempt.id,
        node_id=attempt.node_id,
        state=attempt.state,
        live=attempt.state in _LIVE_ATTEMPT,
        chunks_done=0 if totals is None else int(totals.chunks_done),
        items_done=0 if totals is None else int(totals.items_done),
        failed_tries=0 if totals is None else int(totals.failed),
        current=tuple(current),
        rate_items_s=lane_rate(recent, first_lease_at=first),
        started_at=attempt.started_at,
        first_lease_at=first,
        last_commit_at=None if totals is None else totals.last_commit,
        ended_at=attempt.ended_at,
    )
