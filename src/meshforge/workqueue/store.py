"""PostgreSQL operations of the work queue (V0.10 D262; tables in :mod:`.tables`).

Every function takes an :class:`~sqlalchemy.ext.asyncio.AsyncSession` inside a
transaction the **caller** owns and commits: the loss of a worker and the
release of its leases, or an item's commit and the caller's own bookkeeping
rows, are one transaction (V0.10 checklist 35/36). Nothing here commits, rolls back or
retries; database errors propagate unchanged to the caller's transaction
handling. Only bad arguments raise :class:`WorkQueueError`.

The rules are the pure functions of :mod:`.core`: an operation locks the rows,
rebuilds :class:`~.core.Item` values, applies the core transition and writes
back what changed. Times (``now``) come from the caller - one controller clock -
and must be timezone-aware.

Locking (deadlock freedom)
--------------------------
Order is always **queue row, then item rows in ascending id**:

* ``lease_next`` takes ``FOR KEY SHARE`` on the queue and picks items with
  ``FOR UPDATE SKIP LOCKED ORDER BY idx`` - concurrent pullers never wait for
  each other and never get the same item.
* ``renew`` and ``release`` (one item each) take ``FOR KEY SHARE`` on the
  queue, then the one item.
* Everything that changes queue counters or locks several items -
  ``complete``, ``fail``, ``speculate``, ``expire_due``, ``release_holder`` -
  takes ``FOR NO KEY UPDATE`` on the queue (several queues: ascending id), so
  multi-item lockers of one queue are serialised and single-item lockers never
  hold more than one item. ``cancel`` takes ``FOR UPDATE`` (it also waits for
  pullers).

Completions of one queue therefore serialise on the queue row for the length of
the caller's transaction; that is what lets the last commit see
``done_count = item_count`` in its own transaction (so a caller can start its
follow-up work atomically with the last commit). Keep those transactions short.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Row, and_, case, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger
from meshforge.workqueue import core
from meshforge.workqueue.core import (
    AlreadyDone,
    Committed,
    CompleteOutcome,
    Expired,
    FailOutcome,
    Fenced,
    FenceReason,
    Item,
    ItemFailed,
    ItemState,
    Lease,
    LeaseState,
    QueuePolicy,
    QueueState,
    Released,
    RenewOutcome,
    SpeculationRefusal,
)
from meshforge.workqueue.tables import (
    MAX_KIND_CHARS,
    MAX_OWNER_CHARS,
    work_item,
    work_lease,
    work_queue,
)

__all__ = [
    "MAX_LEASES_PER_PULL",
    "MAX_QUEUE_ITEMS",
    "CompleteResult",
    "ExpireResult",
    "FailResult",
    "Grant",
    "QueueCounts",
    "QueueInfo",
    "WorkQueueError",
    "cancel",
    "complete",
    "counts",
    "create_queue",
    "expire_due",
    "fail",
    "find_queue",
    "get_queue",
    "lease_next",
    "release",
    "release_holder",
    "renew",
    "speculate",
    "speculation_candidates",
]

log = get_logger(__name__)

MAX_QUEUE_ITEMS: Final = 100_000
MAX_LEASES_PER_PULL: Final = 64
_ROWS_PER_INSERT: Final = 5_000


class WorkQueueError(MeshForgeError):
    """Bad arguments (``invalid_input``), unknown queue (``not_found``) or a
    duplicate owner (``conflict``). Database errors are not wrapped."""

    default_failure_type = FailureType.INVALID_INPUT


# --- records ----------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QueueInfo:
    id: int
    kind: str
    owner_id: str
    state: QueueState
    policy: QueuePolicy
    item_count: int
    done_count: int
    speculated_count: int
    created_at: dt.datetime
    closed_at: dt.datetime | None


@dataclass(frozen=True, slots=True)
class Grant:
    """One lease handed to a puller: what to tell the worker."""

    queue_id: int
    item_id: int
    idx: int
    seq: int
    lease_id: int
    payload: Any
    holder: str
    site: str
    leased_at: dt.datetime
    deadline: dt.datetime
    speculative: bool


@dataclass(frozen=True, slots=True)
class QueueCounts:
    total: int
    pending: int
    leased: int
    done: int
    failed: int
    live_leases: int
    speculative_live: int


@dataclass(frozen=True, slots=True)
class CompleteResult:
    outcome: CompleteOutcome
    queue_id: int | None
    queue_done: bool
    """This commit was the queue's last item (it is ``done`` now)."""


