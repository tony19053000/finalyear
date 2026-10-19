"""V0.10 G2 tester r1 (D262, D266, D267): adversarial acceptance of the work-queue
store on real PostgreSQL, derived from the brief's criteria (exactly-once
completion, fencing, <= 2 live leases, speculation rules, failed sites, holder
loss, rollback on a dropped connection, caller clock, extreme sizes)."""

# ruff: noqa: B023, C408, DTZ001, F841, PT012, PT017, RUF059, S110, S311, SIM105
# (test file: closures run before the loop advances, deliberate naive datetime, seeded rng)
from __future__ import annotations

import asyncio
import datetime as dt
import random
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_workqueue import Db, at, digest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from meshforge.common.errors import FailureType
from meshforge.workqueue import store
from meshforge.workqueue.core import (
    AlreadyDone,
    Committed,
    Fenced,
    FenceReason,
    ItemFailed,
    QueuePolicy,
    QueueState,
    Renewed,
    Retried,
    SpeculationRefusal,
)
from meshforge.workqueue.tables import work_item, work_lease, work_queue

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

P = QueuePolicy(visibility_timeout_s=30, max_attempts=3, max_failed_sites=2)


async def mk(
    db: Db,
    n: int = 5,
    *,
    owner: str = "o",
    policy: QueuePolicy = P,
    payloads: list[Any] | None = None,
) -> int:
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s,
            kind="t",
            owner_id=owner,
            payloads=payloads if payloads is not None else [{"i": i} for i in range(n)],
            policy=policy,
        )
    return info.id


async def lease(
    db: Db, q: int, holder: str, *, k: int = 1, now: Any = None, site: str | None = None
) -> list[store.Grant]:
    async with db.sessions() as s, s.begin():
        return await store.lease_next(
            s, q, holder=holder, site=site, k=k, now=now or at(0)
        )


async def comp(
    db: Db,
    g: store.Grant,
    *,
    now: Any = None,
    holder: str | None = None,
    d: str | None = None,
    seq: int | None = None,
) -> store.CompleteResult:
    async with db.sessions() as s, s.begin():
        return await store.complete(
            s,
            item_id=g.item_id,
            seq=g.seq if seq is None else seq,
            holder=holder or g.holder,
            digest=d or digest(g.idx + 1),
            now=now or at(1),
        )


async def fl(
    db: Db,
    g: store.Grant,
    *,
    now: Any = None,
    retryable: bool = True,
    holder: str | None = None,
    seq: int | None = None,
) -> store.FailResult:
    async with db.sessions() as s, s.begin():
        return await store.fail(
            s,
            item_id=g.item_id,
            seq=g.seq if seq is None else seq,
            holder=holder or g.holder,
            kind="boom",
            now=now or at(1),
            retryable=retryable,
        )


async def expire(db: Db, now: Any) -> store.ExpireResult:
    async with db.sessions() as s, s.begin():
        return await store.expire_due(s, now=now)


async def spec(
    db: Db, item_id: int, holder: str, *, site: str | None = None, now: Any = None
) -> store.Grant | SpeculationRefusal:
    async with db.sessions() as s, s.begin():
        return await store.speculate(
            s, item_id=item_id, holder=holder, site=site, now=now or at(5)
        )


async def cnt(db: Db, q: int) -> store.QueueCounts:
    async with db.sessions() as s:
        return await store.counts(s, q)


async def qinfo(db: Db, q: int) -> store.QueueInfo:
    async with db.sessions() as s:
        info = await store.get_queue(s, q)
    assert info is not None
    return info


async def live_by_item(db: Db, q: int) -> dict[int, int]:
    async with db.sessions() as s:
        rows = (
            await s.execute(
                select(work_lease.c.item_id, func.count())
                .join(work_item, work_item.c.id == work_lease.c.item_id)
                .where(work_item.c.queue_id == q, work_lease.c.state == "live")
                .group_by(work_lease.c.item_id)
            )
        ).all()
    return {r[0]: r[1] for r in rows}


async def check_invariants(db: Db, q: int) -> None:
    """Cross-table consistency that must hold whenever no transaction is open."""
    c = await cnt(db, q)
    assert c.total == c.pending + c.leased + c.done + c.failed
    info = await qinfo(db, q)
    assert info.done_count == c.done
    assert info.item_count == c.total
    live = await live_by_item(db, q)
    assert all(n <= 2 for n in live.values())
    async with db.sessions() as s:
        rows = (
            await s.execute(
                select(
                    work_item.c.id, work_item.c.state, work_item.c.done_lease_seq
                ).where(work_item.c.queue_id == q)
            )
        ).all()
        for r in rows:
            n_live = live.get(r.id, 0)
            if r.state == "leased":
                assert n_live >= 1, f"leased item {r.id} without a live lease"
            else:
                assert n_live == 0, f"{r.state} item {r.id} with live lease"
            done_leases = await s.scalar(
                select(func.count())
                .select_from(work_lease)
                .where(work_lease.c.item_id == r.id, work_lease.c.state == "completed")
            )
            assert done_leases == (1 if r.state == "done" else 0)


# ---------------------------------------------------------------- exactly-once ----


