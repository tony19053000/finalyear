"""V0.4 G1 (D43): overflow and rejection are visible.

* ``registry.write_queue_overflow`` - once per overflow episode per session; the
  episode ends when the queue drained below half its capacity, and a new episode
  is reported again. Per-drop ``registry.write_dropped`` is debug only.
* ``session.closed`` lists every per-session counter, including
  ``hardware_scores_rejected`` and the overflow-episode count.
* The shutdown deferred-end backstop, tested directly.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog
from test_session_linger import admit, make, settle

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.writer import WriteChain, WriteKind

pytestmark = pytest.mark.unit


async def _noop() -> None:
    return None


def overflow_events(logs: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in logs if e["event"] == "registry.write_queue_overflow"]


@pytest.mark.asyncio
async def test_one_overflow_warning_per_episode_and_again_after_a_drain() -> None:
    gate = asyncio.Event()

    async def held() -> None:
        await gate.wait()

    chain = WriteChain(node_id="node_x", session_id="sess_x", capacity=4)
    with structlog.testing.capture_logs() as logs:
        # First item blocks the consumer; four more fill the queue.
        chain.enqueue("set_node_state", WriteKind.STATE, held)
        chain.start()
        await settle()  # the consumer took the first write and is stuck on it
        for _ in range(4):
            assert chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        for _ in range(10):  # every one of these drops a touch
            chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        assert chain.dropped == 10
        (first,) = overflow_events(logs)
        assert first["log_level"] == "warning"
        assert (first["dropped"], first["capacity"], first["pending"]) == (1, 4, 3)
        assert first["session_id"] == "sess_x" and first["node_id"] == "node_x"
        drops = [e for e in logs if e["event"] == "registry.write_dropped"]
        assert len(drops) == 10 and all(e["log_level"] == "debug" for e in drops)

        gate.set()  # the database answers: the queue drains below half
        await settle()
        assert chain.pending == 0
        gate.clear()
        chain.enqueue("set_node_state", WriteKind.STATE, held)
        await settle()
        for _ in range(5):
            chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        episodes = overflow_events(logs)
        assert len(episodes) == 2 and episodes[1]["episode"] == 2
        assert chain.overflow_episodes == 2
        gate.set()
        chain.finish()
        await asyncio.wait_for(chain.task, 1.0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_draining_to_exactly_half_does_not_end_the_episode() -> None:
    gates = [asyncio.Event() for _ in range(3)]

    def blocker(gate: asyncio.Event) -> Any:
        async def write() -> None:
            await gate.wait()

        return write

    chain = WriteChain(node_id="node_x", session_id="sess_x", capacity=4)
    with structlog.testing.capture_logs() as logs:
        chain.enqueue("set_node_state", WriteKind.STATE, blocker(gates[0]))
        chain.start()
        await settle()  # consumer stuck on gates[0], queue empty
        chain.enqueue("set_node_state", WriteKind.STATE, blocker(gates[1]))
        chain.enqueue("set_node_state", WriteKind.STATE, blocker(gates[2]))
        chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        chain.enqueue("touch_node", WriteKind.TOUCH, _noop)  # drop: episode 1
        gates[0].set()
        await settle()  # popped one state write: 3 pending, stuck on gates[1]
        gates[1].set()
        await settle()  # popped the other: 2 pending = exactly half
        assert chain.pending == 2
        chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        chain.enqueue("touch_node", WriteKind.TOUCH, _noop)  # drop, same episode
        assert chain.dropped == 2
        assert len(overflow_events(logs)) == 1
        assert chain.overflow_episodes == 1
        gates[2].set()
        chain.finish()
        await asyncio.wait_for(chain.task, 1.0)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_session_closed_lists_every_per_session_counter() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    # One score report that fails validation (no run_id / fingerprint).
    await manager.handle_message(
        live,
        agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=live.session_id,
            hardware_scores=agent_pb2.HardwareScores(),
        ),
    )
    with structlog.testing.capture_logs() as logs:
        await manager.end_session(live, SessionEndReason.AGENT_CLOSED)
    (closed,) = [e for e in logs if e["event"] == "session.closed"]
    for key in (
        "heartbeats",
        "stale_messages",
        "duplicates",
        "resource_updates",
        "resource_updates_clamped",
        "hardware_scores_received",
        "hardware_scores_rejected",
        "pings_sent",
        "pongs_matched",
        "last_rtt_ms",
        "pending_writes",
        "writes_failed",
        "writes_dropped",
        "write_overflow_episodes",
    ):
        assert key in closed, key
    assert closed["hardware_scores_rejected"] == 1
    assert closed["write_overflow_episodes"] == 0


@pytest.mark.asyncio
async def test_shutdown_settles_a_deferred_end_with_its_event() -> None:
    """The backstop in ``SessionManager.shutdown``: a deferred end still parked
    when shutdown runs is finished with its event, so its chain gets its END
    and the row is not left ONLINE. Reached here by leaving a Hello's claim
    unreleased (what a crashed admission path would leave behind)."""
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    manager._claims.claim(node, 2)  # a pending Hello whose admission never returns
    await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
    assert manager._deferred_ends.get(node) is old
    assert old.state is NodeState.ONLINE  # deferred: no event yet
    await manager.shutdown(drain_s=1.0)
    assert manager._deferred_ends == {}
    assert old.state is NodeState.OFFLINE
    assert old.writer.done()
    assert reg.nodes[node].state is NodeState.OFFLINE
    assert ("close_session", old.session_id, SessionEndReason.AGENT_CLOSED, True) in (
        reg.writes
    )
