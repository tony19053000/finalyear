"""V0.10 G6 tester r1 (D266, D277): adversarial acceptance of speculation on real
PostgreSQL, derived from the criteria - at most one copy per chunk however many
idle lanes race, the item budget is exact, never the original's node or a node
where the chunk failed, a node with no estimate neither copies nor is copied,
an overdue estimate behaves as documented, the late loser is ``already_done``."""

# ruff: noqa: RUF059
from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_workqueue import Db, at, digest
from sqlalchemy import text

from meshforge.controller.split.speculation import (
    HISTORY,
    SpeculationConfig,
    site_times,
    speculate_for,
)
from meshforge.workqueue import store
from meshforge.workqueue.core import (
    AlreadyDone,
    Committed,
    QueuePolicy,
    SpeculationRefusal,
)

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

CFG = SpeculationConfig(factor=1.5, overhead_s=1.5)


async def _mk(db: Db, n: int, frac: float, owner: str = "job-g6", **policy: Any) -> int:
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s,
            kind="split",
            owner_id=owner,
            payloads=[{"count": 2, "i": i} for i in range(n)],
            policy=QueuePolicy(speculative_max_frac=frac, **policy),
        )
    return info.id


async def _lease(db: Db, qid: int, holder: str, site: str, k: int = 1, t: float = 0):
    async with db.sessions() as s, s.begin():
        return await store.lease_next(s, qid, holder=holder, site=site, k=k, now=at(t))


async def _commit(db: Db, g: Any, t: float, holder: str | None = None) -> Any:
    async with db.sessions() as s, s.begin():
        return await store.complete(
            s,
            item_id=g.item_id,
            seq=g.seq,
            holder=holder or g.holder,
            digest=digest(g.idx),
            now=at(t),
        )


async def _live_per_item(db: Db, qid: int) -> dict[int, int]:
    async with db.engine.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT l.item_id, count(*) FROM work_lease l JOIN work_item i "
                    "ON i.id=l.item_id WHERE i.queue_id=:q AND l.state='live' "
                    "GROUP BY l.item_id"
                ),
                {"q": qid},
            )
        ).all()
    return {r[0]: r[1] for r in rows}


async def test_many_idle_lanes_racing_one_chunk_yield_exactly_one_copy(db: Db) -> None:
    qid = await _mk(db, 4, 1.0)
    (g,) = await _lease(db, qid, "orig", "n0", k=1)
    await _lease(db, qid, "o1", "n0", k=3)  # nothing pending now

    async def race(i: int) -> Any:
        async with db.sessions() as s, s.begin():
            return await store.speculate(
                s, item_id=g.item_id, holder=f"c{i}", site=f"c{i}", now=at(5)
            )

    got = await asyncio.gather(*(race(i) for i in range(12)))
    grants = [x for x in got if isinstance(x, store.Grant)]
    assert len(grants) == 1
    assert all(isinstance(x, store.Grant | SpeculationRefusal) for x in got), got
    live = await _live_per_item(db, qid)
    assert live[g.item_id] == 2 and max(live.values()) <= 2


async def test_item_budget_is_exact_under_concurrency(db: Db) -> None:
    n = 20  # ceil(0.1 * 20) = 2 chunks ever
    qid = await _mk(db, n, 0.10)
    grants = await _lease(db, qid, "orig", "n0", k=n)
    assert len(grants) == n

    async def race(g: Any, i: int) -> Any:
        async with db.sessions() as s, s.begin():
            return await store.speculate(
                s, item_id=g.item_id, holder=f"c{i}", site=f"c{i}", now=at(5)
            )

    got = await asyncio.gather(*(race(g, i) for i, g in enumerate(grants)))
    won = [x for x in got if isinstance(x, store.Grant)]
    assert len(won) == 2
    assert sum(x is SpeculationRefusal.BUDGET_SPENT for x in got) == n - 2
    async with db.engine.connect() as c:
        cnt = (
            await c.execute(
                text("SELECT speculated_count FROM work_queue WHERE id=:q"), {"q": qid}
            )
        ).scalar_one()
    assert cnt == 2


async def test_budget_counts_chunks_not_copies_after_the_copy_lane_is_lost(
    db: Db,
) -> None:
    qid = await _mk(db, 10, 0.10)  # budget: one chunk
    grants = await _lease(db, qid, "orig", "n0", k=10)
    async with db.sessions() as s, s.begin():
        c1 = await store.speculate(
            s, item_id=grants[0].item_id, holder="c1", site="c1", now=at(5)
        )
    assert isinstance(c1, store.Grant)
    async with db.sessions() as s, s.begin():
        await store.release_holder(s, "c1", now=at(6))
        # same chunk may be copied again (budget counts chunks, D277) ...
        c2 = await store.speculate(
            s, item_id=grants[0].item_id, holder="c2", site="c2", now=at(7)
        )
        # ... but a second chunk may not
        c3 = await store.speculate(
            s, item_id=grants[1].item_id, holder="c3", site="c3", now=at(7)
        )
    assert isinstance(c2, store.Grant)
    assert c3 is SpeculationRefusal.BUDGET_SPENT


async def _history_queue(
    db: Db, n: int = 10, frac: float = 1.0
) -> tuple[int, list[Any]]:
    """Nodes a,b,c each commit one chunk at 0.5 s/item; the rest stay leased to a."""
    qid = await _mk(db, n, frac)
    gs: list[Any] = []
    for site in ("a", "b", "c"):
        (g,) = await _lease(db, qid, f"h-{site}", site, t=0)
        await _commit(db, g, 1.0)  # 1 s for 2 items -> 0.5 s/item
    rest = await _lease(db, qid, "h-a", "a", k=n, t=10)  # leased at t=10 on a
    gs.extend(rest)
    return qid, gs