async def test_late_original_races_retry_copy_exactly_one_commit(db: Db) -> None:
    """A's lease expires, B re-leases; A's late result and B's result race."""
    for rnd in range(12):
        q = await mk(db, 1, owner=f"o{rnd}")
        [a] = await lease(db, q, "A", now=at(0))
        await expire(db, at(31))
        [b] = await lease(db, q, "B", now=at(32))
        assert b.seq == a.seq + 1
        ra, rb = await asyncio.gather(
            comp(db, a, now=at(33), d=digest(100)),
            comp(db, b, now=at(33), d=digest(200)),
        )
        kinds = sorted(type(r.outcome).__name__ for r in (ra, rb))
        assert kinds == ["AlreadyDone", "Committed"], (rnd, ra, rb)
        assert sum(r.queue_done for r in (ra, rb)) == 1
        win = next(r for r in (ra, rb) if isinstance(r.outcome, Committed))
        lose = next(r for r in (ra, rb) if isinstance(r.outcome, AlreadyDone))
        assert isinstance(lose.outcome, AlreadyDone)
        assert (await qinfo(db, q)).done_count == 1
        async with db.sessions() as s:
            dg = await s.scalar(
                select(work_item.c.result_digest).where(work_item.c.queue_id == q)
            )
        assert isinstance(win.outcome, Committed) and dg == win.outcome.digest
        await check_invariants(db, q)


async def test_many_concurrent_duplicate_completes_of_same_lease(db: Db) -> None:
    q = await mk(db, 1)
    [g] = await lease(db, q, "A")
    res = await asyncio.gather(*[comp(db, g) for _ in range(12)])
    assert sum(isinstance(r.outcome, Committed) for r in res) == 1
    assert sum(r.queue_done for r in res) == 1
    assert all(isinstance(r.outcome, (Committed, AlreadyDone)) for r in res)
    assert (await qinfo(db, q)).done_count == 1


async def test_late_speculative_loser_after_queue_done_is_acked_not_fenced(
    db: Db,
) -> None:
    q = await mk(db, 2, policy=QueuePolicy(30, 3, 2, speculative_max_frac=1.0))
    g0, g1 = await lease(db, q, "A", k=2)
    cp = await spec(db, g0.item_id, "B")
    assert isinstance(cp, store.Grant) and cp.speculative
    assert isinstance((await comp(db, g1, now=at(6))).outcome, Committed)
    r = await comp(db, g0, now=at(7))
    assert isinstance(r.outcome, Committed) and r.queue_done
    assert {x.seq for x in r.outcome.superseded} == {cp.seq}
    late = await comp(db, cp, now=at(8), d=digest(999))
    assert isinstance(late.outcome, AlreadyDone) and not late.outcome.own
    assert late.outcome.winner_digest == digest(g0.idx + 1)
    assert not late.queue_done
    await check_invariants(db, q)


async def test_wrong_holder_never_learns_winner_digest(db: Db) -> None:
    q = await mk(db, 1)
    [g] = await lease(db, q, "A")
    await comp(db, g)
    r = await comp(db, g, holder="mallory")
    assert (
        isinstance(r.outcome, Fenced) and r.outcome.reason is FenceReason.WRONG_HOLDER
    )


async def test_complete_replay_is_own_alreadydone_and_queue_done_only_once(
    db: Db,
) -> None:
    q = await mk(db, 1)
    [g] = await lease(db, q, "A")
    first = await comp(db, g)
    assert first.queue_done
    again = await comp(db, g)
    assert (
        isinstance(again.outcome, AlreadyDone)
        and again.outcome.own
        and not again.queue_done
    )


# ---------------------------------------------------------------- fencing ----


async def test_fencing_matrix(db: Db) -> None:
    q = await mk(db, 6)
    gs = await lease(db, q, "A", k=6)
    unknown, wrong = gs[0], gs[1]
    # stale / unknown seq
    r = await comp(db, unknown, seq=unknown.seq + 7)
    assert (
        isinstance(r.outcome, Fenced) and r.outcome.reason is FenceReason.UNKNOWN_LEASE
    )
    # unknown item id
    async with db.sessions() as s, s.begin():
        r2 = await store.complete(
            s, item_id=10**9, seq=1, holder="A", digest="d", now=at(1)
        )
    assert (
        isinstance(r2.outcome, Fenced)
        and r2.outcome.reason is FenceReason.UNKNOWN_LEASE
    )
    assert not r2.queue_done
    # wrong holder, on complete, fail, renew, release
    r = await comp(db, wrong, holder="B")
    assert (
        isinstance(r.outcome, Fenced) and r.outcome.reason is FenceReason.WRONG_HOLDER
    )
    f = await fl(db, wrong, holder="B")
    assert (
        isinstance(f.outcome, Fenced) and f.outcome.reason is FenceReason.WRONG_HOLDER
    )
    async with db.sessions() as s, s.begin():
        [rn] = await store.renew(
            s, holder="B", leases=[(wrong.item_id, wrong.seq)], now=at(2)
        )
        assert isinstance(rn, Fenced)
        rel = await store.release(
            s, item_id=wrong.item_id, seq=wrong.seq, holder="B", now=at(2)
        )
        assert rel is None
    # the lease is untouched
    assert (await cnt(db, q)).leased == 6 and (await cnt(db, q)).live_leases == 6
    # a failed lease cannot complete later
    f = await fl(db, gs[2])
    assert isinstance(f.outcome, Retried)
    r = await comp(db, gs[2], now=at(3))
    assert (
        isinstance(r.outcome, Fenced) and r.outcome.reason is FenceReason.LEASE_FAILED
    )
    # renew after fail is fenced
    async with db.sessions() as s, s.begin():
        [rn] = await store.renew(
            s, holder="A", leases=[(gs[2].item_id, gs[2].seq)], now=at(3)
        )
    assert isinstance(rn, Fenced)
    await check_invariants(db, q)


