"""V0.10 G2 (D262, D276 fallback): seeded randomised model test.

``hypothesis`` is not available (§7 Q5 pending, pypi unreachable here), so this
is the in-house fallback: for each seed a random sequence of operations -
lease, renew, complete (also with a wrong holder or an unknown lease number),
fail, expire, release a holder, speculate, cancel - is applied both to the
PostgreSQL store and to an in-memory model built from the pure core. Every
outcome and, after every step, every item and lease row must be identical.
A failing seed is printed and reproducible.
"""

from __future__ import annotations

import datetime as dt
import random
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_workqueue import Db, at, digest
from sqlalchemy import select

from meshforge.workqueue import core, store
from meshforge.workqueue.core import (
    Committed,
    Fenced,
    FenceReason,
    Item,
    ItemFailed,
    ItemState,
    Lease,
    QueuePolicy,
    QueueState,
)
from meshforge.workqueue.tables import work_item, work_lease

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]

SEEDS = range(24)
STEPS = 150
HOLDERS = {"h0": "n0", "h1": "n1", "h2": "n2", "h3": "n0"}


@dataclass
class Model:
    """The queue as the pure core says it must be."""

    queue_id: int
    policy: QueuePolicy
    items: dict[int, Item]
    state: QueueState = QueueState.OPEN
    done_count: int = 0
    speculated_count: int = 0
    granted: list[tuple[int, int, str]] = field(default_factory=list)
    seen: Counter[str] = field(default_factory=Counter)

    def _saw(self, out: object) -> None:
        name = type(out).__name__
        if isinstance(out, Fenced):
            name = f"Fenced.{out.reason.value}"
        elif isinstance(out, core.AlreadyDone):
            name = "AlreadyDone.own" if out.own else "AlreadyDone.other"
        elif isinstance(out, Committed) and out.late:
            name = "Committed.late"
        self.seen[name] += 1

    def _put(self, item: Item) -> None:
        self.items[item.id] = item

    def _close(self, state: QueueState) -> tuple[Lease, ...]:
        self.state = state
        released: list[Lease] = []
        for item_id in sorted(self.items):
            new, out = core.release_all(self.items[item_id])
            if out is not None:
                self._put(new)
                released.extend(out.leases)
        return tuple(released)

    def lease_next(
        self, holder: str, site: str, k: int, now: dt.datetime
    ) -> list[tuple[Any, ...]]:
        if self.state is not QueueState.OPEN:
            return []
        out = []
        for item in core.next_pending(self.items.values(), site=site, k=k):
            new, lease = core.lease(
                item, holder=holder, site=site, now=now, policy=self.policy
            )
            self._put(new)
            out.append(_grant_key(item.id, item.idx, lease))
        return out

    def renew(self, holder: str, item_id: int, seq: int, now: dt.datetime) -> Any:
        if self.state is not QueueState.OPEN:
            return Fenced(item_id, seq, FenceReason.QUEUE_CLOSED)
        new, out = core.renew(
            self.items[item_id], seq=seq, holder=holder, now=now, policy=self.policy
        )
        self._put(new)
        self._saw(out)
        return out

    def complete(self, holder: str, item_id: int, seq: int, d: str) -> Any:
        new, out = core.complete(
            self.items[item_id],
            seq=seq,
            holder=holder,
            digest=d,
            queue_state=self.state,
        )
        self._put(new)
        self._saw(out)
        done = False
        if isinstance(out, Committed):
            self.done_count += 1
            if self.done_count == len(self.items):
                self.state = QueueState.DONE
                done = True
        return (out, done)

    def fail(
        self, holder: str, item_id: int, seq: int, kind: str, retryable: bool
    ) -> Any:
        new, out = core.fail(
            self.items[item_id],
            seq=seq,
            holder=holder,
            kind=kind,
            policy=self.policy,
            queue_state=self.state,
            retryable=retryable,
        )
        self._put(new)
        self._saw(out)
        if isinstance(out, ItemFailed):
            return (out, True, self._close(QueueState.FAILED))
        return (out, False, ())

    def expire(self, now: dt.datetime) -> Any:
        expired = []
        failed = False
        for item_id in sorted(self.items):
            new, out = core.expire(self.items[item_id], now=now, policy=self.policy)
            if out is not None:
                self._put(new)
                expired.append(out)
                self._saw(out)
                failed = failed or out.item_failed
        released: tuple[Lease, ...] = ()
        failed_queues: tuple[int, ...] = ()
        if failed and self.state is QueueState.OPEN:
            failed_queues = (self.queue_id,)
            released = self._close(QueueState.FAILED)
        return (tuple(expired), failed_queues, released)

    def release_holder(self, holder: str) -> tuple[Lease, ...]:
        released: list[Lease] = []
        for item_id in sorted(self.items):
            new, out = core.release(self.items[item_id], holder=holder)
            if out is not None:
                self._put(new)
                released.extend(out.leases)
        return tuple(released)

    def speculate(self, holder: str, site: str, item_id: int, now: dt.datetime) -> Any:
        if self.state is not QueueState.OPEN:
            return core.SpeculationRefusal.QUEUE_CLOSED
        if any(x.state is ItemState.PENDING for x in self.items.values()):
            return core.SpeculationRefusal.PENDING_LEFT
        budget = core.speculation_budget(
            total_items=len(self.items),
            speculated_items=self.speculated_count,
            max_frac=self.policy.speculative_max_frac,
        )
        if budget <= 0:
            return core.SpeculationRefusal.BUDGET_SPENT
        item = self.items[item_id]
        new, out = core.speculate(
            item, holder=holder, site=site, now=now, policy=self.policy
        )
        if isinstance(out, core.SpeculationRefusal):
            return out
        self.seen["speculative_grant"] += 1
        self._put(new)
        if item.speculations == 0:
            self.speculated_count += 1
        return _grant_key(item.id, item.idx, out)

    def cancel(self) -> tuple[Lease, ...]:
        if self.state is not QueueState.OPEN:
            return ()
        return self._close(QueueState.CANCELLED)


