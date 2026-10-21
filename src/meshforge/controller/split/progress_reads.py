"""The session-owning reads of split progress (V0.10 G7, D270): the REST
routes' summary and chunk pages, and the tracker's job lists. Every call opens
its own session and reads the rows (:mod:`.progress`; chunk pages through
:func:`meshforge.workqueue.reads.list_items`) - nothing is cached here.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.states import JOB_TERMINAL_STATES
from meshforge.controller.jobs.store import typed_store_call
from meshforge.controller.split.lanes import QUEUE_KIND
from meshforge.controller.split.progress import (
    SplitProgress,
    payload_items,
    read_progress,
)
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.job import Job
from meshforge.controller.store.models.telemetry import ChunkRecord
from meshforge.workqueue.core import ItemState
from meshforge.workqueue.reads import ItemView, list_items
from meshforge.workqueue.tables import work_queue

__all__ = ["ChunkLease", "ChunkRow", "ProgressReads", "chunk_row"]


@dataclass(frozen=True, slots=True)
class ChunkLease:
    attempt_id: str
    node_id: str
    lease_seq: int
    speculative: bool
    leased_at: dt.datetime
    deadline: dt.datetime


@dataclass(frozen=True, slots=True)
class ChunkRow:
    chunk_index: int
    state: str
    first_item: int | None
    item_count: int
    attempts: int
    speculations: int
    node_id: str | None
    """Done: the node whose result was committed; leased: the original
    lease's node; else None."""
    attempt_id: str | None
    result_digest: str | None
    done_at: dt.datetime | None
    failed_kind: str | None
    failed_nodes: tuple[str, ...]
    leases: tuple[ChunkLease, ...]


def chunk_row(view: ItemView) -> ChunkRow:
    """One work item as the API's chunk (pure)."""
    payload = view.payload
    first = payload.get("first") if isinstance(payload, Mapping) else None
    holder = view.winner
    if holder is None and view.live:
        holder = next((le for le in view.live if not le.speculative), view.live[0])
    return ChunkRow(
        chunk_index=view.idx,
        state=view.state.value,
        first_item=(
            first if isinstance(first, int) and not isinstance(first, bool) else None
        ),
        item_count=payload_items(payload),
        attempts=view.attempts,
        speculations=view.speculations,
        node_id=None if holder is None else holder.site,
        attempt_id=None if holder is None else holder.holder,
        result_digest=view.result_digest,
        done_at=view.done_at,
        failed_kind=view.failed_kind,
        failed_nodes=view.failed_sites,
        leases=tuple(
            ChunkLease(
                attempt_id=le.holder,
                node_id=le.site,
                lease_seq=le.seq,
                speculative=le.speculative,
                leased_at=le.leased_at,
                deadline=le.deadline,
            )
            for le in view.live
        ),
    )


class ProgressReads:
    """The REST routes' reads (``GET /v1/jobs/{id}/split`` and ``/chunks``):
    always straight from the rows, never from the tracker's memory."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        rate_window: int = 8,
        wall_clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._sessions = create_session_factory(engine)
        self._window = rate_window
        self._wall = wall_clock or (lambda: dt.datetime.now(dt.UTC))

    @typed_store_call
    async def progress(
        self, job_id: str, *, with_map: bool = False
    ) -> SplitProgress | None:
        async with self._sessions() as db:
            return await read_progress(
                db,
                job_id,
                now=self._wall(),
                rate_window=self._window,
                with_map=with_map,
            )

    @typed_store_call
    async def running_split_jobs(self, *, limit: int) -> list[str]:
        """Split jobs not ended yet (the tracker's resync / restart rebuild)."""
        async with self._sessions() as db:
            return list(
                await db.scalars(
                    select(Job.id)
                    .where(
                        Job.split_items_digest.is_not(None),
                        Job.state.not_in([s.value for s in JOB_TERMINAL_STATES]),
                    )
                    .order_by(Job.id)
                    .limit(limit)
                )
            )

    @typed_store_call
    async def split_job_ids(self, job_ids: Collection[str]) -> set[str]:
        """Which of ``job_ids`` are split jobs."""
        if not job_ids:
            return set()
        async with self._sessions() as db:
            return set(
                await db.scalars(
                    select(Job.id).where(
                        Job.id.in_(list(job_ids)),
                        Job.split_items_digest.is_not(None),
                    )
                )
            )

    @typed_store_call
    async def chunk_page(
        self,
        job_id: str,
        *,
        limit: int,
        after_idx: int | None = None,
        states: Collection[ItemState] | None = None,
        node_id: str | None = None,
    ) -> list[ChunkRow] | None:
        """One page of the job's chunks in index order; None = the job has no
        chunk queue yet (not dispatched). The caller checked the job is a split
        job the user may see."""
        async with self._sessions() as db:
            # one snapshot: list_items reads items then leases (reviewer N2)
            await db.connection(
                execution_options={"isolation_level": "REPEATABLE READ"}
            )
            queue_id = await db.scalar(
                select(work_queue.c.id).where(
                    work_queue.c.kind == QUEUE_KIND, work_queue.c.owner_id == job_id
                )
            )
            if queue_id is None:
                return None
            views = await list_items(
                db,
                queue_id,
                limit=limit,
                after_idx=after_idx,
                states=states,
                site=node_id,
            )
            await db.rollback()
        return [chunk_row(v) for v in views]

    @typed_store_call
    async def chunk_tries(
        self, job_id: str, chunk_index: int, *, limit: int
    ) -> list[ChunkRecord]:
        """V0.10 G8 (D261/D271): the chunk's recorded tries (its chunk
        records, with their stderr tails), the latest ``limit`` of them in
        lease order. Empty = no try of it ended with a result yet."""
        async with self._sessions() as db:
            rows = list(
                await db.scalars(
                    select(ChunkRecord)
                    .where(
                        ChunkRecord.job_id == job_id,
                        ChunkRecord.chunk_index == chunk_index,
                    )
                    .order_by(ChunkRecord.lease_seq.desc())
                    .limit(limit)
                )
            )
        rows.reverse()
        return rows