async def test_results_after_cancel_and_after_queue_failed_are_fenced(db: Db) -> None:
    q = await mk(db, 3)
    gs = await lease(db, q, "A", k=3)
    async with db.sessions() as s, s.begin():
        released = await store.cancel(s, q, now=at(1))
    assert {x.seq for x in released} == {g.seq for g in gs} and len(released) == 3
    for g in gs:
        r = await comp(db, g, now=at(2))
        assert (
            isinstance(r.outcome, Fenced)
            and r.outcome.reason is FenceReason.QUEUE_CLOSED
        )
        f = await fl(db, g, now=at(2))
        assert isinstance(f.outcome, Fenced)
    async with db.sessions() as s, s.begin():
        assert await store.cancel(s, q, now=at(3)) == ()  # idempotent
        rn = await store.renew(
            s, holder="A", leases=[(gs[0].item_id, gs[0].seq)], now=at(3)
        )
        assert isinstance(rn[0], Fenced) and rn[0].reason is FenceReason.QUEUE_CLOSED
    assert await lease(db, q, "B") == []
    assert (await qinfo(db, q)).state is QueueState.CANCELLED
    # failed queue
    q2 = await mk(db, 3, owner="o2", policy=QueuePolicy(30, 1, None))
    a, b, _c = await lease(db, q2, "A", k=3)
    f = await fl(db, a)
    assert isinstance(f.outcome, ItemFailed) and f.queue_failed
    assert {x.seq for x in f.released} == {b.seq, _c.seq} or len(f.released) == 2
    r = await comp(db, b, now=at(2))
    assert (
        isinstance(r.outcome, Fenced) and r.outcome.reason is FenceReason.QUEUE_CLOSED
    )
    assert (await qinfo(db, q2)).state is QueueState.FAILED
    assert (await cnt(db, q2)).live_leases == 0
    # a late result for the failed item itself
    r = await comp(db, a, now=at(2))
    assert isinstance(r.outcome, Fenced)


async def test_late_result_after_release_commits_if_item_not_done(db: Db) -> None:
    q = await mk(db, 1)
    [g] = await lease(db, q, "A")
    async with db.sessions() as s, s.begin():
        assert await store.release(
            s, item_id=g.item_id, seq=g.seq, holder="A", now=at(1)
        )
    assert (await cnt(db, q)).pending == 1
    r = await comp(db, g, now=at(2))
    assert isinstance(r.outcome, Committed) and r.outcome.late and r.queue_done
    await check_invariants(db, q)


async def test_late_result_does_not_resurrect_failed_item_or_override_done(
    db: Db,
) -> None:
    q = await mk(db, 2, policy=QueuePolicy(30, 3, 2, speculative_max_frac=1.0))
    a, b = await lease(db, q, "A", k=2)
    await comp(db, a)
    # after done, a re-lease is impossible and a fresh digest from stale copy cannot replace
    r = await comp(db, a, d=digest(777))
    assert isinstance(r.outcome, AlreadyDone) and r.outcome.winner_digest == digest(
        a.idx + 1
    )
    async with db.sessions() as s:
        dg = await s.scalar(
            select(work_item.c.result_digest).where(work_item.c.id == a.item_id)
        )
    assert dg == digest(a.idx + 1)


# ------------------------------------------------- expiry vs completion race ----


async def test_expiry_racing_completion_keeps_consistent_state(db: Db) -> None:
    for rnd in range(25):
        q = await mk(db, 1, owner=f"o{rnd}")
        [g] = await lease(db, q, "A", now=at(0))
        res, exp = await asyncio.gather(comp(db, g, now=at(31)), expire(db, at(31)))
        item_state = None
        async with db.sessions() as s:
            item_state = await s.scalar(
                select(work_item.c.state).where(work_item.c.queue_id == q)
            )
        if isinstance(res.outcome, Committed):
            assert item_state == "done"
        else:
            raise AssertionError(
                f"complete lost a race it must not lose: {res.outcome}"
            )
        assert (await cnt(db, q)).live_leases == 0
        await check_invariants(db, q)


async def test_expire_not_due_clock_going_backwards_is_harmless(db: Db) -> None:
    q = await mk(db, 2)
    gs = await lease(db, q, "A", k=2, now=at(100))
    res = await expire(db, at(0))  # clock earlier than lease time
    assert res.expired == ()
    res = await expire(db, at(129.999))
    assert res.expired == ()
    res = await expire(db, at(130))
    assert len(res.expired) == 2
    assert (await cnt(db, q)).pending == 2
    # renew with an earlier clock never shortens a deadline
    [g] = await lease(db, q, "B", now=at(200))
    async with db.sessions() as s, s.begin():
        [rn] = await store.renew(s, holder="B", leases=[(g.item_id, g.seq)], now=at(0))
    assert isinstance(rn, Renewed) and rn.deadline >= g.deadline
    assert (await expire(db, at(229))).expired == ()
    # complete with clock behind leased_at still works
    r = await comp(db, g, now=at(-50))
    assert isinstance(r.outcome, Committed)
    await check_invariants(db, q)


