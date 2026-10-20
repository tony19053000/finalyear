"""Read-only views of a work queue's rows (V0.10 G7; D277 "paged ``list_items``").

Kept apart from :mod:`.store` (which owns every *write* and its lock protocol):
nothing here locks a row or changes one. Each function is **one bounded read**
on a caller-owned session; under PostgreSQL's default ``READ COMMITTED`` one
statement sees one committed snapshot, so a page is internally consistent (a
lease and its item never disagree inside one row). Two statements in one call
(:func:`list_items` reads the page, then the page's leases) may straddle a
commit; the caller that needs one snapshot for both opens a ``REPEATABLE READ``
transaction.

* :func:`list_items` - one page of items in ``idx`` order (keyset: ``after_idx``),
  optionally only some states and/or one ``site``; each item with its live
  leases and, when done, the lease whose result won (who committed it).

Like the rest of the package: no caller vocabulary (an item is an opaque
payload, a holder/site an opaque string).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.errors import FailureType
from meshforge.workqueue.core import MAX_NAME_CHARS, ItemState, LeaseState
from meshforge.workqueue.store import WorkQueueError
from meshforge.workqueue.tables import work_item, work_lease

__all__ = [
    "MAX_PAGE_ITEMS",
    "ItemView",
    "LeaseView",
    "list_items",
]

MAX_PAGE_ITEMS: Final = 1_000
"""Most items one :func:`list_items` call returns (bounded memory per call)."""


@dataclass(frozen=True, slots=True)
class LeaseView:
    seq: int
    holder: str
    site: str
    state: LeaseState
    speculative: bool
    leased_at: dt.datetime
    deadline: dt.datetime
    ended_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class ItemView:
    id: int
    idx: int
    payload: Any
    state: ItemState
    attempts: int
    speculations: int
    result_digest: str | None
    done_at: dt.datetime | None
    failed_kind: str | None
    failed_sites: tuple[str, ...]
    live: tuple[LeaseView, ...]
    """Live leases, ``seq`` order (at most two: original + speculative copy)."""
    winner: LeaseView | None
    """Done items: the lease whose result was committed (any lease state - a
    late result of an expired lease can win, ADR-0015)."""


def _check_page(limit: int, after_idx: int | None, site: str | None) -> None:
    if not 1 <= limit <= MAX_PAGE_ITEMS:
        raise WorkQueueError(
            f"limit must be 1..{MAX_PAGE_ITEMS}", failure_type=FailureType.INVALID_INPUT
        )
    if after_idx is not None and after_idx < -1:
        raise WorkQueueError(
            "after_idx must be >= -1", failure_type=FailureType.INVALID_INPUT
        )
    if site is not None and not 1 <= len(site) <= MAX_NAME_CHARS:
        raise WorkQueueError(
            f"site must be 1..{MAX_NAME_CHARS} characters",
            failure_type=FailureType.INVALID_INPUT,
        )


def _lease_view(row: Any) -> LeaseView:
    return LeaseView(
        seq=row.seq,
        holder=row.holder,
        site=row.site,
        state=LeaseState(row.state),
        speculative=row.speculative,
        leased_at=row.leased_at,
        deadline=row.deadline,
        ended_at=row.ended_at,
    )


async def list_items(
    db: AsyncSession,
    queue_id: int,
    *,
    limit: int,
    after_idx: int | None = None,
    states: Collection[ItemState] | None = None,
    site: str | None = None,
) -> list[ItemView]:
    """Items of ``queue_id`` with ``idx > after_idx``, ``idx`` order, at most
    ``limit`` (1..:data:`MAX_PAGE_ITEMS`). ``states`` keeps only those states
    (empty or None = all). ``site`` keeps items that ``site`` holds a live lease
    on or whose committed result came from ``site``. The next page starts after
    the last returned ``idx``."""
    _check_page(limit, after_idx, site)
    query = select(work_item).where(work_item.c.queue_id == queue_id)
    if after_idx is not None:
        query = query.where(work_item.c.idx > after_idx)
    if states:
        query = query.where(work_item.c.state.in_([s.value for s in states]))
    if site is not None:
        query = query.where(
            exists().where(
                work_lease.c.item_id == work_item.c.id,
                work_lease.c.site == site,
                or_(
                    work_lease.c.state == LeaseState.LIVE.value,
                    and_(
                        work_item.c.state == ItemState.DONE.value,
                        work_lease.c.seq == work_item.c.done_lease_seq,
                    ),
                ),
            )
        )
    rows = (await db.execute(query.order_by(work_item.c.idx).limit(limit))).all()
    if not rows:
        return []
    ids = [r.id for r in rows]
    winner_seq = {r.id: r.done_lease_seq for r in rows if r.done_lease_seq is not None}
    lease_rows = (
        await db.execute(
            select(work_lease)
            .where(
                work_lease.c.item_id.in_(ids),
                or_(
                    work_lease.c.state == LeaseState.LIVE.value,
                    and_(
                        work_lease.c.seq == work_item.c.done_lease_seq,
                        work_item.c.id == work_lease.c.item_id,
                    ),
                ),
            )
            .select_from(work_lease.join(work_item))
            .order_by(work_lease.c.item_id, work_lease.c.seq)
        )
    ).all()
    live: dict[int, list[LeaseView]] = {}
    winners: dict[int, LeaseView] = {}
    for lr in lease_rows:
        view = _lease_view(lr)
        if winner_seq.get(lr.item_id) == lr.seq:
            winners[lr.item_id] = view
        if view.state is LeaseState.LIVE:
            live.setdefault(lr.item_id, []).append(view)
    return [
        ItemView(
            id=r.id,
            idx=r.idx,
            payload=r.payload,
            state=ItemState(r.state),
            attempts=r.attempts,
            speculations=r.speculations,
            result_digest=r.result_digest,
            done_at=r.done_at,
            failed_kind=r.failed_kind,
            failed_sites=tuple(r.failed_sites),
            live=tuple(live.get(r.id, ())),
            winner=winners.get(r.id),
        )
        for r in rows
    ]
