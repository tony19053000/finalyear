"""Speculative copies of straggling chunks (V0.10 G6, D266; ADR-0006, ADR-0019).

Asked from :meth:`~.store.ChunkStore.request` when a lane is **idle** - it
asked for work and no chunk is pending (every remaining chunk is leased
elsewhere). Then, inside that request's transaction:

1. the leased chunks this lane *may* duplicate come from the work queue
   (:func:`meshforge.workqueue.store.speculation_candidates`: nothing pending,
   one live ordinary lease held by another lane on another node, the job's
   speculation budget not spent - ``ceil(speculative_max_frac x chunks)``
   chunks ever get a copy, D277);
2. each node's expected time for a chunk is taken from **this job's committed
   chunks** on that node (the p50 of seconds per item, so a ragged last chunk
   scales; ``history``) or, for a node with none yet, from the planner's
   predicted items/s for it in the job's latest dispatched decision plus the
   assumed per-chunk overhead (``planner``); a node with neither gets no copy
   and its chunks are not duplicated (no estimate, no duplicate work);
3. :func:`meshforge.workqueue.core.pick_speculation` picks the chunk with the
   largest predicted remaining time, if it exceeds ``speculation_factor`` (1.5)
   x the idle node's expected time for it; never one that already failed on
   the idle node, never one the lane is cooling down from;
4. :func:`meshforge.workqueue.store.speculate` issues the copy - re-checking
   every rule under the queue + item locks, so a race (the original committed,
   a new chunk became pending, the budget went) is a refusal, not a copy.

The first committed result wins (:func:`meshforge.workqueue.store.complete`);
the other lease is superseded and its lane gets ``CancelChunk``; a later result
of it is ``already_done`` and, when its digest differs while
``split.deterministic`` is on, the job's ``nondeterministic_chunk`` warning
(V0.10 G5 hook). Lock order (ADR-0019 addendum): the caller holds the lane's
attempt and job rows (``FOR SHARE``) before any work-queue row.
"""

from __future__ import annotations

import datetime as dt
import math
import time
from collections import OrderedDict
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Float, Integer, and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from meshforge.common.logging import get_logger
from meshforge.controller.store.models.plan import PlanDecision
from meshforge.workqueue import store as wq
from meshforge.workqueue.core import (
    Item,
    Lease,
    LeaseState,
    SpeculationPick,
    SpeculationRefusal,
    pick_speculation,
)
from meshforge.workqueue.tables import work_item, work_lease

__all__ = [
    "HISTORY",
    "PLANNER",
    "SiteTime",
    "SiteTimesCache",
    "Speculated",
    "SpeculationConfig",
    "site_times",
    "speculate_for",
]

log = get_logger(__name__)

HISTORY: Final = "history"
"""A node's chunk time measured from this job's committed chunks."""
PLANNER: Final = "planner"
"""A node's chunk time from the planner's predicted rate (no chunk yet)."""
_SINGLE_MODES: Final = frozenset({"local", "offload"})
"""Decision rows that carry one node's ``rate_items_s`` (D265)."""


@dataclass(frozen=True, slots=True)
class SpeculationConfig:
    """``[controller.split]`` knobs the decision needs (D266/D275)."""

    factor: float = 1.5
    """Duplicate only when remaining(holder) > factor x expected(idle node)."""
    overhead_s: float = 1.5
    """Assumed per-chunk overhead added to a planner-rate estimate."""
    cache_s: float = 5.0
    """V0.10 G8 (D285): how long a queue's chunk times are reused while no
    chunk of it committed (:class:`SiteTimesCache`); 0 = never cached."""
    cache_jobs: int = 256
    """Most queues whose chunk times are cached (LRU)."""

    def __post_init__(self) -> None:
        if not (math.isfinite(self.factor) and self.factor >= 1.0):
            raise ValueError("speculation factor must be a finite number >= 1")
        if not (math.isfinite(self.overhead_s) and self.overhead_s >= 0):
            raise ValueError("overhead_s must be a finite number >= 0")
        if not (math.isfinite(self.cache_s) and self.cache_s >= 0):
            raise ValueError("cache_s must be a finite number >= 0")
        if self.cache_jobs < 1:
            raise ValueError("cache_jobs must be >= 1")


@dataclass(frozen=True, slots=True)
class SiteTime:
    """One node's expected seconds for a chunk of ``n`` items."""

    per_item_s: float
    fixed_s: float
    source: str
    """:data:`HISTORY` or :data:`PLANNER`."""
    samples: int = 0
    """Committed chunks behind a ``history`` estimate."""

    def seconds(self, items: int) -> float:
        return self.fixed_s + self.per_item_s * max(1, items)


@dataclass(frozen=True, slots=True)
class Speculated:
    """A copy was issued: the grant and the numbers that justified it."""

    grant: wq.Grant
    pick: SpeculationPick
    holder_node: str
    holder_source: str
    copy_source: str


# ------------------------------------------------------------------ estimates