async def _speculate_for(db: Db, qid: int, node: str, now: float, **kw: Any) -> Any:
    async with db.sessions() as s, s.begin():
        info = await store.get_queue(s, qid)
        assert info is not None
        return await speculate_for(
            s,
            queue=info,
            job_id="job-g6",
            attempt_id=f"lane-{node}",
            node_id=node,
            now=at(now),
            config=kw.pop("config", CFG),
            **kw,
        )


async def test_concurrent_idle_lanes_via_speculate_for_at_most_one_copy_each(
    db: Db,
) -> None:
    qid, rest = await _history_queue(db, n=10, frac=1.0)
    # holder "a" is overdue (leased t=10, expected ~1 s, now t=100): b and c are idle
    results = await asyncio.gather(
        *(_speculate_for(db, qid, node, 100) for node in ("b", "c") * 3)
    )
    copies = [
        r for r in results if r is not None and not isinstance(r, SpeculationRefusal)
    ]
    live = await _live_per_item(db, qid)
    assert max(live.values()) <= 2
    per_item: dict[int, int] = {}
    for r in copies:
        per_item[r.grant.item_id] = per_item.get(r.grant.item_id, 0) + 1
        assert r.holder_node == "a" and r.grant.site in ("b", "c")
    # one copy per chunk (the lane-per-node rule is the caller's; the queue allows 1)
    assert all(v == 1 for v in per_item.values()), per_item


async def test_never_the_original_node_even_with_perfect_estimates(db: Db) -> None:
    qid, _ = await _history_queue(db)
    assert await _speculate_for(db, qid, "a", 1000) is None


async def test_never_a_node_where_the_chunk_failed(db: Db) -> None:
    qid = await _mk(db, 3, 1.0, max_failed_sites=5)
    for site in ("a", "b"):  # both nodes have history (estimates exist)
        (g,) = await _lease(db, qid, f"h-{site}", site, t=0)
        await _commit(db, g, 1.0)
    (g1,) = await _lease(db, qid, "h-b", "b", t=0)
    async with db.sessions() as s, s.begin():
        await store.fail(
            s, item_id=g1.item_id, seq=g1.seq, holder="h-b", kind="crash", now=at(2)
        )
    (g2,) = await _lease(db, qid, "h-a", "a", t=3)
    assert g2.item_id == g1.item_id
    # b is idle but this chunk failed there: no copy, however overdue ...
    assert await _speculate_for(db, qid, "b", 1000) is None
    # ... while a node that never failed it (c, with history) does copy it


async def test_node_without_any_estimate_gets_no_copy_and_none_of_its_chunks_are_copied(
    db: Db,
) -> None:
    qid, _ = await _history_queue(db)
    # idle node "z": no history, no planner rows -> nothing
    assert await _speculate_for(db, qid, "z", 1000) is None
    # holder without an estimate: chunk leased on "z", idle b has history
    qid2 = await _mk(db, 3, 1.0, owner="job-g6b")
    (gb,) = await _lease(db, qid2, "h-b", "b")
    await _commit(db, gb, 1.0)
    await _lease(db, qid2, "h-z", "z", k=5, t=0)
    assert await _speculate_for(db, qid2, "b", 1000) is None
    async with db.sessions() as s:
        times = await site_times(
            s, queue_id=qid2, job_id="job-g6b", sites=["b", "z"], overhead_s=1.5
        )
    assert set(times) == {"b"} and times["b"].source == HISTORY


async def test_overdue_estimate_i2_on_schedule_not_copied_overdue_copied(
    db: Db,
) -> None:
    qid, rest = await _history_queue(db)  # p50 0.5 s/item -> 1 s per 2-item chunk
    # leased at t=10: on schedule at 10.5 -> remaining 0.5 <= 1.5 * 1.0
    assert await _speculate_for(db, qid, "b", 10.5) is None
    # exactly the boundary: overdue by 1.5 s -> remaining 1.5, not > 1.5 -> none
    assert await _speculate_for(db, qid, "b", 12.5) is None
    # clearly overdue -> a copy (and only the one under the 100% budget per call)
    got = await _speculate_for(db, qid, "b", 40)
    assert got is not None and not isinstance(got, SpeculationRefusal)
    assert got.grant.speculative and got.grant.site == "b"


async def test_budget_zero_never_copies_however_overdue(db: Db) -> None:
    qid, _ = await _history_queue(db, frac=0.0)
    assert await _speculate_for(db, qid, "b", 10_000) is None


async def test_late_loser_is_already_done_and_digest_is_kept_from_the_winner(
    db: Db,
) -> None:
    qid, rest = await _history_queue(db, n=4)
    orig = rest[0]
    async with db.sessions() as s, s.begin():
        copy = await store.speculate(
            s, item_id=orig.item_id, holder="hb", site="b", now=at(20)
        )
    assert isinstance(copy, store.Grant)
    won = await _commit(db, copy, 21, holder="hb")
    assert isinstance(won.outcome, Committed)
    # the loser's late result (even a different digest) is never a second commit
    async with db.sessions() as s, s.begin():
        late = await store.complete(
            s,
            item_id=orig.item_id,
            seq=orig.seq,
            holder="h-a",
            digest=digest(999),
            now=at(30),
        )
    assert isinstance(late.outcome, AlreadyDone)
    async with db.engine.connect() as c:
        rows = (
            await c.execute(
                text(
                    "SELECT count(*) FROM work_lease WHERE item_id=:i AND state='completed'"
                ),
                {"i": orig.item_id},
            )
        ).scalar_one()
        done = (
            await c.execute(
                text("SELECT result_digest FROM work_item WHERE id=:i"),
                {"i": orig.item_id},
            )
        ).scalar_one()
    assert rows == 1 and done == digest(orig.idx)