@dataclass(frozen=True, slots=True)
class FailResult:
    outcome: FailOutcome
    queue_id: int | None
    queue_failed: bool
    released: tuple[Lease, ...]
    """Live leases of *other* items released because the queue failed."""


@dataclass(frozen=True, slots=True)
class ExpireResult:
    expired: tuple[Expired, ...]
    failed_queues: tuple[int, ...]
    released: tuple[Lease, ...]
    """Live leases released because their queue failed (``count_expiry``)."""


# --- row mapping ------------------------------------------------------------------

_ITEM_COLUMNS: Final = (
    work_item.c.id,
    work_item.c.queue_id,
    work_item.c.idx,
    work_item.c.state,
    work_item.c.attempts,
    work_item.c.lease_seq,
    work_item.c.speculations,
    work_item.c.result_digest,
    work_item.c.done_lease_seq,
    work_item.c.failed_kind,
    work_item.c.failed_sites,
)


def _aware(now: dt.datetime) -> dt.datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise WorkQueueError("now must be timezone-aware")
    return now


def _policy(row: Row[Any]) -> QueuePolicy:
    return QueuePolicy(
        visibility_timeout_s=row.visibility_timeout_s,
        max_attempts=row.max_attempts,
        max_failed_sites=row.max_failed_sites,
        count_expiry=row.count_expiry,
        speculative_max_frac=row.speculative_max_frac,
    )


def _queue_info(row: Row[Any]) -> QueueInfo:
    return QueueInfo(
        id=row.id,
        kind=row.kind,
        owner_id=row.owner_id,
        state=QueueState(row.state),
        policy=_policy(row),
        item_count=row.item_count,
        done_count=row.done_count,
        speculated_count=row.speculated_count,
        created_at=row.created_at,
        closed_at=row.closed_at,
    )


def _lease_from(row: Row[Any]) -> Lease:
    return Lease(
        item_id=row.item_id,
        seq=row.seq,
        holder=row.holder,
        site=row.site,
        leased_at=row.leased_at,
        deadline=row.deadline,
        state=LeaseState(row.state),
        speculative=row.speculative,
    )


def _item_from(row: Row[Any], leases: Iterable[Lease]) -> Item:
    return Item(
        id=row.id,
        idx=row.idx,
        state=ItemState(row.state),
        attempts=row.attempts,
        lease_seq=row.lease_seq,
        speculations=row.speculations,
        result_digest=row.result_digest,
        done_lease_seq=row.done_lease_seq,
        failed_kind=row.failed_kind,
        failed_sites=tuple(row.failed_sites),
        leases=tuple(sorted(leases, key=lambda x: x.seq)),
    )


# --- locking + loading ------------------------------------------------------------


async def _lock_queue(db: AsyncSession, queue_id: int, mode: str) -> Row[Any] | None:
    stmt = select(work_queue).where(work_queue.c.id == queue_id)
    if mode == "key_share":
        stmt = stmt.with_for_update(read=True, key_share=True)
    elif mode == "no_key_update":
        stmt = stmt.with_for_update(key_share=True)
    else:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).one_or_none()


async def _queue_of(db: AsyncSession, item_id: int) -> int | None:
    found = await db.scalar(
        select(work_item.c.queue_id).where(work_item.c.id == item_id)
    )
    return None if found is None else int(found)


