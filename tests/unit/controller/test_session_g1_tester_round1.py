"""V0.4 G1 tester round 1 — races the implementer's own suite did not cover.

Uses the same fixtures as ``test_session_admission_deadline.py`` /
``test_session_goodbye.py`` (in-memory ``FencedRegistry``, real ``asyncio.wait``
timeouts for admission, a ``FedStream`` for reader-driven Goodbye scenarios).

1. Controller ``shutdown()`` racing an admission that is about to be abandoned
   (D41 x shutdown): the node row must never be left ONLINE, ``shutdown()``
   must not hang, and nothing leaks (claims, lingering entries, tasks).
2. Repeated admission timeouts on distinct nodes must not leak tasks/claims/
   lingering entries — the manager's live bookkeeping must return to empty
   after every one.
3. A Goodbye arriving while the *live* (still-connected, not lingering)
   session is already SUSPECT (a missed heartbeat, stream still open) must go
   straight to OFFLINE, not into a linger.
4. Two distinct Goodbye messages (different ``message_id``) delivered to the
   handler directly: idempotent, first reason wins, only one ``close_session``
   write, no exception.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from test_session_admission_deadline import Hang, drain_chains
from test_session_goodbye import FedStream, goodbye
from test_session_linger import CFG, FakeClock, admit, hello, make, settle

from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.manager import Rejected

pytestmark = pytest.mark.unit

DEADLINE_S = 0.02


def make_slow(clock: Any = None) -> Any:
    return make(clock, admission_timeout_s=DEADLINE_S)


def live_task_names() -> list[str]:
    return [
        t.get_name()
        for t in asyncio.all_tasks()
        if not t.done()
        and (
            t.get_name().startswith("session-admit")
            or t.get_name().startswith("registry-chain")
        )
    ]


# ------------------------------------------------ 1. shutdown races the timeout


@pytest.mark.asyncio
async def test_shutdown_racing_an_admission_abandoned_after_the_row_write() -> None:
    """The timeout fires with ``row_written`` True (the upsert already committed,
    ``open_session`` hangs) while ``shutdown()`` is concurrently called. Both
    want the table lock; ``shutdown()`` must still complete, and the node must
    never be left reading ONLINE for a session nothing will ever finish."""
    manager, reg, _clock, _timer = make_slow()
    node = new_node_id()
    old = await admit(manager, node, 1)
    assert old.state is NodeState.ONLINE
    hang = Hang(reg, "open_session")

    admit_task = asyncio.create_task(manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"))
    await asyncio.wait_for(hang.held.wait(), 1.0)
    shutdown_task = asyncio.create_task(manager.shutdown(drain_s=1.0))

    result = await asyncio.wait_for(admit_task, 2.0)
    assert isinstance(result, Rejected)
    await asyncio.wait_for(shutdown_task, 3.0)  # must not hang

    # Nothing left claimed, deferred, or lingering.
    assert manager._pending_hellos == {}
    assert manager._deferred_ends == {}
    assert manager.lingering() == []
    assert manager.snapshot() == []

    # The safety invariant: the row is never left ONLINE once shutdown returns
    # (there is no controller left to ever move it off ONLINE again).
    row = reg.nodes[node]
    assert row.state is not NodeState.ONLINE, (
        f"node row read {row.state} after a shutdown that raced an abandoned "
        "admission — a controller restart would find a node ONLINE with no "
        "session behind it"
    )

    leaked = live_task_names()
    assert leaked == [], f"tasks leaked across the race: {leaked}"


@pytest.mark.asyncio
async def test_shutdown_racing_an_admission_abandoned_before_any_write() -> None:
    """The timeout fires before ``upsert_node_on_hello`` (``get_node`` hangs):
    nothing was ever written for the new attempt. Concurrent shutdown must
    still close the *old*, still-live session cleanly."""
    manager, reg, _clock, _timer = make_slow()
    node = new_node_id()
    old = await admit(manager, node, 1)
    hang = Hang(reg, "get_node")

    admit_task = asyncio.create_task(manager.admit(hello(node, 2), "ipv4:1.2.3.4:5"))
    await asyncio.wait_for(hang.held.wait(), 1.0)
    shutdown_task = asyncio.create_task(manager.shutdown(drain_s=1.0))

    result = await asyncio.wait_for(admit_task, 2.0)
    assert isinstance(result, Rejected)
    await asyncio.wait_for(shutdown_task, 3.0)

    assert manager._pending_hellos == {}
    assert manager._deferred_ends == {}
    assert manager.lingering() == []
    assert manager.snapshot() == []
    assert old.state is NodeState.OFFLINE  # closed by shutdown's own loop
    assert old.end_reason is SessionEndReason.CONTROLLER_SHUTDOWN
    row = reg.nodes[node]
    assert row.state is NodeState.OFFLINE
    assert live_task_names() == []


# --------------------------------------------------- 2. repeated timeouts leak


@pytest.mark.asyncio
async def test_repeated_admission_timeouts_leave_no_residue() -> None:
    """Five Hellos, five nodes, every one abandoned after the row write (the
    worst case: each creates a synthetic lingering entry). The claim/deferred
    bookkeeping returns to empty at once every time; the synthetic linger is
    resolved (by design) only at its lease edge — advance past it and confirm
    that too leaves no task, chain, or lingering-table residue behind."""
    clock = FakeClock()
    manager, reg, _clock, timer = make_slow(clock=clock)
    baseline = live_task_names()
    for _ in range(5):
        node = new_node_id()
        Hang(reg, "open_session")
        result = await asyncio.wait_for(
            manager.admit(hello(node, 1), "ipv4:1.2.3.4:5"), 2.0
        )
        assert isinstance(result, Rejected)
        await drain_chains()
        assert manager._pending_hellos == {}
        assert manager._deferred_ends == {}
        entry = manager.get_lingering(node)
        assert entry is not None  # the synthetic abandoned-admission linger
        clock.now += CFG.offline_after_s + 1e-3
        await timer.tick(clock.now)
        await drain_chains()
        assert manager.get_lingering(node) is None
        assert reg.nodes[node].state is NodeState.OFFLINE
        assert live_task_names() == baseline, live_task_names()
    assert manager._pending_hellos == {}
    assert manager._deferred_ends == {}
    assert manager.lingering() == []


# ------------------------------------------ 3. Goodbye from a live SUSPECT node


@pytest.mark.asyncio
async def test_goodbye_from_a_live_suspect_session_is_offline_not_lingering() -> None:
    """A missed heartbeat moves a still-connected session to SUSPECT (the lease
    timer's LEASE_MISSED, module docstring in leases.py) without ending it. If
    the agent then says Goodbye, the session must end ``agent_closed`` ->
    OFFLINE at once, exactly as from ONLINE — never treated as a stream loss
    and never left lingering."""
    manager, reg, clock, timer = make()
    live = await admit(manager, new_node_id(), 1)
    # Advance past suspect_after_s with no heartbeat: LEASE_MISSED while live.
    clock.now += CFG.suspect_after_s + 1e-3
    await timer.tick(clock.now)
    assert live.state is NodeState.SUSPECT
    assert not live.ended and not live.lingering
    assert manager.get(live.node_id) is live  # still the live session

    stream = FedStream()
    task = manager.start_reader(live, stream)
    with structlog.testing.capture_logs() as logs:
        stream.feed(goodbye(live))
        await asyncio.wait_for(task, 0.5)
    assert live.end_reason is SessionEndReason.AGENT_CLOSED
    assert live.state == NodeState.OFFLINE
    assert not live.lingering
    assert manager.get_lingering(live.node_id) is None
    changes = [
        (e["from"], e["to"], e["liveness_event"])
        for e in logs
        if e["event"] == "node.state_changed"
    ]
    assert ("suspect", "offline", "stream_closed") in changes
    await settle()
    assert reg.nodes[live.node_id].state is NodeState.OFFLINE


# ------------------------------------------------------- 4. duplicate Goodbye


@pytest.mark.asyncio
async def test_two_distinct_goodbyes_delivered_directly_is_idempotent() -> None:
    """Two different Goodbye messages (distinct message_id, e.g. a retransmit
    the agent should never send but a hostile/buggy peer might) reaching the
    handler: the first ends the session; the second must not raise, must not
    re-close, and must not queue a second close_session."""
    manager, reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    await manager.handle_message(live, goodbye(live))
    assert live.end_reason is SessionEndReason.AGENT_CLOSED
    await settle()
    closes_before = [w for w in reg.writes if w[0] == "close_session"]

    # A second, distinct Goodbye (handle_message is also called directly by
    # tests, bypassing the reader's "stop reading after Goodbye" guard, to
    # pin the handler's own idempotency rather than the reader's).
    await manager.handle_message(live, goodbye(live))
    await settle()
    closes_after = [w for w in reg.writes if w[0] == "close_session"]

    assert live.end_reason is SessionEndReason.AGENT_CLOSED  # first reason wins
    assert closes_after == closes_before, "a second Goodbye queued a second close"
