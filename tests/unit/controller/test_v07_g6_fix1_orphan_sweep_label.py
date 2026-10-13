"""V0.7 G6 fix round 1 (gate, T-11): the orphan sweep never steals the label of
a node that just went OFFLINE.

When a lease expires, the session leaves the manager and an ``_Offline`` item
is queued in the same synchronous step. A dispatcher tick that was already
running then saw "no session" and lost the attempts as ``node_not_connected``
before the queued item could lose them as ``node_offline`` - which one won
depended on load (``test_offline_by_lease_expiry_retries_on_the_other_node``
failed 2/5 under a CPU burn). The sweep now skips a node with a queued
``_Offline`` item:

* pending -> skipped; other unconnected nodes are still swept;
* the item taken from the queue -> no longer pending (the sweep is the
  backstop again if handling it fails);
* an item that could not be queued (queue full) is never pending;
* two queued items for one node are counted, not a flag.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.jobs.dispatcher import Dispatcher
from meshforge.controller.jobs.recovery import _Offline
from meshforge.controller.liveness import LivenessEvent

pytestmark = pytest.mark.unit


def _entry(node_id: str) -> Any:
    return SimpleNamespace(
        attempt=SimpleNamespace(node_id=node_id, id=f"att_{node_id}")
    )


def _dispatcher(queue_size: int = 8) -> Any:
    d: Any = Dispatcher.__new__(Dispatcher)
    d._queue = asyncio.Queue(queue_size)
    d._offline_pending = {}
    d._clock = lambda: 100.0
    d._started_at = 0.0
    d._settings = SimpleNamespace(reconnect_grace_s=1.0)
    d._manager = SimpleNamespace(get=lambda _n: None, get_lingering=lambda _n: None)
    d.lost = []

    async def lose(entry: Any, detail: str) -> None:
        d.lost.append((entry.attempt.node_id, detail))

    d._lose = lose
    return d


def _live(node_id: str) -> Any:
    return SimpleNamespace(node_id=node_id, session_id=f"ses_{node_id}")


def test_a_node_with_a_queued_offline_item_is_left_to_that_item() -> None:
    async def main() -> None:
        d = _dispatcher()
        d._on_node_offline(_live("gone"), LivenessEvent.LEASE_EXPIRED)
        assert d._offline_pending == {"gone": 1}
        await d._sweep_orphans([_entry("gone"), _entry("never_connected")])
        assert d.lost == [("never_connected", "node_not_connected")]
        # The run loop takes the item: handled next, no longer pending.
        item = d._take(d._queue.get_nowait())
        assert isinstance(item, _Offline) and item.node_id == "gone"
        assert d._offline_pending == {}
        # Had handling it failed for good, the sweep is the backstop again.
        await d._sweep_orphans([_entry("gone")])
        assert d.lost[-1] == ("gone", "node_not_connected")

    asyncio.run(main())


def test_an_item_that_could_not_be_queued_is_not_pending() -> None:
    async def main() -> None:
        d = _dispatcher(queue_size=1)
        d._on_node_offline(_live("a"), LivenessEvent.LEASE_EXPIRED)
        d._on_node_offline(_live("b"), LivenessEvent.LEASE_EXPIRED)  # queue full
        assert d._offline_pending == {"a": 1}
        await d._sweep_orphans([_entry("a"), _entry("b")])
        assert d.lost == [("b", "node_not_connected")]

    asyncio.run(main())


def test_two_queued_items_for_one_node_are_counted() -> None:
    async def main() -> None:
        d = _dispatcher()
        d._on_node_offline(_live("n"), LivenessEvent.LEASE_EXPIRED)
        d._on_node_offline(_live("n"), LivenessEvent.REVOKE)
        assert d._offline_pending == {"n": 2}
        d._take(d._queue.get_nowait())
        assert d._offline_pending == {"n": 1}
        await d._sweep_orphans([_entry("n")])
        assert d.lost == []
        d._take(d._queue.get_nowait())
        assert d._offline_pending == {}
        # Other queue items do not touch the count.
        assert d._take("status-item") == "status-item"

    asyncio.run(main())