async def _load_items(
    db: AsyncSession,
    item_ids: Sequence[int],
    *,
    seqs: Iterable[tuple[int, int]] = (),
) -> dict[int, tuple[Row[Any], Item]]:
    """Lock ``item_ids`` (ascending) and load each with its live leases plus the
    leases named in ``seqs`` (``(item_id, seq)``)."""
    if not item_ids:
        return {}
    rows = (
        await db.execute(
            select(*_ITEM_COLUMNS)
            .where(work_item.c.id.in_(sorted(set(item_ids))))
            .order_by(work_item.c.id)
            .with_for_update()
        )
    ).all()
    named = [and_(work_lease.c.item_id == i, work_lease.c.seq == s) for i, s in seqs]
    live = work_lease.c.state == LeaseState.LIVE.value
    lease_rows = (
        await db.execute(
            select(work_lease).where(
                work_lease.c.item_id.in_([r.id for r in rows]),
                or_(live, *named) if named else live,
            )
        )
    ).all()
    by_item: dict[int, list[Lease]] = {}
    for lr in lease_rows:
        by_item.setdefault(lr.item_id, []).append(_lease_from(lr))
    return {r.id: (r, _item_from(r, by_item.get(r.id, ()))) for r in rows}


async def _save(
    db: AsyncSession, old: Item, new: Item, *, now: dt.datetime
) -> dict[int, int]:
    """Write ``new`` over ``old``; returns ``{seq: lease_id}`` of inserted leases."""
    if old is new:
        return {}
    values: dict[str, Any] = {}
    for name in (
        "state",
        "attempts",
        "lease_seq",
        "speculations",
        "result_digest",
        "done_lease_seq",
        "failed_kind",
        "failed_sites",
    ):
        before, after = getattr(old, name), getattr(new, name)
        if before != after:
            values[name] = after.value if isinstance(after, ItemState) else after
    if "failed_sites" in values:
        values["failed_sites"] = list(new.failed_sites)
    if new.state is ItemState.DONE and old.state is not ItemState.DONE:
        values["done_at"] = now
    if values:
        await db.execute(
            update(work_item).where(work_item.c.id == new.id).values(**values)
        )
    inserted: dict[int, int] = {}
    for lease in new.leases:
        before_lease = old.lease(lease.seq)
        if before_lease is None:
            lease_id = await db.scalar(
                insert(work_lease)
                .values(
                    item_id=lease.item_id,
                    seq=lease.seq,
                    holder=lease.holder,
                    site=lease.site,
                    state=lease.state.value,
                    speculative=lease.speculative,
                    leased_at=lease.leased_at,
                    deadline=lease.deadline,
                )
                .returning(work_lease.c.id)
            )
            inserted[lease.seq] = int(lease_id)  # type: ignore[arg-type]
        elif before_lease != lease:
            changes: dict[str, Any] = {
                "state": lease.state.value,
                "deadline": lease.deadline,
            }
            if before_lease.live and not lease.live:
                changes["ended_at"] = now
            await db.execute(
                update(work_lease)
                .where(
                    work_lease.c.item_id == lease.item_id,
                    work_lease.c.seq == lease.seq,
                )
                .values(**changes)
            )
    return inserted


async def _close(
    db: AsyncSession, queue_id: int, state: QueueState, *, now: dt.datetime
) -> tuple[Lease, ...]:
    """Close an open queue (row already locked) and release every live lease."""
    await db.execute(
        update(work_queue)
        .where(work_queue.c.id == queue_id)
        .values(state=state.value, closed_at=now)
    )
    held = (
        await db.scalars(
            select(work_lease.c.item_id)
            .join(work_item, work_item.c.id == work_lease.c.item_id)
            .where(
                work_item.c.queue_id == queue_id,
                work_lease.c.state == LeaseState.LIVE.value,
            )
            .distinct()
        )
    ).all()
    released: list[Lease] = []
    for _row, item in (await _load_items(db, held)).values():
        new, out = core.release_all(item)
        if out is not None:
            await _save(db, item, new, now=now)
            released.extend(out.leases)
    log.info(
        "workqueue.closed",
        queue_id=queue_id,
        state=state.value,
        released=len(released),
    )
    return tuple(released)


def _log_fenced(outcome: object, *, queue_id: int | None, op: str) -> None:
    if isinstance(outcome, Fenced):
        log.info(
            "workqueue.fenced",
            op=op,
            queue_id=queue_id,
            item_id=outcome.item_id,
            seq=outcome.seq,
            reason=outcome.reason.value,
        )


# --- queues -----------------------------------------------------------------------


