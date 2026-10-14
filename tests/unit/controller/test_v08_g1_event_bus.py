"""V0.8 G1 deliverable 3 (D172): the event bus - order, resume or resync,
bounded queues and slow-subscriber drop, per-user visibility."""

from __future__ import annotations

import asyncio
import sys

import pytest

from meshforge.controller.events.bus import (
    Audience,
    BusEvent,
    EventBus,
    EventType,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _publish(bus: EventBus, n: int) -> None:
    for i in range(n):
        bus.publish(EventType.JOB_STATE, {"job_id": f"j{i}", "state": "queued"})


async def test_seq_is_monotonic_from_one_and_frames_hide_the_audience() -> None:
    bus = EventBus(wall_clock=lambda: 12.5)
    first = bus.publish(EventType.NODE_STATUS, {"node_id": "n"})
    second = bus.publish(
        EventType.JOB_STATE, {"job_id": "j"}, audience=Audience.OWNER, owner_user_id="u"
    )
    assert (first.seq, second.seq, bus.seq) == (1, 2, 2)
    assert second.frame() == {
        "seq": 2,
        "type": "job.state",
        "at": 12.5,
        "data": {"job_id": "j"},
    }


async def test_resume_gets_exactly_the_missed_events_in_order() -> None:
    bus = EventBus()
    _publish(bus, 5)
    sub, resync = bus.subscribe(since=2, epoch=bus.epoch)
    assert resync is None
    got = [sub.queue.get_nowait().seq for _ in range(sub.queue.qsize())]
    assert got == [3, 4, 5]
    bus.publish(EventType.ALERT, {"kind": "x"}, audience=Audience.ADMIN)
    assert sub.queue.get_nowait().seq == 6  # live events follow, no gap
    caught_up, resync = bus.subscribe(since=6, epoch=bus.epoch)
    assert resync is None and caught_up.queue.empty()


@pytest.mark.parametrize(
    ("since", "epoch", "reason"),
    [
        (1, "other-run", "epoch"),
        (99, None, "future"),
        (2, None, "ring"),  # the ring (size 5) no longer holds seq 3
    ],
)
async def test_resync_when_the_resume_point_is_gone(
    since: int, epoch: str | None, reason: str
) -> None:
    bus = EventBus(ring_size=5)
    _publish(bus, 10)  # ring holds 6..10
    sub, resync = bus.subscribe(since=since, epoch=epoch)
    assert resync is not None and resync.reason == reason
    assert sub.queue.empty()
    assert bus.stats.resyncs == 1


async def test_the_oldest_event_still_in_the_ring_resumes() -> None:
    bus = EventBus(ring_size=5)
    _publish(bus, 10)
    sub, resync = bus.subscribe(since=5, epoch=bus.epoch)  # wants 6..10
    assert resync is None and sub.queue.qsize() == 5


async def test_a_backlog_bigger_than_the_queue_is_a_resync() -> None:
    bus = EventBus(ring_size=100, queue_size=3)
    _publish(bus, 10)
    _, resync = bus.subscribe(since=0, epoch=bus.epoch)
    assert resync is not None and resync.reason == "ring"


@pytest.mark.security
async def test_a_slow_subscriber_is_dropped_and_memory_stays_bounded() -> None:
    bus = EventBus(ring_size=50, queue_size=4)
    slow, _ = bus.subscribe()
    fast, _ = bus.subscribe()
    for i in range(1000):
        bus.publish(EventType.NODE_METRICS, {"i": i, "pad": "x" * 100})
        while not fast.queue.empty():
            fast.queue.get_nowait()
    assert slow.overflowed and slow.wake.is_set()
    assert slow.queue.qsize() == 4  # never more than the queue size
    assert len(bus) == 1  # the slow one is gone, the fast one stays
    assert bus.stats.dropped_subscribers == 1
    assert len(bus._ring) == 50
    assert sys.getsizeof(bus._ring) < 10_000


@pytest.mark.security
async def test_too_many_subscribers_is_refused() -> None:
    bus = EventBus(max_subscribers=2)
    bus.subscribe()
    bus.subscribe()
    with pytest.raises(OverflowError):
        bus.subscribe()
    assert bus.stats.refused_subscribers == 1


@pytest.mark.security
@pytest.mark.parametrize(
    ("audience", "owner", "user", "admin", "visible"),
    [
        (Audience.ALL, None, "u1", False, True),
        (Audience.OWNER, "u1", "u1", False, True),
        (Audience.OWNER, "u1", "u2", False, False),
        (Audience.OWNER, None, "u1", False, False),  # legacy job: admin only
        (Audience.OWNER, "u1", None, False, False),
        (Audience.OWNER, "u1", "u2", True, True),
        (Audience.ADMIN, None, "u1", False, False),
        (Audience.ADMIN, None, "u1", True, True),
    ],
)
async def test_visibility(
    audience: Audience, owner: str | None, user: str | None, admin: bool, visible: bool
) -> None:
    event = BusEvent(
        seq=1,
        type=EventType.JOB_STATE,
        at=0.0,
        data={},
        audience=audience,
        owner_user_id=owner,
    )
    assert event.visible_to(user, admin=admin) is visible


async def test_unsubscribe_is_idempotent_and_epochs_differ_per_bus() -> None:
    bus = EventBus()
    sub, _ = bus.subscribe()
    bus.unsubscribe(sub)
    bus.unsubscribe(sub)
    assert len(bus) == 0
    assert EventBus().epoch != bus.epoch
    await asyncio.sleep(0)