async def test_expiry_and_renew_race_never_both_win(db: Db) -> None:
    for rnd in range(15):
        q = await mk(db, 1, owner=f"o{rnd}")
        [g] = await lease(db, q, "A", now=at(0))

        async def renew_it() -> Any:
            async with db.sessions() as s, s.begin():
                return await store.renew(
                    s, holder="A", leases=[(g.item_id, g.seq)], now=at(31)
                )

        _, rn = await asyncio.gather(expire(db, at(31)), renew_it())
        c = await cnt(db, q)
        # either the expiry won (item pending, renew fenced) or the renew did not
        # move the deadline into the future before expiry: item is never both
        if isinstance(rn[0], Renewed):
            assert (c.leased, c.pending) in {(1, 0), (0, 1)}
        await check_invariants(db, q)


async def test_expiry_with_two_live_leases_expires_both_independently(db: Db) -> None:
    q = await mk(db, 1, policy=QueuePolicy(30, 3, 2, speculative_max_frac=1.0))
    [a] = await lease(db, q, "A", now=at(0))
    c = await spec(db, a.item_id, "B", now=at(20))
    assert isinstance(c, store.Grant)
    async with db.sessions() as s, s.begin():
        await store.renew(
            s, holder="A", leases=[(a.item_id, a.seq)], now=at(25)
        )  # A -> 55
    res = await expire(db, at(50))  # copy deadline 50 expires, A live
    assert len(res.expired) == 1
    assert (await cnt(db, q)).leased == 1 and (await cnt(db, q)).live_leases == 1
    await check_invariants(db, q)


# ------------------------------------------------------------- close vs pull ----


async def test_cancel_racing_many_pulls_leaves_no_live_lease(db: Db) -> None:
    for rnd in range(6):
        q = await mk(db, 60, owner=f"o{rnd}")

        async def puller(n: int) -> list[store.Grant]:
            out: list[store.Grant] = []
            for _ in range(8):
                out.extend(await lease(db, q, f"p{n}", k=3, now=at(0)))
            return out

        async def closer() -> tuple[Any, ...]:
            await asyncio.sleep(random.random() * 0.05)
            async with db.sessions() as s, s.begin():
                return await store.cancel(s, q, now=at(1))

        *_g, released = await asyncio.gather(*[puller(i) for i in range(8)], closer())
        c = await cnt(db, q)
        assert c.live_leases == 0, (rnd, c)
        assert (await qinfo(db, q)).state is QueueState.CANCELLED
        granted_total = sum(len(x) for x in _g)
        # everything granted before the close was reported released by cancel
        assert len(released) <= granted_total
        async with db.sessions() as s:
            n_released = await s.scalar(
                select(func.count())
                .select_from(work_lease)
                .join(work_item)
                .where(work_item.c.queue_id == q, work_lease.c.state == "released")
            )
        assert n_released == len(released) == granted_total, (
            granted_total,
            len(released),
        )


async def test_failing_item_racing_pulls_closes_cleanly(db: Db) -> None:
    for rnd in range(5):
        q = await mk(db, 40, owner=f"o{rnd}", policy=QueuePolicy(30, 1, None))
        [first] = await lease(db, q, "boss", now=at(0))

        async def puller(n: int) -> int:
            got = 0
            for _ in range(6):
                got += len(await lease(db, q, f"p{n}", k=2, now=at(0)))
            return got

        await asyncio.gather(fl(db, first), *[puller(i) for i in range(6)])
        assert (await qinfo(db, q)).state is QueueState.FAILED
        assert (await cnt(db, q)).live_leases == 0


# ---------------------------------------------------------- <= 2 live leases ----


async def test_concurrent_speculation_issues_at_most_one_copy(db: Db) -> None:
    q = await mk(db, 1, policy=QueuePolicy(30, 3, 2, speculative_max_frac=1.0))
    [g] = await lease(db, q, "A")
    res = await asyncio.gather(*[spec(db, g.item_id, f"h{i}") for i in range(10)])
    grants = [r for r in res if isinstance(r, store.Grant)]
    assert len(grants) == 1
    refusals = {r for r in res if isinstance(r, SpeculationRefusal)}
    assert refusals <= {
        SpeculationRefusal.ALREADY_SPECULATIVE,
        SpeculationRefusal.NOT_ONE_LIVE_LEASE,
    }
    assert (await live_by_item(db, q)) == {g.item_id: 2}
    assert (await qinfo(db, q)).speculated_count == 1


