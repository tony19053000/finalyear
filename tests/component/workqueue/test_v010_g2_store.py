"""V0.10 G2 (D262): the work-queue store on real PostgreSQL - SKIP LOCKED
leasing, visibility timeout, lease-number fencing, idempotent completion, at
most two live leases, release on holder loss in the caller's transaction,
failure, cancel and restart safety. Nothing is mocked."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_workqueue import Db, at, digest, open_db
from sqlalchemy import func, insert, select, text
from sqlalchemy.exc import IntegrityError

from meshforge.common.errors import FailureType
from meshforge.controller.store.settings import DatabaseSettings
from meshforge.workqueue import store
from meshforge.workqueue.core import (
    AlreadyDone,
    Committed,
    Fenced,
    FenceReason,
    ItemFailed,
    LeaseState,
    QueuePolicy,
    QueueState,
    Renewed,
    Retried,
    SpeculationRefusal,
)
from meshforge.workqueue.tables import work_lease

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

POLICY = QueuePolicy(visibility_timeout_s=30, max_attempts=3, max_failed_sites=2)


async def _queue(
    db: Db, n: int = 5, *, owner: str = "owner-1", policy: QueuePolicy = POLICY
) -> int:
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s,
            kind="test",
            owner_id=owner,
            payloads=[{"i": i} for i in range(n)],
            policy=policy,
        )
    return info.id


async def _lease(
    db: Db,
    qid: int,
    holder: str,
    *,
    k: int = 1,
    now: Any = None,
    site: str | None = None,
) -> list[store.Grant]:
    async with db.sessions() as s, s.begin():
        return await store.lease_next(
            s, qid, holder=holder, site=site, k=k, now=now or at(0)
        )


async def _complete(
    db: Db,
    g: store.Grant,
    *,
    holder: str | None = None,
    d: str | None = None,
    now: Any = None,
) -> store.CompleteResult:
    async with db.sessions() as s, s.begin():
        return await store.complete(
            s,
            item_id=g.item_id,
            seq=g.seq,
            holder=holder or g.holder,
            digest=d or digest(g.idx),
            now=now or at(1),
        )


async def _counts(db: Db, qid: int) -> store.QueueCounts:
    async with db.sessions() as s:
        return await store.counts(s, qid)


# --- creation ---------------------------------------------------------------------


async def test_create_queue_stores_items_in_order_and_refuses_a_second_owner_queue(
    db: Db,
) -> None:
    qid = await _queue(db, 3)
    async with db.sessions() as s:
        info = await store.find_queue(s, kind="test", owner_id="owner-1")
        assert info is not None and info.id == qid
        assert info.state is QueueState.OPEN and info.item_count == 3
        assert info.policy == POLICY
        assert await store.get_queue(s, qid) == info
        assert await store.get_queue(s, qid + 999) is None
    assert await _counts(db, qid) == store.QueueCounts(3, 3, 0, 0, 0, 0, 0)
    with pytest.raises(store.WorkQueueError) as err:
        await _queue(db, 1)
    assert err.value.failure_type is FailureType.CONFLICT


@pytest.mark.parametrize(
    ("kind", "owner", "n"),
    [("", "o", 1), ("k" * 33, "o", 1), ("k", "", 1), ("k", "o" * 65, 1), ("k", "o", 0)],
)
async def test_create_queue_refuses_bad_arguments(
    db: Db, kind: str, owner: str, n: int
) -> None:
    async with db.sessions() as s, s.begin():
        with pytest.raises(store.WorkQueueError) as err:
            await store.create_queue(
                s, kind=kind, owner_id=owner, payloads=[1] * n, policy=POLICY
            )
    assert err.value.failure_type is FailureType.INVALID_INPUT


async def test_bad_arguments_of_operations(db: Db) -> None:
    qid = await _queue(db, 1)
    async with db.sessions() as s, s.begin():
        with pytest.raises(store.WorkQueueError):
            await store.lease_next(s, qid, holder="h", k=0, now=at(0))
        with pytest.raises(store.WorkQueueError):
            await store.lease_next(
                s, qid, holder="h", k=1, now=at(0).replace(tzinfo=None)
            )
        with pytest.raises(store.WorkQueueError) as err:
            await store.lease_next(s, qid + 1, holder="h", now=at(0))
        assert err.value.failure_type is FailureType.NOT_FOUND


# --- leasing ----------------------------------------------------------------------


async def test_lease_next_hands_out_items_in_idx_order_with_payloads(db: Db) -> None:
    qid = await _queue(db, 5)
    first = await _lease(db, qid, "h1", k=2)
    second = await _lease(db, qid, "h2", k=1)
    assert [g.idx for g in first] == [0, 1]
    assert [g.idx for g in second] == [2]
    assert first[0].payload == {"i": 0} and first[0].seq == 1
    assert first[0].site == "h1"  # site defaults to the holder
    assert first[0].deadline == at(30) and not first[0].speculative
    c = await _counts(db, qid)
    assert (c.pending, c.leased, c.live_leases) == (2, 3, 3)


async def test_skip_locked_concurrent_pullers_never_wait_or_share(db: Db) -> None:
    """Puller A holds its lease transaction open; puller B is not blocked by
    A's locked item and gets the next one."""
    qid = await _queue(db, 3)
    async with db.sessions() as a, a.begin():
        got_a = await store.lease_next(a, qid, holder="a", now=at(0))
        async with db.sessions() as b, b.begin():
            got_b = await asyncio.wait_for(
                store.lease_next(b, qid, holder="b", now=at(0)), timeout=5
            )
    assert [g.idx for g in got_a] == [0]
    assert [g.idx for g in got_b] == [1]