def _check_owner(kind: str, owner_id: str) -> None:
    if not kind or len(kind) > MAX_KIND_CHARS:
        raise WorkQueueError(f"kind must be 1..{MAX_KIND_CHARS} characters")
    if not owner_id or len(owner_id) > MAX_OWNER_CHARS:
        raise WorkQueueError(f"owner_id must be 1..{MAX_OWNER_CHARS} characters")


async def create_queue(
    db: AsyncSession,
    *,
    kind: str,
    owner_id: str,
    payloads: Sequence[Any],
    policy: QueuePolicy,
) -> QueueInfo:
    """Create the queue of ``(kind, owner_id)`` with one item per payload
    (``idx`` = position). Raises ``conflict`` if the owner already has one."""
    _check_owner(kind, owner_id)
    if not 1 <= len(payloads) <= MAX_QUEUE_ITEMS:
        raise WorkQueueError(f"a queue holds 1..{MAX_QUEUE_ITEMS} items")
    existing = await db.scalar(
        select(work_queue.c.id).where(
            work_queue.c.kind == kind, work_queue.c.owner_id == owner_id
        )
    )
    if existing is not None:
        raise WorkQueueError(
            "the owner already has a queue",
            failure_type=FailureType.CONFLICT,
            details={"kind": kind, "owner_id": owner_id, "queue_id": existing},
        )
    row = (
        await db.execute(
            insert(work_queue)
            .values(
                kind=kind,
                owner_id=owner_id,
                state=QueueState.OPEN.value,
                visibility_timeout_s=policy.visibility_timeout_s,
                max_attempts=policy.max_attempts,
                max_failed_sites=policy.max_failed_sites,
                count_expiry=policy.count_expiry,
                speculative_max_frac=policy.speculative_max_frac,
                item_count=len(payloads),
            )
            .returning(work_queue)
        )
    ).one()
    for start in range(0, len(payloads), _ROWS_PER_INSERT):
        await db.execute(
            insert(work_item),
            [
                {
                    "queue_id": row.id,
                    "idx": start + offset,
                    "payload": payload,
                    "state": ItemState.PENDING.value,
                }
                for offset, payload in enumerate(
                    payloads[start : start + _ROWS_PER_INSERT]
                )
            ],
        )
    log.info(
        "workqueue.created",
        queue_id=row.id,
        kind=kind,
        owner_id=owner_id,
        items=len(payloads),
    )
    return _queue_info(row)


async def get_queue(db: AsyncSession, queue_id: int) -> QueueInfo | None:
    row = (
        await db.execute(select(work_queue).where(work_queue.c.id == queue_id))
    ).one_or_none()
    return None if row is None else _queue_info(row)


async def find_queue(db: AsyncSession, *, kind: str, owner_id: str) -> QueueInfo | None:
    row = (
        await db.execute(
            select(work_queue).where(
                work_queue.c.kind == kind, work_queue.c.owner_id == owner_id
            )
        )
    ).one_or_none()
    return None if row is None else _queue_info(row)


async def counts(db: AsyncSession, queue_id: int) -> QueueCounts:
    """Item counts by state and live-lease counts, read from the rows."""
    by_state = {
        r.state: r.n
        for r in (
            await db.execute(
                select(work_item.c.state, func.count().label("n"))
                .where(work_item.c.queue_id == queue_id)
                .group_by(work_item.c.state)
            )
        ).all()
    }
    live = (
        await db.execute(
            select(
                func.count().label("n"),
                func.count().filter(work_lease.c.speculative).label("spec"),
            )
            .select_from(work_lease.join(work_item))
            .where(
                work_item.c.queue_id == queue_id,
                work_lease.c.state == LeaseState.LIVE.value,
            )
        )
    ).one()
    return QueueCounts(
        total=sum(by_state.values()),
        pending=by_state.get(ItemState.PENDING.value, 0),
        leased=by_state.get(ItemState.LEASED.value, 0),
        done=by_state.get(ItemState.DONE.value, 0),
        failed=by_state.get(ItemState.FAILED.value, 0),
        live_leases=live.n,
        speculative_live=live.spec,
    )