def _grant_key(item_id: int, idx: int, lease: Lease) -> tuple[Any, ...]:
    return (
        item_id,
        idx,
        lease.seq,
        lease.holder,
        lease.site,
        lease.leased_at,
        lease.deadline,
        lease.speculative,
    )


def _store_grant_key(g: store.Grant) -> tuple[Any, ...]:
    return (
        g.item_id,
        g.idx,
        g.seq,
        g.holder,
        g.site,
        g.leased_at,
        g.deadline,
        g.speculative,
    )


async def _db_items(db: Db, queue_id: int) -> dict[int, tuple[Any, ...]]:
    async with db.sessions() as s:
        items = (
            await s.execute(select(work_item).where(work_item.c.queue_id == queue_id))
        ).all()
        leases = (
            await s.execute(
                select(work_lease)
                .join(work_item)
                .where(work_item.c.queue_id == queue_id)
                .order_by(work_lease.c.item_id, work_lease.c.seq)
            )
        ).all()
    by_item: dict[int, list[tuple[Any, ...]]] = {}
    for x in leases:
        by_item.setdefault(x.item_id, []).append(
            (x.seq, x.holder, x.site, x.state, x.speculative, x.leased_at, x.deadline)
        )
    return {
        r.id: (
            r.idx,
            r.state,
            r.attempts,
            r.lease_seq,
            r.speculations,
            r.result_digest,
            r.done_lease_seq,
            r.failed_kind,
            tuple(r.failed_sites),
            tuple(by_item.get(r.id, ())),
        )
        for r in items
    }


def _model_items(model: Model) -> dict[int, tuple[Any, ...]]:
    return {
        x.id: (
            x.idx,
            x.state.value,
            x.attempts,
            x.lease_seq,
            x.speculations,
            x.result_digest,
            x.done_lease_seq,
            x.failed_kind,
            x.failed_sites,
            tuple(
                (
                    lease.seq,
                    lease.holder,
                    lease.site,
                    lease.state.value,
                    lease.speculative,
                    lease.leased_at,
                    lease.deadline,
                )
                for lease in x.leases
            ),
        )
        for x in model.items.values()
    }