async def test_lease_next_prefers_items_that_did_not_fail_at_this_site(db: Db) -> None:
    qid = await _queue(db, 2)
    (g,) = await _lease(db, qid, "h1", site="n1")
    async with db.sessions() as s, s.begin():
        out = await store.fail(
            s, item_id=g.item_id, seq=g.seq, holder="h1", kind="oom", now=at(1)
        )
    assert isinstance(out.outcome, Retried) and out.outcome.requeued
    again = await _lease(db, qid, "h1b", site="n1", k=2)
    assert [x.idx for x in again] == [1, 0]  # item 0 failed at n1: last
    assert again[1].seq == 2


# --- completion -------------------------------------------------------------------


async def test_complete_is_idempotent_first_result_wins(db: Db) -> None:
    qid = await _queue(db, 2)
    (g,) = await _lease(db, qid, "h1")
    first = await _complete(db, g, d=digest(7))
    assert isinstance(first.outcome, Committed) and not first.queue_done
    replay = await _complete(db, g, d=digest(8))
    assert replay.outcome == AlreadyDone(g.item_id, g.seq, g.seq, digest(7))
    async with db.sessions() as s:
        n = await s.scalar(
            select(func.count())
            .select_from(work_lease)
            .where(work_lease.c.state == LeaseState.COMPLETED.value)
        )
        stored = await s.scalar(
            text("SELECT result_digest FROM work_item WHERE id = :i"), {"i": g.item_id}
        )
    assert n == 1 and stored == digest(7)


async def test_last_commit_reports_queue_done(db: Db) -> None:
    qid = await _queue(db, 2)
    grants = await _lease(db, qid, "h1", k=2)
    assert not (await _complete(db, grants[1])).queue_done
    last = await _complete(db, grants[0])
    assert last.queue_done
    async with db.sessions() as s:
        info = await store.get_queue(s, qid)
    assert info is not None and info.state is QueueState.DONE and info.done_count == 2
    assert info.closed_at == at(1)
    assert await _lease(db, qid, "h2") == []


async def test_fencing_by_holder_and_lease_number(db: Db) -> None:
    qid = await _queue(db, 1)
    (g,) = await _lease(db, qid, "h1")
    async with db.sessions() as s, s.begin():
        wrong = await store.complete(
            s,
            item_id=g.item_id,
            seq=g.seq,
            holder="intruder",
            digest=digest(1),
            now=at(1),
        )
        unknown = await store.complete(
            s,
            item_id=g.item_id,
            seq=g.seq + 1,
            holder="h1",
            digest=digest(1),
            now=at(1),
        )
        nothing = await store.complete(
            s, item_id=g.item_id + 999, seq=1, holder="h1", digest=digest(1), now=at(1)
        )
    assert wrong.outcome == Fenced(g.item_id, g.seq, FenceReason.WRONG_HOLDER)
    assert unknown.outcome == Fenced(g.item_id, g.seq + 1, FenceReason.UNKNOWN_LEASE)
    assert nothing.outcome == Fenced(g.item_id + 999, 1, FenceReason.UNKNOWN_LEASE)
    assert nothing.queue_id is None
    assert (await _counts(db, qid)).done == 0