async def cancel(
    db: AsyncSession, queue_id: int, *, now: dt.datetime
) -> tuple[Lease, ...]:
    """Close an open queue as ``cancelled``; returns the released live leases
    (tell their holders to stop). Idempotent: a closed queue returns ``()``."""
    _aware(now)
    queue = await _lock_queue(db, queue_id, "update")
    if queue is None:
        raise WorkQueueError("no such queue", failure_type=FailureType.NOT_FOUND)
    if queue.state != QueueState.OPEN.value:
        return ()
    return await _close(db, queue_id, QueueState.CANCELLED, now=now)


# --- leasing ----------------------------------------------------------------------


async def lease_next(
    db: AsyncSession,
    queue_id: int,
    *,
    holder: str,
    site: str | None = None,
    k: int = 1,
    now: dt.datetime,
    timeout_s: float | None = None,
) -> list[Grant]:
    """Lease up to ``k`` pending items in ``idx`` order (items that have not
    failed at ``site`` first). ``[]`` when nothing is pending or the queue is
    closed. ``site`` defaults to ``holder``."""
    _aware(now)
    site = holder if site is None else site
    if not 1 <= k <= MAX_LEASES_PER_PULL:
        raise WorkQueueError(f"k must be in [1, {MAX_LEASES_PER_PULL}]")
    queue = await _lock_queue(db, queue_id, "key_share")
    if queue is None:
        raise WorkQueueError("no such queue", failure_type=FailureType.NOT_FOUND)
    if queue.state != QueueState.OPEN.value:
        return []
    policy = _policy(queue)
    pending = and_(
        work_item.c.queue_id == queue_id,
        work_item.c.state == ItemState.PENDING.value,
    )
    picked: list[Row[Any]] = []
    for failed_here in (False, True):
        want = k - len(picked)
        if want <= 0:
            break
        here = work_item.c.failed_sites.any(site)
        rows = (
            await db.execute(
                select(*_ITEM_COLUMNS, work_item.c.payload)
                .where(pending, here if failed_here else ~here)
                .order_by(work_item.c.idx)
                .limit(want)
                .with_for_update(skip_locked=True)
            )
        ).all()
        picked.extend(rows)
    grants: list[Grant] = []
    for row in picked:
        item = _item_from(row, ())
        new, granted = core.lease(
            item, holder=holder, site=site, now=now, policy=policy, timeout_s=timeout_s
        )
        ids = await _save(db, item, new, now=now)
        grants.append(_grant(queue_id, item, granted, ids[granted.seq], row.payload))
    return grants


def _grant(
    queue_id: int, item: Item, lease: Lease, lease_id: int, payload: Any
) -> Grant:
    return Grant(
        queue_id=queue_id,
        item_id=item.id,
        idx=item.idx,
        seq=lease.seq,
        lease_id=lease_id,
        payload=payload,
        holder=lease.holder,
        site=lease.site,
        leased_at=lease.leased_at,
        deadline=lease.deadline,
        speculative=lease.speculative,
    )


async def renew(
    db: AsyncSession,
    *,
    holder: str,
    leases: Sequence[tuple[int, int]],
    now: dt.datetime,
    timeout_s: float | None = None,
) -> list[RenewOutcome]:
    """Extend ``holder``'s live leases ``(item_id, seq)`` to ``now + timeout``.
    Outcomes are in the order of ``leases``."""
    _aware(now)
    results: dict[tuple[int, int], RenewOutcome] = {}
    for item_id, seq in sorted(set(leases)):
        queue_id = await _queue_of(db, item_id)
        queue = (
            None if queue_id is None else await _lock_queue(db, queue_id, "key_share")
        )
        if queue_id is None or queue is None:
            results[(item_id, seq)] = Fenced(item_id, seq, FenceReason.UNKNOWN_LEASE)
            continue
        if queue.state != QueueState.OPEN.value:
            results[(item_id, seq)] = Fenced(item_id, seq, FenceReason.QUEUE_CLOSED)
            continue
        _row, item = (await _load_items(db, [item_id], seqs=[(item_id, seq)]))[item_id]
        new, outcome = core.renew(
            item,
            seq=seq,
            holder=holder,
            now=now,
            policy=_policy(queue),
            timeout_s=timeout_s,
        )
        await _save(db, item, new, now=now)
        _log_fenced(outcome, queue_id=queue_id, op="renew")
        results[(item_id, seq)] = outcome
    return [results[key] for key in leases]


