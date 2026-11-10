"""V1.0 G9-D (D437): a chunk that runs for hours keeps its lease while it is
renewed - on real PostgreSQL, with a fake wall clock and the controller's
default ``visibility_timeout_s`` (60 s): no expiry, no re-lease, lease_seq
stays 1. Once renewals stop, the lease expires one timeout after the last."""

from __future__ import annotations

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_workqueue import Db, at, digest

from meshforge.workqueue import store
from meshforge.workqueue.core import Committed, QueuePolicy, Renewed

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

POLICY = QueuePolicy(visibility_timeout_s=60, max_attempts=3, max_failed_sites=2)
EVERY_S = 20  # the lane renews every third of the lease timeout
HOURS = 2


async def test_a_renewed_chunk_is_never_expired_or_released(db: Db) -> None:
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s, kind="test", owner_id="job-1", payloads=[{"i": 0}], policy=POLICY
        )
    async with db.sessions() as s, s.begin():
        (grant,) = await store.lease_next(
            s, info.id, holder="lane-1", site="n2", k=1, now=at(0)
        )
    last = 0
    for t in range(EVERY_S, HOURS * 3600 + 1, EVERY_S):
        async with db.sessions() as s, s.begin():
            # the expiry sweep runs before the renewal lands: the worst order
            assert (await store.expire_due(s, now=at(t))).expired == ()
            out = await store.renew(
                s, holder="lane-1", leases=[(grant.item_id, grant.seq)], now=at(t)
            )
        assert out == [Renewed(grant.item_id, 1, at(t + 60))]
        last = t
        async with db.sessions() as s, s.begin():
            # nobody else can take the chunk while it is held
            assert (
                await store.lease_next(
                    s, info.id, holder="lane-2", site="n3", k=1, now=at(t)
                )
                == []
            )
    async with db.sessions() as s, s.begin():
        done = await store.complete(
            s,
            item_id=grant.item_id,
            seq=grant.seq,
            holder="lane-1",
            digest=digest(0),
            now=at(last + 1),
        )
    assert isinstance(done.outcome, Committed) and not done.outcome.late
    assert done.queue_done


async def test_without_renewals_the_lease_expires_one_timeout_after_the_last(
    db: Db,
) -> None:
    async with db.sessions() as s, s.begin():
        info = await store.create_queue(
            s, kind="test", owner_id="job-2", payloads=[{"i": 0}], policy=POLICY
        )
    async with db.sessions() as s, s.begin():
        (grant,) = await store.lease_next(
            s, info.id, holder="lane-1", site="n2", k=1, now=at(0)
        )
        await store.renew(s, holder="lane-1", leases=[(grant.item_id, 1)], now=at(900))
    async with db.sessions() as s, s.begin():
        assert (await store.expire_due(s, now=at(959))).expired == ()
        assert len((await store.expire_due(s, now=at(960))).expired) == 1
