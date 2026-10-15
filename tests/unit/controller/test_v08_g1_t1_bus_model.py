"""V0.8 G1 tester r1: the event bus checked against a simple model (D172).

Contract (brief D172 + bus docstring): a resumed subscriber gets exactly the
events after ``since`` - no gap, no duplicate - or an honest ``resync``.
"""

# ruff: noqa: S311

from __future__ import annotations

import asyncio
import random
import resource as _resource
import sys

import pytest

from meshforge.controller.events.bus import Audience, EventBus, EventType

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _drain(sub) -> list[int]:  # type: ignore[no-untyped-def]
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait().seq)
    return out


async def test_random_resume_is_exact_or_resync_never_a_gap_or_duplicate() -> None:
    rng = random.Random(20260915)
    for _ in range(300):
        ring = rng.randint(1, 40)
        queue = rng.randint(1, 60)
        bus = EventBus(ring_size=ring, queue_size=queue)
        total = rng.randint(0, 120)
        for _ in range(total):
            bus.publish(EventType.JOB_STATE, {"job_id": "j", "state": "x"})
        since = rng.randint(0, total + 3)
        sub, resync = bus.subscribe(since=since, epoch=bus.epoch)
        got = _drain(sub)
        if resync is None:
            assert got == list(range(since + 1, total + 1)), (ring, queue, total, since)
        else:
            assert resync.reason in {"ring", "future"}
            assert got == [], "a resync must not also replay a partial backlog"
            # And it was legitimately unavoidable:
            if resync.reason == "future":
                assert since > total
            else:
                held = min(ring, total)
                missed = total - since
                assert missed > held or missed > queue or since < total - held


async def test_resume_boundary_off_by_one_around_the_ring_edge() -> None:
    bus = EventBus(ring_size=5, queue_size=100)
    for _ in range(12):
        bus.publish(EventType.JOB_STATE, {})
    # ring holds seq 8..12. since=7 -> missed 8..12: exact.
    sub, resync = bus.subscribe(since=7, epoch=bus.epoch)
    assert resync is None and _drain(sub) == [8, 9, 10, 11, 12]
    # since=6 -> event 7 is gone: resync.
    _, resync = bus.subscribe(since=6, epoch=bus.epoch)
    assert resync is not None and resync.reason == "ring"
    # since == head: nothing missed, nothing replayed.
    sub, resync = bus.subscribe(since=12, epoch=bus.epoch)
    assert resync is None and _drain(sub) == []
    # since beyond head: future.
    _, resync = bus.subscribe(since=13, epoch=bus.epoch)
    assert resync is not None and resync.reason == "future"
    # a stranger's epoch: resync even though the numbers fit.
    _, resync = bus.subscribe(since=10, epoch="not-this-bus")
    assert resync is not None and resync.reason == "epoch"


async def test_since_zero_on_a_fresh_bus_and_on_a_busy_one() -> None:
    bus = EventBus(ring_size=3, queue_size=10)
    sub, resync = bus.subscribe(since=0, epoch=bus.epoch)
    assert resync is None and _drain(sub) == []
    for _ in range(10):
        bus.publish(EventType.ALERT, {}, audience=Audience.ADMIN)
    _, resync = bus.subscribe(since=0, epoch=bus.epoch)
    assert resync is not None  # events 1..7 are gone


async def test_live_events_after_resume_continue_without_a_seam() -> None:
    bus = EventBus(ring_size=50, queue_size=100)
    for _ in range(10):
        bus.publish(EventType.JOB_STATE, {})
    sub, resync = bus.subscribe(since=4, epoch=bus.epoch)
    assert resync is None
    for _ in range(5):
        bus.publish(EventType.JOB_STATE, {})
    assert _drain(sub) == list(range(5, 16))


async def test_seq_is_strictly_monotonic_and_unique_under_concurrent_publishers() -> (
    None
):
    bus = EventBus(ring_size=100_000, queue_size=100_000)
    sub, _ = bus.subscribe()

    async def burst(n: int) -> None:
        for _ in range(n):
            bus.publish(EventType.NODE_STATUS, {})
            await asyncio.sleep(0)

    await asyncio.gather(*(burst(500) for _ in range(8)))
    seqs = _drain(sub)
    assert seqs == list(range(1, 4001))


async def test_a_slow_subscriber_is_dropped_and_memory_stays_bounded() -> None:
    bus = EventBus(ring_size=200, queue_size=50)
    slow, _ = bus.subscribe()
    fast, _ = bus.subscribe()
    payload = {"blob": "x" * 2048}
    before = len(bus)
    for _ in range(5_000):
        bus.publish(EventType.NODE_METRICS, payload)
        _drain(fast)
    assert slow.overflowed and slow.wake.is_set()
    assert len(bus) == before - 1  # only the fast one is left
    assert slow.queue.qsize() <= 50
    assert bus.stats.dropped_subscribers == 1
    assert len(bus._ring) == 200
    # Further publishes do not grow the dropped queue.
    size = slow.queue.qsize()
    bus.publish(EventType.NODE_METRICS, payload)
    assert slow.queue.qsize() == size


async def test_visibility_matrix() -> None:
    bus = EventBus()
    bus.publish(EventType.NODE_STATUS, {}, audience=Audience.ALL)
    bus.publish(EventType.JOB_STATE, {}, audience=Audience.OWNER, owner_user_id="a")
    bus.publish(EventType.JOB_STATE, {}, audience=Audience.OWNER, owner_user_id=None)
    bus.publish(EventType.ALERT, {}, audience=Audience.ADMIN)
    ev = list(bus._ring)

    def seen(uid: str | None, admin: bool) -> list[int]:
        return [e.seq for e in ev if e.visible_to(uid, admin=admin)]

    assert seen("a", False) == [1, 2]
    assert seen("b", False) == [1]
    assert seen(None, False) == [1]  # no user id never matches an ownerless job
    assert seen("b", True) == [1, 2, 3, 4]


async def test_frames_never_leak_audience_or_owner() -> None:
    bus = EventBus()
    event = bus.publish(
        EventType.JOB_STATE, {"job_id": "j"}, audience=Audience.OWNER, owner_user_id="u"
    )
    assert set(event.frame()) == {"seq", "type", "at", "data"}
    assert "u" not in str(event.frame())


async def test_subscriber_cap_is_enforced_and_unsubscribe_frees_a_slot() -> None:
    bus = EventBus(max_subscribers=3)
    subs = [bus.subscribe()[0] for _ in range(3)]
    with pytest.raises(OverflowError):
        bus.subscribe()
    bus.unsubscribe(subs[0])
    bus.unsubscribe(subs[0])  # idempotent
    bus.subscribe()


@pytest.mark.skipif(sys.platform == "win32", reason="resource module")
async def test_ring_of_100k_big_publishes_does_not_grow_without_bound() -> None:
    bus = EventBus(ring_size=1000, queue_size=10)
    rss0 = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
    for i in range(100_000):
        bus.publish(EventType.NODE_METRICS, {"i": i, "pad": "y" * 1024})
    rss1 = _resource.getrusage(_resource.RUSAGE_SELF).ru_maxrss
    assert (rss1 - rss0) < 80_000  # KiB; a 100k-event leak would be >100 MB