async def test_speculation_rules(db: Db) -> None:
    q = await mk(db, 3, policy=QueuePolicy(30, 3, 2, speculative_max_frac=1.0))
    gs = await lease(db, q, "A", k=2, site="nodeA")
    # pending item left -> refused
    r = await spec(db, gs[0].item_id, "B")
    assert r is SpeculationRefusal.PENDING_LEFT
    [last] = await lease(db, q, "A", site="nodeA")
    assert (
        await spec(db, gs[0].item_id, "A", site="other")
        is SpeculationRefusal.SAME_HOLDER
    )
    assert (
        await spec(db, gs[0].item_id, "B", site="nodeA") is SpeculationRefusal.SAME_SITE
    )
    async with db.sessions() as s:
        cands = await store.speculation_candidates(s, q, holder="A", site="nodeA")
        assert cands == []
        cands = await store.speculation_candidates(s, q, holder="B", site="nodeB")
        assert {c.id for c in cands} == {g.item_id for g in (*gs, last)}
    cp = await spec(db, gs[0].item_id, "B", site="nodeB")
    assert isinstance(cp, store.Grant) and cp.speculative and cp.site == "nodeB"
    third = await spec(
        db, gs[0].item_id, "C", site="nodeC"
    )  # a 3rd live lease: refused
    assert third in (
        SpeculationRefusal.ALREADY_SPECULATIVE,
        SpeculationRefusal.NOT_ONE_LIVE_LEASE,
    )
    assert (await live_by_item(db, q))[gs[0].item_id] == 2
    assert await spec(db, 10**9, "C") is SpeculationRefusal.UNKNOWN_ITEM
    # done item
    await comp(db, gs[1])
    assert await spec(db, gs[1].item_id, "C") is SpeculationRefusal.NOT_LEASED
    await check_invariants(db, q)


async def test_speculation_budget_is_ceil_fraction_of_items(db: Db) -> None:
    q = await mk(
        db, 25, policy=QueuePolicy(30, 3, 2, speculative_max_frac=0.10)
    )  # ceil(2.5)=3
    gs = [x for _ in range(25) for x in await lease(db, q, "A")]
    assert len(gs) == 25
    ok = []
    for g in gs:
        r = await spec(db, g.item_id, "B", site="nb")
        if isinstance(r, store.Grant):
            ok.append(r)
        else:
            assert r is SpeculationRefusal.BUDGET_SPENT
    assert len(ok) == 3
    assert (await qinfo(db, q)).speculated_count == 3
    # zero fraction -> never
    q0 = await mk(
        db, 2, owner="z", policy=QueuePolicy(30, 3, 2, speculative_max_frac=0.0)
    )
    gz = await lease(db, q0, "A", k=2)
    assert await spec(db, gz[0].item_id, "B") is SpeculationRefusal.BUDGET_SPENT


async def test_speculation_on_closed_queue_refused(db: Db) -> None:
    q = await mk(db, 1, policy=QueuePolicy(30, 3, 2, speculative_max_frac=1.0))
    [g] = await lease(db, q, "A")
    async with db.sessions() as s, s.begin():
        await store.cancel(s, q, now=at(1))
    assert await spec(db, g.item_id, "B") is SpeculationRefusal.QUEUE_CLOSED


# -------------------------------------------------------------- failed sites ----


async def test_failed_sites_distinct_and_preference(db: Db) -> None:
    q = await mk(db, 2, policy=QueuePolicy(30, 10, 2))
    [g] = await lease(db, q, "h1", site="s1")
    assert g.idx == 0
    f = await fl(db, g)
    assert isinstance(f.outcome, Retried) and f.outcome.requeued
    # s1 pulling again gets idx 1 first (not the one it failed)
    [h] = await lease(db, q, "h1", site="s1")
    assert h.idx == 1
    # nothing else pending except the item s1 failed: s1 may still get it (last resort)
    [again] = await lease(db, q, "h1b", site="s1")
    assert again.idx == 0
    # the same SITE failing twice counts once -> queue still open
    f = await fl(db, again)
    assert isinstance(f.outcome, Retried)
    assert (await qinfo(db, q)).state is QueueState.OPEN
    # a different site failing -> 2 distinct sites -> item failed -> queue failed
    [other] = await lease(db, q, "h2", site="s2")
    assert other.idx == 0
    f = await fl(db, other)
    assert isinstance(f.outcome, ItemFailed) and f.queue_failed
    assert (await qinfo(db, q)).state is QueueState.FAILED
    assert (await cnt(db, q)).live_leases == 0


async def test_max_attempts_and_non_retryable(db: Db) -> None:
    q = await mk(db, 1, policy=QueuePolicy(30, 3, None))
    for i in range(2):
        [g] = await lease(db, q, f"h{i}", now=at(i))
        assert isinstance((await fl(db, g)).outcome, Retried)
    [g] = await lease(db, q, "h9", now=at(5))
    f = await fl(db, g)
    assert isinstance(f.outcome, ItemFailed) and f.queue_failed
    q2 = await mk(db, 1, owner="o2", policy=QueuePolicy(30, 3, None))
    [g] = await lease(db, q2, "h")
    f = await fl(db, g, retryable=False)
    assert isinstance(f.outcome, ItemFailed)