async def release(
    db: AsyncSession,
    *,
    item_id: int,
    seq: int,
    holder: str,
    now: dt.datetime,
) -> Released | None:
    """Release one live lease (its holder stops without a result, not the
    item's fault: a cancelled copy, a pre-empted worker). ``None`` = nothing
    live to release."""
    _aware(now)
    queue_id = await _queue_of(db, item_id)
    if queue_id is None or await _lock_queue(db, queue_id, "key_share") is None:
        return None
    _row, item = (await _load_items(db, [item_id]))[item_id]
    new, out = core.release(item, holder=holder, seq=seq)
    await _save(db, item, new, now=now)
    return out


async def release_holder(
    db: AsyncSession, holder: str, *, now: dt.datetime
) -> tuple[Lease, ...]:
    """Release every live lease of ``holder`` in every queue (the holder is gone:
    D267a - call it in the transaction that records the loss). Attempts are not
    counted; items with no other live lease go back to ``pending``."""
    _aware(now)
    rows = (
        await db.execute(
            select(work_lease.c.item_id, work_item.c.queue_id)
            .join(work_item, work_item.c.id == work_lease.c.item_id)
            .where(
                work_lease.c.holder == holder,
                work_lease.c.state == LeaseState.LIVE.value,
            )
        )
    ).all()
    by_queue: dict[int, list[int]] = {}
    for r in rows:
        by_queue.setdefault(r.queue_id, []).append(r.item_id)
    released: list[Lease] = []
    for queue_id in sorted(by_queue):
        if await _lock_queue(db, queue_id, "no_key_update") is None:
            continue
        for _row, item in (await _load_items(db, by_queue[queue_id])).values():
            new, out = core.release(item, holder=holder)
            if out is not None:
                await _save(db, item, new, now=now)
                released.extend(out.leases)
    if released:
        log.info("workqueue.holder_released", holder=holder, released=len(released))
    return tuple(released)


# --- results ----------------------------------------------------------------------


async def complete(
    db: AsyncSession,
    *,
    item_id: int,
    seq: int,
    holder: str,
    digest: str,
    now: dt.datetime,
) -> CompleteResult:
    """Commit a result for ``(item_id, seq)`` - idempotent, first commit wins
    (:func:`.core.complete`)."""
    _aware(now)
    queue_id = await _queue_of(db, item_id)
    queue = (
        None if queue_id is None else await _lock_queue(db, queue_id, "no_key_update")
    )
    if queue_id is None or queue is None:
        outcome: CompleteOutcome = Fenced(item_id, seq, FenceReason.UNKNOWN_LEASE)
        _log_fenced(outcome, queue_id=queue_id, op="complete")
        return CompleteResult(outcome, queue_id, queue_done=False)
    _row, item = (await _load_items(db, [item_id], seqs=[(item_id, seq)]))[item_id]
    new, outcome = core.complete(
        item,
        seq=seq,
        holder=holder,
        digest=digest,
        queue_state=QueueState(queue.state),
    )
    await _save(db, item, new, now=now)
    _log_fenced(outcome, queue_id=queue_id, op="complete")
    queue_done = False
    if isinstance(outcome, Committed):
        last = case(
            (
                work_queue.c.done_count + 1 == work_queue.c.item_count,
                QueueState.DONE.value,
            ),
            else_=work_queue.c.state,
        )
        counters = (
            await db.execute(
                update(work_queue)
                .where(work_queue.c.id == queue_id)
                .values(
                    done_count=work_queue.c.done_count + 1,
                    state=last,
                    closed_at=case(
                        (work_queue.c.done_count + 1 == work_queue.c.item_count, now),
                        else_=work_queue.c.closed_at,
                    ),
                )
                .returning(work_queue.c.done_count, work_queue.c.item_count)
            )
        ).one()
        queue_done = counters.done_count == counters.item_count
        if queue_done:
            log.info("workqueue.drained", queue_id=queue_id, items=counters.item_count)
    elif isinstance(outcome, AlreadyDone) and not outcome.own:
        log.info(
            "workqueue.already_done",
            queue_id=queue_id,
            item_id=item_id,
            seq=seq,
            winner_seq=outcome.winner_seq,
        )
    return CompleteResult(outcome, queue_id, queue_done=queue_done)