async def test_concurrent_completions_commit_exactly_one(db: Db) -> None:
    """Original and speculative copy finish at the same moment in two parallel
    transactions: one Committed, the other AlreadyDone with the winner's digest."""
    for round_no in range(10):
        qid = await _queue(db, 1, owner=f"race-{round_no}")
        (orig,) = await _lease(db, qid, "h1")
        async with db.sessions() as s, s.begin():
            copy = await store.speculate(
                s, item_id=orig.item_id, holder="h2", now=at(1)
            )
        assert isinstance(copy, store.Grant) and copy.speculative and copy.seq == 2
        results = await asyncio.gather(
            _complete(db, orig, d=digest(1)), _complete(db, copy, d=digest(2))
        )
        committed = [r.outcome for r in results if isinstance(r.outcome, Committed)]
        already = [r.outcome for r in results if isinstance(r.outcome, AlreadyDone)]
        assert len(committed) == 1 and len(already) == 1
        assert already[0].winner_digest == committed[0].digest
        assert already[0].winner_seq == committed[0].seq
        assert sum(r.queue_done for r in results) == 1
        c = await _counts(db, qid)
        assert (c.done, c.live_leases) == (1, 0)


# --- visibility timeout -----------------------------------------------------------


async def test_expiry_requeues_and_a_late_result_still_commits_once(db: Db) -> None:
    qid = await _queue(db, 1)
    (old,) = await _lease(db, qid, "h1")
    async with db.sessions() as s, s.begin():
        early = await store.expire_due(s, now=at(29))
    assert early.expired == ()
    async with db.sessions() as s, s.begin():
        due = await store.expire_due(s, now=at(30))
    assert [e.item_id for e in due.expired] == [old.item_id]
    assert (await _counts(db, qid)).pending == 1
    async with db.sessions() as s, s.begin():
        renewed = await store.renew(
            s, holder="h1", leases=[(old.item_id, old.seq)], now=at(31)
        )
    assert renewed == [Fenced(old.item_id, old.seq, FenceReason.LEASE_NOT_LIVE)]
    (new,) = await _lease(db, qid, "h2", now=at(31))
    assert new.seq == 2
    late = await _complete(db, old, d=digest(1), now=at(32))
    assert isinstance(late.outcome, Committed) and late.outcome.late
    assert [x.seq for x in late.outcome.superseded] == [2]
    assert late.queue_done
    loser = await _complete(db, new, d=digest(2), now=at(33))
    assert loser.outcome == AlreadyDone(new.item_id, 2, 1, digest(1))


async def test_renew_extends_the_deadline_so_expiry_skips_it(db: Db) -> None:
    qid = await _queue(db, 2)
    a, b = await _lease(db, qid, "h1", k=2)
    async with db.sessions() as s, s.begin():
        out = await store.renew(
            s, holder="h1", leases=[(b.item_id, b.seq), (a.item_id, a.seq)], now=at(20)
        )
    assert out == [Renewed(b.item_id, 1, at(50)), Renewed(a.item_id, 1, at(50))]
    async with db.sessions() as s, s.begin():
        assert (await store.expire_due(s, now=at(40))).expired == ()
        foreign = await store.renew(s, holder="h2", leases=[(a.item_id, 1)], now=at(41))
    assert foreign == [Fenced(a.item_id, 1, FenceReason.WRONG_HOLDER)]


async def test_counted_expiry_can_fail_the_queue(db: Db) -> None:
    policy = QueuePolicy(visibility_timeout_s=5, max_attempts=1, count_expiry=True)
    qid = await _queue(db, 2, policy=policy)
    a, b = await _lease(db, qid, "h1", k=2)
    async with db.sessions() as s, s.begin():
        await store.renew(s, holder="h1", leases=[(b.item_id, b.seq)], now=at(4))
        result = await store.expire_due(s, now=at(5))
    assert [e.item_id for e in result.expired] == [a.item_id]
    assert result.expired[0].item_failed
    assert result.failed_queues == (qid,)
    assert [x.item_id for x in result.released] == [b.item_id]
    async with db.sessions() as s:
        info = await store.get_queue(s, qid)
    assert info is not None and info.state is QueueState.FAILED


# --- at most two live leases ------------------------------------------------------


