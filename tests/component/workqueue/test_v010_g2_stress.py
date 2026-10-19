"""V0.10 G2 (D262, brief §5b): 16 concurrent pullers x 10 000 items on real
PostgreSQL with crashing, slow and failing workers and an expiry sweeper
running under load. Every item ends ``done`` with exactly one committed result,
no item is lost, and no item ever has more than two live leases."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import time
from collections import Counter
from dataclasses import dataclass, field

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_workqueue import T0, Db, digest
from sqlalchemy import text

from meshforge.workqueue import store
from meshforge.workqueue.core import (
    AlreadyDone,
    Committed,
    Fenced,
    ItemFailed,
    QueuePolicy,
    QueueState,
    Retried,
)

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]

ITEMS = 10_000
PULLERS = 16
TIMEOUT_S = 1.0


@dataclass
class Tally:
    committed: list[tuple[int, int, str]] = field(default_factory=list)
    already_done: int = 0
    already_done_own: int = 0
    fenced: Counter[str] = field(default_factory=Counter)
    abandoned: int = 0
    slow: int = 0
    failed: int = 0
    grants: int = 0
    speculated: int = 0


class Clock:
    """The controller's clock for the test: T0 + real elapsed time."""

    def __init__(self) -> None:
        self._start = time.monotonic()

    def now(self) -> dt.datetime:
        return T0 + dt.timedelta(seconds=time.monotonic() - self._start)


async def _puller(
    db: Db, qid: int, n: int, clock: Clock, tally: Tally, rng: random.Random
) -> None:
    holder = f"worker-{n:02d}"
    site = f"site-{n % 5}"
    while True:
        async with db.sessions() as s, s.begin():
            grants = await store.lease_next(
                s, qid, holder=holder, site=site, k=rng.choice((1, 2)), now=clock.now()
            )
        if not grants:
            async with db.sessions() as s:
                info = await store.get_queue(s, qid)
            assert info is not None
            if info.state is not QueueState.OPEN:
                return
            await _speculate(db, qid, holder, site, clock, tally)
            await asyncio.sleep(0.02)
            continue
        tally.grants += len(grants)
        for g in grants:
            roll = rng.random()
            if roll < 0.004:  # the worker "crashes": the lease is never answered
                tally.abandoned += 1
                continue
            if roll < 0.010:  # retryable failure
                async with db.sessions() as s, s.begin():
                    out = await store.fail(
                        s,
                        item_id=g.item_id,
                        seq=g.seq,
                        holder=holder,
                        kind="oom",
                        now=clock.now(),
                    )
                assert not isinstance(out.outcome, ItemFailed)
                if isinstance(out.outcome, Retried):
                    tally.failed += 1
                continue
            if roll < 0.016:  # slower than the visibility timeout: a late result
                tally.slow += 1
                await asyncio.sleep(TIMEOUT_S * 1.3)
            async with db.sessions() as s, s.begin():
                res = await store.complete(
                    s,
                    item_id=g.item_id,
                    seq=g.seq,
                    holder=holder,
                    digest=digest(g.idx),
                    now=clock.now(),
                )
            out = res.outcome
            if isinstance(out, Committed):
                tally.committed.append((out.item_id, out.seq, out.digest))
            elif isinstance(out, AlreadyDone):
                tally.already_done += 1
                tally.already_done_own += out.own
                assert out.winner_digest == digest(g.idx)
            else:
                assert isinstance(out, Fenced)
                tally.fenced[out.reason.value] += 1


async def _speculate(
    db: Db, qid: int, holder: str, site: str, clock: Clock, tally: Tally
) -> None:
    """At the tail, an idle worker duplicates the oldest leased item (D266)."""
    async with db.sessions() as s:
        candidates = await store.speculation_candidates(
            s, qid, holder=holder, site=site
        )
    if not candidates:
        return
    async with db.sessions() as s, s.begin():
        copy = await store.speculate(
            s, item_id=candidates[0].id, holder=holder, site=site, now=clock.now()
        )
    if not isinstance(copy, store.Grant):
        return
    tally.speculated += 1
    async with db.sessions() as s, s.begin():
        res = await store.complete(
            s,
            item_id=copy.item_id,
            seq=copy.seq,
            holder=holder,
            digest=digest(copy.idx),
            now=clock.now(),
        )
    if isinstance(res.outcome, Committed):
        tally.committed.append(
            (res.outcome.item_id, res.outcome.seq, res.outcome.digest)
        )
    elif isinstance(res.outcome, AlreadyDone):
        tally.already_done += 1


