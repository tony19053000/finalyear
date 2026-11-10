"""A Hello's prior state is the in-memory one while an ended session's writes
are still queued (CI run 42181066876, cluster ``test_h ...[sigkill]``).

An admission cut short after its node-row upsert (D41) leaves the row ONLINE
and a lingering entry whose SUSPECT / OFFLINE writes wait for the abandoned
admission task. When that entry's lease expires its linger ends - but its
writes may still be queued, so the row keeps reading ONLINE. A Hello admitted
then took ``prior`` from that row: ONLINE -> a "superseding heartbeat"
self-loop, no ``node.state_changed`` at all, although the node had just gone
OFFLINE (its attempts lost). The same holds for any session ended just before
a Hello arrives (a Goodbye's OFFLINE still queued). The in-memory state of the
node's most recent ended session wins over the row until its chain is done -
the rule lingering and deferred entries already follow.
"""

from __future__ import annotations

import asyncio

import pytest
import structlog
from test_session_admission_deadline import (
    Hang,
    drain_chains,
    make_slow,
    state_changes,
)
from test_session_linger import CFG, NOW0, admit, hello

from meshforge.common.ids import new_node_id
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.manager import Accepted, Rejected

pytestmark = pytest.mark.unit


@pytest.mark.asyncio
async def test_a_hello_after_an_abandoned_entry_expired_is_a_reconnect() -> None:
    manager, reg, clock, timer = make_slow()
    node = new_node_id()
    first = await admit(manager, node, 1)
    # The agent is killed: stream lost, the linger expires, the row is OFFLINE.
    manager.end_session_nowait(first, SessionEndReason.STREAM_LOST)
    await drain_chains()
    clock.now = NOW0 + CFG.offline_after_s
    await timer.tick(clock.now)
    await drain_chains()
    assert reg.nodes[node].state is NodeState.OFFLINE

    # The restarted agent's Hello writes the row ONLINE, then misses its deadline.
    hang = Hang(reg, "open_session")
    reg.gate.clear()  # the abandoned entry's state writes stay queued
    with structlog.testing.capture_logs() as logs:
        result = await asyncio.wait_for(manager.admit(hello(node, 2), "ipv4:x:1"), 1.0)
        assert isinstance(result, Rejected)
        await drain_chains()
        assert reg.nodes[node].state is NodeState.ONLINE  # the upsert landed
        clock.now += CFG.offline_after_s
        await timer.tick(clock.now)  # the abandoned entry goes OFFLINE in memory
        await drain_chains()
        assert reg.nodes[node].state is NodeState.ONLINE  # ...not yet in the row

        reg.open_session = hang._inner  # type: ignore[method-assign]
        retry = await asyncio.wait_for(manager.admit(hello(node, 3), "ipv4:x:2"), 1.0)
        assert isinstance(retry, Accepted)
        reg.gate.set()
        await drain_chains()
    assert state_changes(logs, node) == [
        ("online", "suspect", "stream_lost"),  # the abandoned admission's entry
        ("suspect", "offline", "lease_expired"),
        ("offline", "online", "reconnected"),  # never a silent ONLINE self-loop
    ]
    rec = reg.nodes[node]
    assert rec.state is NodeState.ONLINE
    assert rec.last_session_id == retry.live.session_id  # stale writes fenced out


@pytest.mark.asyncio
async def test_a_hello_right_after_a_goodbye_whose_offline_is_queued() -> None:
    manager, reg, _clock, _timer = make_slow()
    node = new_node_id()
    first = await admit(manager, node, 1)
    await drain_chains()
    reg.gate.clear()  # the Goodbye's OFFLINE write is still queued
    with structlog.testing.capture_logs() as logs:
        manager.end_session_nowait(first, SessionEndReason.AGENT_CLOSED)
        await drain_chains()
        assert reg.nodes[node].state is NodeState.ONLINE
        second = await asyncio.wait_for(manager.admit(hello(node, 2), "ipv4:x:2"), 1.0)
        assert isinstance(second, Accepted)
        reg.gate.set()
        await drain_chains()
    assert state_changes(logs, node) == [
        ("online", "offline", "stream_closed"),
        ("offline", "online", "reconnected"),
    ]
    assert reg.nodes[node].state is NodeState.ONLINE


@pytest.mark.asyncio
async def test_a_settled_ended_session_leaves_the_row_in_charge() -> None:
    """Once the ended session's chain is done its entry is forgotten: the row
    (OFFLINE) is the prior, as before."""
    manager, _reg, _clock, _timer = make_slow()
    node = new_node_id()
    first = await admit(manager, node, 1)
    with structlog.testing.capture_logs() as logs:
        manager.end_session_nowait(first, SessionEndReason.AGENT_CLOSED)
        await drain_chains()
        assert first.writer.done()
        assert manager._ended.get(node) is None
        await admit(manager, node, 2)
        await drain_chains()
    assert state_changes(logs, node)[-1] == ("offline", "online", "reconnected")