async def test_speculation_rules_and_the_two_live_leases_index(db: Db) -> None:
    qid = await _queue(db, 10, policy=QueuePolicy(speculative_max_frac=0.1))
    grants = await _lease(db, qid, "h1", k=9, site="n1")
    async with db.sessions() as s, s.begin():
        refused = await store.speculate(
            s, item_id=grants[0].item_id, holder="h2", site="n2", now=at(1)
        )
    assert refused is SpeculationRefusal.PENDING_LEFT
    (last,) = await _lease(db, qid, "h2", site="n2")
    async with db.sessions() as s:
        cands = await store.speculation_candidates(s, qid, holder="h2", site="n2")
    assert [c.idx for c in cands] == list(range(9))  # not its own item 9
    async with db.sessions() as s, s.begin():
        same_site = await store.speculate(
            s, item_id=last.item_id, holder="h3", site="n2", now=at(1)
        )
        copy = await store.speculate(
            s, item_id=grants[0].item_id, holder="h2", site="n2", now=at(1)
        )
        spent = await store.speculate(
            s, item_id=grants[1].item_id, holder="h2", site="n2", now=at(1)
        )
    assert same_site is SpeculationRefusal.SAME_SITE
    assert isinstance(copy, store.Grant) and copy.speculative and copy.seq == 2
    assert spent is SpeculationRefusal.BUDGET_SPENT  # ceil(0.1 * 10) = 1
    async with db.sessions() as s:
        assert await store.speculation_candidates(s, qid, holder="h3", site="n3") == []
    c = await _counts(db, qid)
    assert (c.live_leases, c.speculative_live) == (11, 1)
    # The database itself refuses a third live lease (or a second live copy).
    async with db.sessions() as s:
        with pytest.raises(IntegrityError):
            async with s.begin():
                await s.execute(
                    insert(work_lease).values(
                        item_id=grants[0].item_id,
                        seq=3,
                        holder="h4",
                        site="n4",
                        state="live",
                        speculative=True,
                        leased_at=at(2),
                        deadline=at(3),
                    )
                )


# --- holder loss, failure, cancel -------------------------------------------------


async def test_release_holder_is_part_of_the_callers_transaction(db: Db) -> None:
    qid = await _queue(db, 3)
    mine = await _lease(db, qid, "lost", k=2)
    await _lease(db, qid, "other")
    async with db.sessions() as s:
        tx = await s.begin()
        released = await store.release_holder(s, "lost", now=at(5))
        assert {x.item_id for x in released} == {g.item_id for g in mine}
        await tx.rollback()  # the caller's transaction failed: nothing applies
    assert (await _counts(db, qid)).live_leases == 3
    async with db.sessions() as s, s.begin():
        released = await store.release_holder(s, "lost", now=at(5))
    assert len(released) == 2 and all(x.state is LeaseState.RELEASED for x in released)
    c = await _counts(db, qid)
    assert (c.pending, c.leased, c.live_leases) == (2, 1, 1)
    again = await _lease(db, qid, "new", k=3)
    assert [(g.idx, g.seq) for g in again] == [(0, 2), (1, 2)]
    # A late result from the lost holder for a still-open item commits (ADR-0015).
    late = await _complete(db, mine[0])
    assert isinstance(late.outcome, Committed) and late.outcome.late


async def test_failure_on_two_sites_fails_the_queue_and_releases_everyone(
    db: Db,
) -> None:
    qid = await _queue(db, 3)
    others = await _lease(db, qid, "h0", k=2, site="n0")  # items 0, 1
    (g,) = await _lease(db, qid, "h1", site="n1")  # item 2
    async with db.sessions() as s, s.begin():
        first = await store.fail(
            s, item_id=g.item_id, seq=g.seq, holder="h1", kind="user_error", now=at(1)
        )
    assert isinstance(first.outcome, Retried) and not first.queue_failed
    (g2,) = await _lease(db, qid, "h2", site="n2")
    assert g2.item_id == g.item_id and g2.seq == 2
    async with db.sessions() as s, s.begin():
        second = await store.fail(
            s, item_id=g2.item_id, seq=2, holder="h2", kind="user_error", now=at(2)
        )
    assert isinstance(second.outcome, ItemFailed) and second.queue_failed
    assert sorted(x.item_id for x in second.released) == sorted(
        o.item_id for o in others
    )
    assert await _lease(db, qid, "h3") == []
    late = await _complete(db, others[0])
    assert late.outcome == Fenced(others[0].item_id, 1, FenceReason.QUEUE_CLOSED)
    c = await _counts(db, qid)
    assert (c.failed, c.live_leases, c.done) == (1, 0, 0)


async def test_a_close_waits_for_an_open_pull_so_no_live_lease_survives_it(
    db: Db,
) -> None:
    """A puller's lease transaction is still open when another item fails for
    good: the close waits for it and then releases the new lease as well."""
    qid = await _queue(db, 3)
    (g,) = await _lease(db, qid, "h1")
    async with db.sessions() as a:
        tx = await a.begin()
        pulled = await store.lease_next(a, qid, holder="h2", now=at(1))
        assert [x.idx for x in pulled] == [1]

        async def close() -> store.FailResult:
            async with db.sessions() as b, b.begin():
                return await store.fail(
                    b,
                    item_id=g.item_id,
                    seq=g.seq,
                    holder="h1",
                    kind="user_error",
                    retryable=False,
                    now=at(2),
                )

        closing = asyncio.create_task(close())
        await asyncio.sleep(0.5)
        assert not closing.done()  # blocked by the open pull
        await tx.commit()
        result = await asyncio.wait_for(closing, timeout=10)
    assert result.queue_failed
    assert [x.item_id for x in result.released] == [pulled[0].item_id]
    c = await _counts(db, qid)
    assert (c.live_leases, c.failed) == (0, 1)