async def test_expiry_does_not_count_attempts_by_default(db: Db) -> None:
    q = await mk(db, 1, policy=QueuePolicy(30, 1, None))
    for i in range(6):
        [g] = await lease(db, q, f"h{i}", now=at(i * 100))
        await expire(db, at(i * 100 + 31))
    assert (await qinfo(db, q)).state is QueueState.OPEN
    q2 = await mk(db, 1, owner="c", policy=QueuePolicy(30, 2, None, count_expiry=True))
    for i in range(2):
        await lease(db, q2, f"h{i}", now=at(i * 100))
        res = await expire(db, at(i * 100 + 31))
    assert res.failed_queues == (q2,)
    assert (await qinfo(db, q2)).state is QueueState.FAILED


# ----------------------------------------------------------- release_holder ----


async def test_release_holder_requeues_across_queues_and_not_attempts(db: Db) -> None:
    q1 = await mk(db, 3, owner="a", policy=QueuePolicy(30, 1, None))
    q2 = await mk(db, 3, owner="b", policy=QueuePolicy(30, 1, None))
    await lease(db, q1, "dead", k=2)
    await lease(db, q2, "dead", k=1)
    await lease(db, q1, "alive", k=1)
    async with db.sessions() as s, s.begin():
        rel = await store.release_holder(s, "dead", now=at(2))
    assert len(rel) == 3
    c1, c2 = await cnt(db, q1), await cnt(db, q2)
    assert (c1.pending, c1.leased, c2.pending, c2.leased) == (2, 1, 3, 0)
    # max_attempts=1 yet the queue is still open: release does not count
    assert (await qinfo(db, q1)).state is QueueState.OPEN
    async with db.sessions() as s, s.begin():
        assert await store.release_holder(s, "dead", now=at(3)) == ()
        assert await store.release_holder(s, "nobody", now=at(3)) == ()
    await check_invariants(db, q1)
    await check_invariants(db, q2)


async def test_release_holder_keeps_other_copy_live(db: Db) -> None:
    q = await mk(db, 1, policy=QueuePolicy(30, 3, 2, speculative_max_frac=1.0))
    [a] = await lease(db, q, "A")
    cp = await spec(db, a.item_id, "B")
    assert isinstance(cp, store.Grant)
    async with db.sessions() as s, s.begin():
        await store.release_holder(s, "A", now=at(6))
    c = await cnt(db, q)
    assert (c.leased, c.pending, c.live_leases) == (1, 0, 1)
    await check_invariants(db, q)


async def test_release_holder_racing_complete_and_pull(db: Db) -> None:
    for rnd in range(10):
        q = await mk(db, 20, owner=f"o{rnd}")
        gs = await lease(db, q, "dead", k=10)

        async def rh() -> Any:
            async with db.sessions() as s, s.begin():
                return await store.release_holder(s, "dead", now=at(2))

        async def completer() -> list[store.CompleteResult]:
            return [await comp(db, g, now=at(2)) for g in gs]

        async def puller() -> None:
            for _ in range(5):
                await lease(db, q, "other", k=3, now=at(2))

        await asyncio.gather(rh(), completer(), puller())
        await check_invariants(db, q)


async def test_rollback_of_release_holder_keeps_leases(db: Db) -> None:
    q = await mk(db, 3)
    await lease(db, q, "A", k=3)
    with pytest.raises(RuntimeError):
        async with db.sessions() as s, s.begin():
            await store.release_holder(s, "A", now=at(1))
            raise RuntimeError("caller aborts")
    assert (await cnt(db, q)).live_leases == 3
    await check_invariants(db, q)


# ------------------------------------------------- dropped connection mid-txn ----


async def _backend_pid(s: Any) -> int:
    return int(await s.scalar(text("select pg_backend_pid()")))


async def _kill(db: Db, pid: int) -> None:
    async with db.engine.connect() as c:
        await c.execute(text("select pg_terminate_backend(:p)"), {"p": pid})


async def test_connection_dropped_mid_lease_and_complete_leaves_consistent_state(
    db: Db,
) -> None:
    q = await mk(db, 3)
    # (1) drop after a lease_next, before commit
    s = db.sessions()
    await s.__aenter__()
    try:
        await s.begin()
        pid = await _backend_pid(s)
        got = await store.lease_next(s, q, holder="A", k=2, now=at(0))
        assert len(got) == 2
        await _kill(db, pid)
        with pytest.raises(DBAPIError):
            await s.commit()
    finally:
        await s.close()
    c = await cnt(db, q)
    assert (c.pending, c.leased, c.live_leases) == (3, 0, 0)
    # (2) drop after complete's statements, before commit; lease remains and can be redone
    [g] = await lease(db, q, "A", now=at(1))
    s = db.sessions()
    await s.__aenter__()
    try:
        await s.begin()
        pid = await _backend_pid(s)
        r = await store.complete(
            s, item_id=g.item_id, seq=g.seq, holder="A", digest=digest(1), now=at(2)
        )
        assert isinstance(r.outcome, Committed)
        await _kill(db, pid)
        with pytest.raises(DBAPIError):
            await s.flush()
            await s.commit()
    finally:
        await s.close()
    c = await cnt(db, q)
    assert (c.done, c.leased, c.live_leases) == (0, 1, 1)
    assert (await qinfo(db, q)).done_count == 0
    r2 = await comp(db, g, now=at(3))  # retry after the lost transaction
    assert isinstance(r2.outcome, Committed)
    await check_invariants(db, q)