async def _history(db: AsyncSession, queue_id: int) -> dict[str, SiteTime]:
    """p50 of seconds per item of the committed chunks, per node (the winning
    lease's grant -> the commit; one aggregate row per node, bounded)."""
    count = work_item.c.payload["count"].astext.cast(Integer)
    seconds = func.extract("epoch", work_item.c.done_at - work_lease.c.leased_at)
    per_item = (seconds / func.greatest(count, 1)).cast(Float)
    rows = (
        await db.execute(
            select(
                work_lease.c.site,
                func.percentile_cont(0.5).within_group(per_item).label("p50"),
                func.count().label("n"),
            )
            .select_from(
                work_lease.join(
                    work_item,
                    and_(
                        work_item.c.id == work_lease.c.item_id,
                        work_item.c.done_lease_seq == work_lease.c.seq,
                    ),
                )
            )
            .where(
                work_item.c.queue_id == queue_id,
                work_lease.c.state == LeaseState.COMPLETED.value,
                work_item.c.done_at.is_not(None),
                func.jsonb_typeof(work_item.c.payload["count"]) == "number",
            )
            .group_by(work_lease.c.site)
        )
    ).all()
    out: dict[str, SiteTime] = {}
    for row in rows:
        p50 = None if row.p50 is None else float(row.p50)
        if p50 is not None and math.isfinite(p50) and p50 >= 0:
            out[str(row.site)] = SiteTime(p50, 0.0, HISTORY, int(row.n))
    return out


def planner_rates(candidates: Any) -> dict[str, float]:
    """``node_id -> items/s`` from a stored decision's single-node rows (the
    Local / Mode 1 rows of a split job carry ``rate_items_s``, D265)."""
    out: dict[str, float] = {}
    if not isinstance(candidates, list):
        return out
    for row in candidates:
        if not isinstance(row, Mapping) or row.get("mode") not in _SINGLE_MODES:
            continue
        node_id, rate = row.get("node_id"), row.get("rate_items_s")
        if (
            isinstance(node_id, str)
            and isinstance(rate, int | float)
            and not isinstance(rate, bool)
            and math.isfinite(rate)
            and rate > 0
        ):
            out.setdefault(node_id, float(rate))
    return out


async def _planner(db: AsyncSession, job_id: str) -> dict[str, float]:
    candidates = await db.scalar(
        select(PlanDecision.candidates)
        .where(PlanDecision.job_id == job_id, PlanDecision.outcome == "dispatched")
        .order_by(PlanDecision.id.desc())
        .limit(1)
    )
    return planner_rates(candidates)


@dataclass(slots=True)
class _Times:
    done: int
    at: float
    history: dict[str, SiteTime]
    planner: dict[str, float] | None = None