async def test_non_retryable_failure_fails_immediately(db: Db) -> None:
    qid = await _queue(db, 1)
    (g,) = await _lease(db, qid, "h1")
    async with db.sessions() as s, s.begin():
        out = await store.fail(
            s,
            item_id=g.item_id,
            seq=g.seq,
            holder="h1",
            kind="user_error",
            retryable=False,
            now=at(1),
        )
    assert isinstance(out.outcome, ItemFailed) and out.queue_failed


async def test_cancel_releases_and_closes(db: Db) -> None:
    qid = await _queue(db, 3)
    grants = await _lease(db, qid, "h1", k=2)
    async with db.sessions() as s, s.begin():
        released = await store.cancel(s, qid, now=at(3))
        again = await store.cancel(s, qid, now=at(4))
    assert len(released) == 2 and again == ()
    assert await _lease(db, qid, "h2") == []
    out = await _complete(db, grants[0])
    assert out.outcome == Fenced(grants[0].item_id, 1, FenceReason.QUEUE_CLOSED)
    async with db.sessions() as s, s.begin():
        renew = await store.renew(
            s, holder="h1", leases=[(grants[1].item_id, 1)], now=at(5)
        )
        with pytest.raises(store.WorkQueueError):
            await store.cancel(s, qid + 77, now=at(5))
    assert renew == [Fenced(grants[1].item_id, 1, FenceReason.QUEUE_CLOSED)]


async def test_release_one_lease(db: Db) -> None:
    qid = await _queue(db, 1)
    (g,) = await _lease(db, qid, "h1")
    async with db.sessions() as s, s.begin():
        out = await store.release(
            s, item_id=g.item_id, seq=g.seq, holder="h1", now=at(1)
        )
        none = await store.release(
            s, item_id=g.item_id, seq=g.seq, holder="h1", now=at(1)
        )
        missing = await store.release(
            s, item_id=g.item_id + 5, seq=1, holder="h1", now=at(1)
        )
    assert out is not None and none is None and missing is None
    assert (await _counts(db, qid)).pending == 1


async def test_a_second_copy_of_the_same_item_does_not_spend_more_budget(
    db: Db,
) -> None:
    qid = await _queue(db, 2, policy=QueuePolicy(speculative_max_frac=0.5))
    a, b = await _lease(db, qid, "h1", k=2, site="n1")
    async with db.sessions() as s, s.begin():
        copy = await store.speculate(
            s, item_id=a.item_id, holder="h2", site="n2", now=at(1)
        )
        assert isinstance(copy, store.Grant)
        released = await store.release(
            s, item_id=a.item_id, seq=copy.seq, holder="h2", now=at(2)
        )
        assert released is not None
        again = await store.speculate(
            s, item_id=a.item_id, holder="h3", site="n3", now=at(3)
        )
        other = await store.speculate(
            s, item_id=b.item_id, holder="h3", site="n3", now=at(3)
        )
    assert isinstance(again, store.Grant) and again.seq == 3
    assert other is SpeculationRefusal.BUDGET_SPENT  # ceil(0.5 * 2) = 1 item
    async with db.sessions() as s:
        info = await store.get_queue(s, qid)
    assert info is not None and info.speculated_count == 1


# --- restart safety ---------------------------------------------------------------


async def test_state_survives_a_new_engine(pg_settings: DatabaseSettings) -> None:
    """Everything is in PostgreSQL: a second engine (a restarted controller)
    continues exactly where the first stopped."""
    async with open_db(pg_settings) as first:
        qid = await _queue(first, 3)
        grants = await _lease(first, qid, "h1", k=2)
        await _complete(first, grants[0])
    async with open_db(pg_settings, migrate=False) as second:
        c = await _counts(second, qid)
        assert (c.done, c.leased, c.pending) == (1, 1, 1)
        replay = await _complete(second, grants[0])
        assert isinstance(replay.outcome, AlreadyDone) and replay.outcome.own
        late = await _complete(second, grants[1])
        assert isinstance(late.outcome, Committed)
        (g,) = await _lease(second, qid, "h2")
        assert g.idx == 2
        assert (await _complete(second, g)).queue_done