async def test_connection_dropped_while_another_holds_row_locks_does_not_wedge(
    db: Db,
) -> None:
    """A puller killed holding the queue lock must not block a closer forever, and
    its rolled-back items become visible again."""
    q = await mk(db, 4)
    [c0] = await lease(db, q, "C", now=at(0))
    s = db.sessions()
    await s.__aenter__()
    pid = 0
    try:
        await s.begin()
        pid = await _backend_pid(s)
        assert len(await store.lease_next(s, q, holder="A", k=2, now=at(0))) == 2
        # a concurrent pull skips A's locked rows and does not wait
        assert len(await asyncio.wait_for(lease(db, q, "B", k=4, now=at(0)), 10)) == 1
        closer = asyncio.create_task(fl(db, c0, now=at(1)))  # FOR UPDATE: waits for A
        await asyncio.sleep(0.5)
        assert not closer.done()
        await _kill(db, pid)
        res = await asyncio.wait_for(closer, 20)
        assert isinstance(res.outcome, Retried)
    finally:
        try:
            await s.close()
        except Exception:  # noqa: BLE001 - the connection was killed on purpose
            pass
    got = await lease(db, q, "D", k=4, now=at(2))
    assert len(got) == 3  # A's two rolled-back items + C's retried item; B holds one
    await check_invariants(db, q)


# -------------------------------------------------------- extreme sizes/inputs ----


async def test_one_item_queue_full_life(db: Db) -> None:
    q = await mk(db, 1)
    [g] = await lease(db, q, "A")
    assert await lease(db, q, "B") == []
    r = await comp(db, g)
    assert isinstance(r.outcome, Committed) and r.queue_done
    assert (await qinfo(db, q)).state is QueueState.DONE
    assert await lease(db, q, "B") == []
    assert (await cnt(db, q)) == store.QueueCounts(1, 0, 0, 1, 0, 0, 0)


async def test_zero_items_and_bad_arguments_rejected(db: Db) -> None:
    with pytest.raises(store.WorkQueueError) as e:
        await mk(db, 0, payloads=[])
    assert e.value.failure_type is FailureType.INVALID_INPUT
    q = await mk(db, 1)
    naive = dt.datetime(2026, 1, 1)
    for bad in (
        dict(k=0),
        dict(k=65),
        dict(k=-1),
    ):
        async with db.sessions() as s, s.begin():
            with pytest.raises(store.WorkQueueError):
                await store.lease_next(s, q, holder="A", now=at(0), **bad)  # type: ignore[arg-type]
    async with db.sessions() as s, s.begin():
        with pytest.raises(store.WorkQueueError):
            await store.lease_next(s, q, holder="A", now=naive)
        for tmo in (0.0, -1.0, float("nan"), float("inf")):
            with pytest.raises(store.WorkQueueError):
                await store.lease_next(s, q, holder="A", now=at(0), timeout_s=tmo)
        with pytest.raises(store.WorkQueueError):
            await store.lease_next(s, q, holder="", now=at(0))
        with pytest.raises(store.WorkQueueError):
            await store.lease_next(s, q, holder="h" * 5000, now=at(0))
        with pytest.raises(store.WorkQueueError) as e:
            await store.lease_next(s, 10**9, holder="A", now=at(0))
        assert e.value.failure_type is FailureType.NOT_FOUND
        with pytest.raises(store.WorkQueueError):
            await store.complete(s, item_id=1, seq=1, holder="A", digest="", now=at(0))
        with pytest.raises(store.WorkQueueError):
            await store.complete(
                s, item_id=1, seq=1, holder="A", digest="x" * 5000, now=at(0)
            )
        with pytest.raises(store.WorkQueueError):
            await store.expire_due(s, now=at(0), limit=0)
    assert (await cnt(db, q)).live_leases == 0


async def test_huge_payload_and_unicode_roundtrip(db: Db) -> None:
    big = {"blob": "é" * 3_000_000, "n": list(range(1000))}
    uni = {
        "path": "C:\\Users\\Zoë\\данные\\日本語 \U0001f600\\x.txt",
        "nested": [None, True, 1.5, {"": ""}],
    }
    q = await mk(db, payloads=[big, uni, "plain", 7, None, [], {}])
    got = await lease(db, q, "A", k=7)
    assert [g.payload for g in got] == [big, uni, "plain", 7, None, [], {}]


async def test_nul_character_in_payload_is_a_clean_error_not_a_corrupt_queue(
    db: Db,
) -> None:
    """jsonb cannot hold U+0000. Whatever happens, no half-created queue remains."""
    try:
        await mk(db, payloads=[{"a": 1}, {"a": "x\u0000y"}])
    except Exception as exc:  # noqa: BLE001 - classification is the point
        name = type(exc).__name__
        async with db.sessions() as s:
            left = await s.scalar(select(func.count()).select_from(work_queue))
        assert left == 0, "failed create_queue left a queue row behind"
        # record how it surfaces (WorkQueueError is the desirable contract)
        assert isinstance(exc, store.WorkQueueError), (
            f"raw {name} leaks from create_queue"
        )
    else:
        raise AssertionError("NUL payload unexpectedly accepted")