async def _sweeper(
    db: Db, clock: Clock, stop: asyncio.Event, expired: list[int]
) -> None:
    while not stop.is_set():
        async with db.sessions() as s, s.begin():
            result = await store.expire_due(s, now=clock.now(), limit=500)
        expired.extend(e.item_id for e in result.expired)
        assert result.failed_queues == ()
        await asyncio.sleep(0.05)


async def _monitor(db: Db, stop: asyncio.Event, peaks: list[int]) -> None:
    while not stop.is_set():
        async with db.sessions() as s:
            peak = await s.scalar(
                text(
                    "SELECT coalesce(max(n), 0) FROM (SELECT count(*) AS n FROM "
                    "work_lease WHERE state = 'live' GROUP BY item_id) AS live"
                )
            )
        peaks.append(int(peak or 0))
        await asyncio.sleep(0.1)


async def test_16_pullers_10000_items_each_done_exactly_once(db: Db) -> None:
    policy = QueuePolicy(visibility_timeout_s=TIMEOUT_S, max_attempts=100)
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s,
            kind="stress",
            owner_id="q-1",
            payloads=[{"i": i} for i in range(ITEMS)],
            policy=policy,
        )
    clock = Clock()
    tallies = [Tally() for _ in range(PULLERS)]
    stop = asyncio.Event()
    expired: list[int] = []
    peaks: list[int] = []
    sweeper = asyncio.create_task(_sweeper(db, clock, stop, expired))
    monitor = asyncio.create_task(_monitor(db, stop, peaks))
    started = time.monotonic()
    try:
        await asyncio.gather(
            *(
                _puller(db, info.id, n, clock, tallies[n], random.Random(1000 + n))  # noqa: S311
                for n in range(PULLERS)
            )
        )
    finally:
        stop.set()
        await asyncio.gather(sweeper, monitor)
    elapsed = time.monotonic() - started

    committed = [c for t in tallies for c in t.committed]
    items = [c[0] for c in committed]
    assert len(committed) == ITEMS, "every item committed"
    assert len(set(items)) == ITEMS, "no item committed twice"
    assert sum(t.abandoned for t in tallies) > 0 and expired, "expiry exercised"
    assert sum(t.failed for t in tallies) > 0
    assert max(peaks) <= 2
    async with db.sessions() as s:
        counts = await store.counts(s, info.id)
        final = await store.get_queue(s, info.id)
        rows = (
            await s.execute(
                text(
                    "SELECT i.id, i.idx, i.state, i.result_digest, i.done_lease_seq, "
                    "count(l.*) FILTER (WHERE l.state = 'completed') AS won, "
                    "count(l.*) FILTER (WHERE l.state = 'live') AS live, "
                    "max(l.seq) FILTER (WHERE l.state = 'completed') AS won_seq "
                    "FROM work_item i JOIN work_lease l ON l.item_id = i.id "
                    "WHERE i.queue_id = :q GROUP BY i.id"
                ),
                {"q": info.id},
            )
        ).all()
    assert counts == store.QueueCounts(ITEMS, 0, 0, ITEMS, 0, 0, 0)
    assert final is not None and final.state is QueueState.DONE
    assert final.done_count == ITEMS
    assert len(rows) == ITEMS
    by_item = {c[0]: c for c in committed}
    for r in rows:
        assert r.state == "done" and r.won == 1 and r.live == 0
        assert r.done_lease_seq == r.won_seq == by_item[r.id][1]
        assert r.result_digest == digest(r.idx) == by_item[r.id][2]
    print(  # noqa: T201 - recorded in the progress note
        f"stress: {ITEMS} items, {PULLERS} pullers, {elapsed:.1f} s, "
        f"grants={sum(t.grants for t in tallies)}, expired={len(expired)}, "
        f"abandoned={sum(t.abandoned for t in tallies)}, "
        f"slow={sum(t.slow for t in tallies)}, failed={sum(t.failed for t in tallies)}, "
        f"already_done={sum(t.already_done for t in tallies)}, "
        f"fenced={sum((t.fenced for t in tallies), Counter())}, "
        f"speculated={sum(t.speculated for t in tallies)}, peak_live={max(peaks)}"
    )