async def _run_seed(db: Db, seed: int) -> tuple[Counter[str], Counter[str]]:
    rng = random.Random(seed)  # noqa: S311 - reproducible test sequence
    policy = QueuePolicy(
        visibility_timeout_s=rng.choice((15.0, 30.0)),
        max_attempts=rng.choice((2, 3, 5)),
        max_failed_sites=rng.choice((None, 2)),
        count_expiry=rng.random() < 0.5,
        speculative_max_frac=rng.choice((0.0, 0.3, 1.0)),
    )
    n = rng.randint(1, 12)
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s,
            kind="model",
            owner_id=f"seed-{seed}",
            payloads=list(range(n)),
            policy=policy,
        )
    async with db.sessions() as s:
        ids = (
            await s.execute(
                select(work_item.c.id, work_item.c.idx)
                .where(work_item.c.queue_id == info.id)
                .order_by(work_item.c.idx)
            )
        ).all()
    model = Model(info.id, policy, {r.id: Item(id=r.id, idx=r.idx) for r in ids})
    ops: Counter[str] = Counter()
    t = 0.0
    for step in range(STEPS):
        t += rng.choice((0.0, 0.5, 3.0, 9.0))
        now = at(t + seed * 10_000)
        holder = rng.choice(sorted(HOLDERS))
        site = HOLDERS[holder]
        op = rng.choices(
            (
                "lease",
                "renew",
                "complete",
                "fail",
                "expire",
                "release",
                "speculate",
                "cancel",
            ),
            weights=(30, 8, 30, 10, 10, 4, 7, 1),
        )[0]
        ops[op] += 1
        where = f"seed={seed} step={step} op={op}"
        async with db.sessions() as s, s.begin():
            if op == "lease":
                k = rng.randint(1, 3)
                got = await store.lease_next(
                    s, info.id, holder=holder, site=site, k=k, now=now
                )
                want = model.lease_next(holder, site, k, now)
                assert [_store_grant_key(g) for g in got] == want, where
                model.granted.extend((g.item_id, g.seq, g.holder) for g in got)
            elif op == "speculate":
                item_id = rng.choice(sorted(model.items))
                got_s = await store.speculate(
                    s, item_id=item_id, holder=holder, site=site, now=now
                )
                want_s = model.speculate(holder, site, item_id, now)
                if isinstance(got_s, store.Grant):
                    assert _store_grant_key(got_s) == want_s, where
                    model.granted.append((got_s.item_id, got_s.seq, got_s.holder))
                else:
                    assert got_s == want_s, where
            elif op in ("renew", "complete", "fail") and model.granted:
                item_id, seq, owner = rng.choice(model.granted)
                roll = rng.random()
                if roll < 0.1:
                    owner = "intruder"
                elif roll < 0.15:
                    seq += 7
                if op == "renew":
                    got_r = await store.renew(
                        s, holder=owner, leases=[(item_id, seq)], now=now
                    )
                    assert got_r == [model.renew(owner, item_id, seq, now)], where
                elif op == "complete":
                    d = digest(rng.randint(0, 3))
                    res = await store.complete(
                        s, item_id=item_id, seq=seq, holder=owner, digest=d, now=now
                    )
                    assert (res.outcome, res.queue_done) == model.complete(
                        owner, item_id, seq, d
                    ), where
                else:
                    retryable = rng.random() < 0.8
                    fres = await store.fail(
                        s,
                        item_id=item_id,
                        seq=seq,
                        holder=owner,
                        kind="oom",
                        retryable=retryable,
                        now=now,
                    )
                    assert (
                        fres.outcome,
                        fres.queue_failed,
                        fres.released,
                    ) == model.fail(owner, item_id, seq, "oom", retryable), where
            elif op == "expire":
                eres = await store.expire_due(s, now=now, limit=1000)
                assert (
                    eres.expired,
                    eres.failed_queues,
                    eres.released,
                ) == model.expire(now), where
            elif op == "release":
                got_rel = await store.release_holder(s, holder, now=now)
                assert got_rel == model.release_holder(holder), where
            elif op == "cancel":
                assert await store.cancel(s, info.id, now=now) == model.cancel(), where
        assert await _db_items(db, info.id) == _model_items(model), where
        async with db.sessions() as s:
            counts = await store.counts(s, info.id)
            queue = await store.get_queue(s, info.id)
        assert queue is not None and queue.state is model.state, where
        assert queue.done_count == model.done_count, where
        assert queue.speculated_count == model.speculated_count, where
        live = [x for i in model.items.values() for x in i.live_leases]
        assert counts.live_leases == len(live), where
        assert all(
            len(i.live_leases) <= core.MAX_LIVE_LEASES for i in model.items.values()
        )
    # Close the queue so the global expiry of the next seed does not see it.
    async with db.sessions() as s, s.begin():
        await store.cancel(s, info.id, now=at(seed * 10_000 + 9_999))
    return ops, model.seen


async def test_store_matches_the_pure_core_on_random_sequences(db: Db) -> None:
    total: Counter[str] = Counter()
    seen: Counter[str] = Counter()
    for seed in SEEDS:
        ops, outcomes = await _run_seed(db, seed)
        total.update(ops)
        seen.update(outcomes)
    print(f"model test: ops={dict(total)} outcomes={dict(seen)}")  # noqa: T201
    # Every interesting outcome was reached by some seed.
    assert set(seen) >= {
        "Committed",
        "Committed.late",
        "AlreadyDone.own",
        "AlreadyDone.other",
        "Fenced.unknown_lease",
        "Fenced.wrong_holder",
        "Fenced.lease_not_live",
        "Fenced.lease_failed",
        "Fenced.queue_closed",
        "Retried",
        "ItemFailed",
        "Expired",
        "Renewed",
        "speculative_grant",
    }, seen
    # Every operation was exercised.
    assert set(total) == {
        "lease",
        "renew",
        "complete",
        "fail",
        "expire",
        "release",
        "speculate",
        "cancel",
    }