async def test_max_queue_items_bound(db: Db) -> None:
    with pytest.raises(store.WorkQueueError):
        async with db.sessions() as s, s.begin():
            await store.create_queue(
                s,
                kind="t",
                owner_id="big",
                payloads=[0] * (store.MAX_QUEUE_ITEMS + 1),
                policy=P,
            )


async def test_duplicate_owner_queue_concurrent_create(db: Db) -> None:
    async def one() -> str:
        try:
            await mk(db, 2, owner="same")
            return "ok"
        except store.WorkQueueError as e:
            return (
                e.failure_type.value
                if hasattr(e.failure_type, "value")
                else str(e.failure_type)
            )
        except Exception as e:  # noqa: BLE001
            return type(e).__name__

    res = await asyncio.gather(*[one() for _ in range(6)])
    assert res.count("ok") == 1, res
    async with db.sessions() as s:
        assert await s.scalar(select(func.count()).select_from(work_queue)) == 1
    others = {r for r in res if r != "ok"}
    assert others == {"conflict"}, res  # D-G2-2 (fixed: ON CONFLICT DO NOTHING)


# -------------------------------------------------------------- counts / soak ----


async def test_counts_consistency_under_randomised_concurrent_workload(db: Db) -> None:
    rng = random.Random(20261019)
    q = await mk(db, 300, policy=QueuePolicy(5, 50, None, speculative_max_frac=0.2))
    clock = {"t": 0.0}
    stop = asyncio.Event()

    async def worker(name: str) -> None:
        while not stop.is_set():
            now = at(clock["t"])
            gs = await lease(db, q, name, k=rng.randint(1, 4), now=now, site=name)
            if not gs:
                await asyncio.sleep(0.01)
                continue
            for g in gs:
                x = rng.random()
                if x < 0.6:
                    await comp(db, g, now=at(clock["t"]))
                elif x < 0.7:
                    await fl(db, g, now=at(clock["t"]))
                elif x < 0.8:
                    async with db.sessions() as s, s.begin():
                        await store.release(
                            s,
                            item_id=g.item_id,
                            seq=g.seq,
                            holder=name,
                            now=at(clock["t"]),
                        )
                # else: abandon -> expiry

    async def sweeper() -> None:
        while not stop.is_set():
            clock["t"] += 2.0
            await expire(db, at(clock["t"]))
            await asyncio.sleep(0.02)

    async def watcher() -> None:
        while not stop.is_set():
            async with db.sessions() as s:
                # one snapshot: counts() must be self-consistent when read in a txn
                await s.connection(
                    execution_options={"isolation_level": "REPEATABLE READ"}
                )
                c = await store.counts(s, q)
            assert c.total == 300 == c.pending + c.leased + c.done + c.failed
            assert c.live_leases <= 2 * c.leased and c.live_leases >= c.leased
            assert c.speculative_live <= c.live_leases
            await asyncio.sleep(0.05)

    async def done_watch() -> None:
        while True:
            if (await qinfo(db, q)).state is not QueueState.OPEN:
                stop.set()
                return
            await asyncio.sleep(0.1)

    async def spec_loop() -> None:
        while not stop.is_set():
            async with db.sessions() as s:
                cands = await store.speculation_candidates(
                    s, q, holder="spec", site="spec"
                )
            for it in cands[:2]:
                await spec(db, it.id, "spec", site="spec", now=at(clock["t"]))
            await asyncio.sleep(0.05)

    await asyncio.wait_for(
        asyncio.gather(
            done_watch(),
            sweeper(),
            watcher(),
            spec_loop(),
            *[worker(f"w{i}") for i in range(8)],
        ),
        240,
    )
    info = await qinfo(db, q)
    if info.state is QueueState.DONE:
        assert (await cnt(db, q)).done == 300
    await check_invariants(db, q)


# ------------------------------------------------- Lead fix: renew lock order ----


async def test_renew_across_two_queues_does_not_deadlock_with_expire_due(
    db: Db,
) -> None:
    """Reviewer r1 item 1: renew spanning queues must lock queues ascending."""
    now = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    async with db.sessions() as s:
        q1 = await store.create_queue(
            s,
            kind="t",
            owner_id="rq1",
            payloads=[1] * 3,
            policy=QueuePolicy(visibility_timeout_s=60),
        )
        q2 = await store.create_queue(
            s,
            kind="t",
            owner_id="rq2",
            payloads=[1] * 3,
            policy=QueuePolicy(visibility_timeout_s=60),
        )
        l1 = await store.lease_next(s, q1.id, holder="h", now=now, k=3)
        l2 = await store.lease_next(s, q2.id, holder="h", now=now, k=3)
        await s.commit()
    # Highest item id first: q2's items were created after q1's.
    keys = [(x.item_id, x.seq) for x in (*l2, *l1)]
    later = now + dt.timedelta(seconds=1)
    for _ in range(10):

        async def ren() -> None:
            async with db.sessions() as s:
                await store.renew(s, holder="h", leases=keys, now=later)
                await asyncio.sleep(0.01)
                await s.commit()

        async def exp() -> None:
            async with db.sessions() as s:
                await store.expire_due(s, now=later)
                await asyncio.sleep(0.01)
                await s.commit()

        await asyncio.wait_for(asyncio.gather(ren(), exp()), 30)
