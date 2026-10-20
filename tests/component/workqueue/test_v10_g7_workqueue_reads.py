"""V0.10 G7 (D277 "paged list_items"): read-only views of a work queue on real
PostgreSQL - keyset pages in idx order, state and site filters, live leases and
the winning lease (also a late winner from an expired lease, ADR-0015), and
argument checks. Nothing is mocked."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_workqueue import Db, at, digest

from meshforge.workqueue import reads, store
from meshforge.workqueue.core import ItemState, LeaseState, QueuePolicy
from meshforge.workqueue.store import WorkQueueError

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

POLICY = QueuePolicy(visibility_timeout_s=30, max_attempts=3, max_failed_sites=2)


async def _setup(db: Db) -> dict[str, Any]:
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s,
            kind="test",
            owner_id="o",
            payloads=[{"i": i} for i in range(25)],
            policy=POLICY,
        )
    qid = info.id
    async with db.sessions() as s, s.begin():
        a = await store.lease_next(s, qid, holder="ha", site="s1", k=3, now=at(0))
    async with db.sessions() as s, s.begin():
        b = await store.lease_next(s, qid, holder="hb", site="s2", k=2, now=at(0))
    for g in a[:2]:
        async with db.sessions() as s, s.begin():
            await store.complete(
                s,
                item_id=g.item_id,
                seq=g.seq,
                holder="ha",
                digest=digest(g.idx),
                now=at(1),
            )
    # hb's first lease expires; its late result still wins (item not done)
    async with db.sessions() as s, s.begin():
        await store.expire_due(s, now=at(100))
    async with db.sessions() as s, s.begin():
        late = await store.complete(
            s,
            item_id=b[0].item_id,
            seq=b[0].seq,
            holder="hb",
            digest=digest(99),
            now=at(101),
        )
    assert late.outcome.__class__.__name__ == "Committed"
    return {"qid": qid, "a": a, "b": b}


async def test_pages_walk_every_item_once_in_idx_order(db: Db) -> None:
    got = await _setup(db)
    seen: list[int] = []
    after = None
    async with db.sessions() as s:
        while True:
            page = await reads.list_items(s, got["qid"], limit=10, after_idx=after)
            if not page:
                break
            seen.extend(v.idx for v in page)
            after = page[-1].idx
    assert seen == list(range(25))


async def test_winners_live_leases_and_filters(db: Db) -> None:
    got = await _setup(db)
    a, b = got["a"], got["b"]
    async with db.sessions() as s:
        everything = {
            v.idx: v for v in await reads.list_items(s, got["qid"], limit=100)
        }
        done = await reads.list_items(s, got["qid"], limit=100, states=[ItemState.DONE])
        on_s1 = await reads.list_items(s, got["qid"], limit=100, site="s1")
        on_s2 = await reads.list_items(s, got["qid"], limit=100, site="s2")
        none = await reads.list_items(s, got["qid"], limit=100, site="s9")
    assert sorted(v.idx for v in done) == sorted([a[0].idx, a[1].idx, b[0].idx])
    first = everything[a[0].idx]
    assert first.state is ItemState.DONE and first.winner is not None
    assert (first.winner.holder, first.winner.site) == ("ha", "s1")
    assert first.winner.state is LeaseState.COMPLETED and first.live == ()
    assert first.result_digest == digest(a[0].idx) and first.payload == {"i": a[0].idx}
    # the late winner: its lease had expired, yet its result was committed
    # (the store marks that lease completed; it ended at the expiry)
    late = everything[b[0].idx]
    assert late.state is ItemState.DONE and late.winner is not None
    assert late.winner.holder == "hb" and late.winner.site == "s2"
    assert late.winner.ended_at == at(100)
    # a live lease after expiry? a[2] and b[1] expired at t=100 -> pending
    assert everything[a[2].idx].state is ItemState.PENDING
    assert everything[a[2].idx].live == ()
    # site filter = live lease there, or the winning lease's site
    assert sorted(v.idx for v in on_s1) == sorted([a[0].idx, a[1].idx])
    assert [v.idx for v in on_s2] == [b[0].idx]
    assert none == []


async def test_live_leases_are_listed_with_their_holder(db: Db) -> None:
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s, kind="test", owner_id="o2", payloads=[{"i": 0}, {"i": 1}], policy=POLICY
        )
    async with db.sessions() as s, s.begin():
        g = await store.lease_next(s, info.id, holder="hx", site="sx", k=1, now=at(0))
    async with db.sessions() as s:
        page = await reads.list_items(s, info.id, limit=5)
        leased = await reads.list_items(
            s, info.id, limit=5, states=[ItemState.LEASED], site="sx"
        )
    assert page[0].state is ItemState.LEASED and page[0].winner is None
    assert [(le.holder, le.site, le.seq) for le in page[0].live] == [
        ("hx", "sx", g[0].seq)
    ]
    assert page[1].live == () and page[1].state is ItemState.PENDING
    assert [v.idx for v in leased] == [0]


async def test_arguments_are_checked(db: Db) -> None:
    async with db.sessions() as s:
        for kwargs in (
            {"limit": 0},
            {"limit": reads.MAX_PAGE_ITEMS + 1},
            {"limit": 5, "after_idx": -2},
            {"limit": 5, "site": ""},
            {"limit": 5, "site": "x" * 1000},
        ):
            with pytest.raises(WorkQueueError):
                await reads.list_items(s, 1, **kwargs)  # type: ignore[arg-type]
        assert await reads.list_items(s, 424242, limit=5) == []