class SiteTimesCache:
    """Per-queue chunk times for the idle-lane check (V0.10 G8, D285).

    Without it every idle lane's ``ChunkRequest`` at the tail ran the
    ``percentile_cont`` history query (and the planner-rate read) - one per
    lane per ``wait_retry_s``. The history only changes when a chunk of the
    queue **commits**, so an entry is keyed by the queue's committed count
    read in the same request: a commit makes the next request read again
    (never a stale p50 after a commit). Bounded twice: an entry lives at most
    ``ttl_s`` (the planner's rates can change on a re-plan without a commit)
    and at most ``entries`` queues are kept (least recently used dropped).
    ``ttl_s`` 0 disables it. One per controller (the dispatcher's one
    ``ChunkStore``); not shared across processes - each controller would read
    its own (there is one controller, ADR-0001)."""

    def __init__(
        self,
        *,
        ttl_s: float,
        entries: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not (math.isfinite(ttl_s) and ttl_s >= 0) or entries < 1:
            raise ValueError("ttl_s must be >= 0 and entries >= 1")
        self._ttl_s = ttl_s
        self._entries = entries
        self._clock = clock
        self._data: OrderedDict[int, _Times] = OrderedDict()
        self.reads = 0
        """History queries run (cache misses) - tests, metrics."""
        self.hits = 0

    def __len__(self) -> int:
        return len(self._data)

    def _get(self, queue_id: int, done: int | None) -> _Times | None:
        if self._ttl_s <= 0 or done is None:
            return None
        hit = self._data.get(queue_id)
        if hit is None:
            return None
        if hit.done != done or self._clock() - hit.at > self._ttl_s:
            del self._data[queue_id]
            return None
        self._data.move_to_end(queue_id)
        return hit

    def _put(self, queue_id: int, entry: _Times) -> None:
        if self._ttl_s <= 0:
            return
        self._data[queue_id] = entry
        self._data.move_to_end(queue_id)
        while len(self._data) > self._entries:
            self._data.popitem(last=False)

    async def history(
        self, db: AsyncSession, queue_id: int, done: int | None
    ) -> tuple[dict[str, SiteTime], _Times | None]:
        hit = self._get(queue_id, done)
        if hit is not None:
            self.hits += 1
            return dict(hit.history), hit
        self.reads += 1
        found = await _history(db, queue_id)
        entry = None
        if done is not None and self._ttl_s > 0:
            entry = _Times(done=done, at=self._clock(), history=dict(found))
            self._put(queue_id, entry)
        return found, entry


async def site_times(
    db: AsyncSession,
    *,
    queue_id: int,
    job_id: str,
    sites: Collection[str],
    overhead_s: float,
    cache: SiteTimesCache | None = None,
    done: int | None = None,
) -> dict[str, SiteTime]:
    """Expected chunk time per node of ``sites`` (module docstring, step 2);
    a node with neither history nor a planner rate is absent. ``cache`` +
    ``done`` (the queue's committed chunks, read in this transaction) reuse
    the queue's last answer while nothing committed (:class:`SiteTimesCache`)."""
    entry: _Times | None = None
    if cache is None:
        found = await _history(db, queue_id)
    else:
        found, entry = await cache.history(db, queue_id, done)
    missing = [s for s in sites if s not in found]
    if missing:
        if entry is not None and entry.planner is not None:
            rates = entry.planner
        else:
            rates = await _planner(db, job_id)
            if entry is not None:
                entry.planner = rates
        for site in missing:
            rate = rates.get(site)
            if rate is not None:
                found[site] = SiteTime(1.0 / rate, overhead_s, PLANNER)
    return {s: found[s] for s in sites if s in found}


async def _item_counts(db: AsyncSession, item_ids: Collection[int]) -> dict[int, int]:
    rows = (
        await db.execute(
            select(work_item.c.id, work_item.c.payload).where(
                work_item.c.id.in_(sorted(item_ids))
            )
        )
    ).all()
    out: dict[int, int] = {}
    for row in rows:
        count = row.payload.get("count") if isinstance(row.payload, dict) else None
        if isinstance(count, int) and not isinstance(count, bool) and count >= 1:
            out[int(row.id)] = count
    return out


# ------------------------------------------------------------------ decision


async def speculate_for(
    db: AsyncSession,
    *,
    queue: wq.QueueInfo,
    job_id: str,
    attempt_id: str,
    node_id: str,
    now: dt.datetime,
    config: SpeculationConfig,
    avoid: Collection[int] = (),
    cache: SiteTimesCache | None = None,
    done: int | None = None,
) -> Speculated | SpeculationRefusal | None:
    """Issue a speculative copy to the idle lane ``attempt_id`` on ``node_id``
    (module docstring), in the caller's transaction. ``None`` = nothing worth
    duplicating now; a :class:`SpeculationRefusal` = the pick lost a race
    under the locks (nothing changed)."""
    if queue.policy.speculative_max_frac <= 0:
        return None  # split.speculative: false (or the operator's 0)
    candidates = await wq.speculation_candidates(
        db, queue.id, holder=attempt_id, site=node_id
    )
    if not candidates:
        return None
    sites = {node_id, *(x.live_leases[0].site for x in candidates if x.live_leases)}
    times = await site_times(
        db,
        queue_id=queue.id,
        job_id=job_id,
        sites=sites,
        overhead_s=config.overhead_s,
        cache=cache,
        done=done,
    )
    mine = times.get(node_id)
    if mine is None:
        return None
    counts = await _item_counts(db, [x.id for x in candidates])

    def copy_s(item: Item) -> float | None:
        n = counts.get(item.id)
        return None if n is None else mine.seconds(n)

    def expected_s(item: Item, lease: Lease) -> float | None:
        n, there = counts.get(item.id), times.get(lease.site)
        return None if n is None or there is None else there.seconds(n)

    pick = pick_speculation(
        candidates,
        site=node_id,
        now=now,
        copy_s=copy_s,
        expected_s=expected_s,
        factor=config.factor,
        exclude=avoid,
    )
    if pick is None:
        return None
    got = await wq.speculate(
        db, item_id=pick.item.id, holder=attempt_id, site=node_id, now=now
    )
    if isinstance(got, SpeculationRefusal):
        log.info(
            "controller.split.speculation_refused",
            job_id=job_id,
            attempt_id=attempt_id,
            node_id=node_id,
            chunk=pick.item.idx,
            refusal=got.value,
        )
        return got
    holder_node = pick.original.site
    out = Speculated(
        grant=got,
        pick=pick,
        holder_node=holder_node,
        holder_source=times[holder_node].source,
        copy_source=mine.source,
    )
    log.info(
        "controller.split.speculated",
        job_id=job_id,
        attempt_id=attempt_id,
        node_id=node_id,
        chunk=pick.item.idx,
        lease_seq=got.seq,
        holder_attempt_id=pick.original.holder,
        holder_node_id=holder_node,
        elapsed_s=round(pick.elapsed_s, 3),
        expected_s=round(pick.expected_s, 3),
        remaining_s=round(pick.remaining_s, 3),
        copy_s=round(pick.copy_s, 3),
        holder_source=out.holder_source,
        copy_source=out.copy_source,
    )
    return out