async def fail(
    db: AsyncSession,
    *,
    item_id: int,
    seq: int,
    holder: str,
    kind: str,
    now: dt.datetime,
    retryable: bool = True,
) -> FailResult:
    """Record a failed execution of ``(item_id, seq)`` (:func:`.core.fail`). If the
    item fails for good, the queue becomes ``failed`` in the same transaction and
    every other live lease is released (D267c: no partial result)."""
    _aware(now)
    queue_id = await _queue_of(db, item_id)
    queue = (
        None if queue_id is None else await _lock_queue(db, queue_id, "no_key_update")
    )
    if queue_id is None or queue is None:
        outcome: FailOutcome = Fenced(item_id, seq, FenceReason.UNKNOWN_LEASE)
        _log_fenced(outcome, queue_id=queue_id, op="fail")
        return FailResult(outcome, queue_id, queue_failed=False, released=())
    _row, item = (await _load_items(db, [item_id], seqs=[(item_id, seq)]))[item_id]
    new, outcome = core.fail(
        item,
        seq=seq,
        holder=holder,
        kind=kind,
        policy=_policy(queue),
        queue_state=QueueState(queue.state),
        retryable=retryable,
    )
    await _save(db, item, new, now=now)
    _log_fenced(outcome, queue_id=queue_id, op="fail")
    if not isinstance(outcome, ItemFailed):
        return FailResult(outcome, queue_id, queue_failed=False, released=())
    log.warning(
        "workqueue.item_failed",
        queue_id=queue_id,
        item_id=item_id,
        idx=outcome.idx,
        seq=seq,
        kind=kind,
        attempts=new.attempts,
    )
    released = await _close(db, queue_id, QueueState.FAILED, now=now)
    return FailResult(outcome, queue_id, queue_failed=True, released=released)


# --- expiry -----------------------------------------------------------------------


async def expire_due(
    db: AsyncSession, *, now: dt.datetime, limit: int = 500
) -> ExpireResult:
    """Expire up to ``limit`` live leases whose deadline is ``<= now`` (oldest
    deadline first), across all queues. Items without another live lease go
    back to ``pending`` (or fail, with ``count_expiry``)."""
    _aware(now)
    if not 1 <= limit <= 10_000:
        raise WorkQueueError("limit must be in [1, 10000]")
    due = (
        await db.execute(
            select(work_lease.c.item_id, work_item.c.queue_id)
            .join(work_item, work_item.c.id == work_lease.c.item_id)
            .where(
                work_lease.c.state == LeaseState.LIVE.value,
                work_lease.c.deadline <= now,
            )
            .order_by(work_lease.c.deadline)
            .limit(limit)
        )
    ).all()
    by_queue: dict[int, set[int]] = {}
    for r in due:
        by_queue.setdefault(r.queue_id, set()).add(r.item_id)
    expired: list[Expired] = []
    failed_queues: list[int] = []
    released: list[Lease] = []
    for queue_id in sorted(by_queue):
        queue = await _lock_queue(db, queue_id, "no_key_update")
        if queue is None:
            continue
        policy = _policy(queue)
        item_failed = False
        for _row, item in (await _load_items(db, sorted(by_queue[queue_id]))).values():
            new, out = core.expire(item, now=now, policy=policy)
            if out is None:
                continue
            await _save(db, item, new, now=now)
            expired.append(out)
            item_failed = item_failed or out.item_failed
            log.info(
                "workqueue.lease_expired",
                queue_id=queue_id,
                item_id=item.id,
                idx=item.idx,
                seqs=[x.seq for x in out.leases],
                holders=sorted({x.holder for x in out.leases}),
                item_failed=out.item_failed,
            )
        if item_failed and queue.state == QueueState.OPEN.value:
            failed_queues.append(queue_id)
            released.extend(await _close(db, queue_id, QueueState.FAILED, now=now))
    return ExpireResult(tuple(expired), tuple(failed_queues), tuple(released))


# --- speculation ------------------------------------------------------------------


async def _pending_left(db: AsyncSession, queue_id: int) -> int:
    return int(
        await db.scalar(
            select(func.count())
            .select_from(work_item)
            .where(
                work_item.c.queue_id == queue_id,
                work_item.c.state == ItemState.PENDING.value,
            )
        )
        or 0
    )


async def speculation_candidates(
    db: AsyncSession, queue_id: int, *, holder: str, site: str | None = None
) -> list[Item]:
    """Leased items ``holder`` at ``site`` may duplicate now, oldest lease first
    (:func:`.core.speculation_candidates`); read-only, no locks. ``[]`` while
    items are pending, when the budget is spent or the queue is closed."""
    site = holder if site is None else site
    queue = await get_queue(db, queue_id)
    if queue is None or queue.state is not QueueState.OPEN:
        return []
    budget = core.speculation_budget(
        total_items=queue.item_count,
        speculated_items=queue.speculated_count,
        max_frac=queue.policy.speculative_max_frac,
    )
    pending = await _pending_left(db, queue_id)
    if pending or budget <= 0:
        return []
    rows = (
        await db.execute(
            select(*_ITEM_COLUMNS).where(
                work_item.c.queue_id == queue_id,
                work_item.c.state == ItemState.LEASED.value,
            )
        )
    ).all()
    lease_rows = (
        await db.execute(
            select(work_lease).where(
                work_lease.c.item_id.in_([r.id for r in rows]),
                work_lease.c.state == LeaseState.LIVE.value,
            )
        )
    ).all()
    by_item: dict[int, list[Lease]] = {}
    for lr in lease_rows:
        by_item.setdefault(lr.item_id, []).append(_lease_from(lr))
    items = [_item_from(r, by_item.get(r.id, ())) for r in rows]
    return core.speculation_candidates(
        items, holder=holder, site=site, pending=pending, budget=budget
    )


async def speculate(
    db: AsyncSession,
    *,
    item_id: int,
    holder: str,
    site: str | None = None,
    now: dt.datetime,
    timeout_s: float | None = None,
) -> Grant | SpeculationRefusal:
    """Issue the speculative copy of ``item_id`` to ``holder`` at ``site``,
    re-checking every rule under the locks (D262/D266: no pending item left,
    budget, one live ordinary lease held elsewhere)."""
    _aware(now)
    site = holder if site is None else site
    queue_id = await _queue_of(db, item_id)
    queue = (
        None if queue_id is None else await _lock_queue(db, queue_id, "no_key_update")
    )
    if queue_id is None or queue is None:
        return SpeculationRefusal.UNKNOWN_ITEM
    if queue.state != QueueState.OPEN.value:
        return SpeculationRefusal.QUEUE_CLOSED
    if await _pending_left(db, queue_id):
        return SpeculationRefusal.PENDING_LEFT
    policy = _policy(queue)
    if (
        core.speculation_budget(
            total_items=queue.item_count,
            speculated_items=queue.speculated_count,
            max_frac=policy.speculative_max_frac,
        )
        <= 0
    ):
        return SpeculationRefusal.BUDGET_SPENT
    _row, item = (await _load_items(db, [item_id]))[item_id]
    new, out = core.speculate(
        item, holder=holder, site=site, now=now, policy=policy, timeout_s=timeout_s
    )
    if isinstance(out, SpeculationRefusal):
        return out
    ids = await _save(db, item, new, now=now)
    if item.speculations == 0:
        await db.execute(
            update(work_queue)
            .where(work_queue.c.id == queue_id)
            .values(speculated_count=work_queue.c.speculated_count + 1)
        )
    payload = await db.scalar(
        select(work_item.c.payload).where(work_item.c.id == item_id)
    )
    log.info(
        "workqueue.speculated",
        queue_id=queue_id,
        item_id=item_id,
        idx=item.idx,
        seq=out.seq,
        holder=holder,
        site=site,
    )
    return _grant(queue_id, item, out, ids[out.seq], payload)
