"""V0.3 G2 review round 2 - B1: the reader and the lease timer never await the DB.

The registry here is a fake whose writes can be *stalled* (they await an Event
the test controls) - the in-process equivalent of PostgreSQL hanging on connect
or on a statement. Everything runs on a fake clock; no ``sleep`` above 50 ms.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Mapping
from typing import Any, cast

import pytest
import structlog

from meshforge import __version__
from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, LivenessEvent, NodeState
from meshforge.controller.registry import (
    NodeRecord,
    Registry,
    RegistryError,
    SessionEndReason,
)
from meshforge.controller.session.leases import LeaseTimer
from meshforge.controller.session.manager import (
    Accepted,
    LiveSession,
    SessionManager,
)
from meshforge.controller.session.writer import WriteChain, WriteKind

pytestmark = pytest.mark.unit

CFG = LivenessConfig()  # 2 s / +0.5 s / x3 -> SUSPECT 2.5 s, OFFLINE 6 s
NOW0 = 1000.0


class FakeClock:
    def __init__(self, start: float = NOW0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class StallingRegistry:
    """Every write after admission awaits ``gate`` (never set = database hung).

    Admission (``get_node``/``upsert``/``open_session``) answers at once so the
    test can open sessions; ``calls`` records writes in the order they *executed*.
    """

    def __init__(self) -> None:
        self.gate = asyncio.Event()
        self.calls: list[tuple[str, Any]] = []
        self.started: list[str] = []
        self.nodes: dict[str, NodeRecord] = {}
        self.fail_next: RegistryError | None = None

    async def _stall(self, name: str) -> None:
        self.started.append(name)
        await self.gate.wait()
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    async def get_node(self, node_id: str) -> NodeRecord | None:
        return self.nodes.get(node_id)

    async def upsert_node_on_hello(self, **kwargs: Any) -> NodeRecord:
        now = dt.datetime.now(dt.UTC)
        rec = NodeRecord(
            node_id=kwargs["node_id"],
            hostname=kwargs["hostname"],
            os=kwargs["os"],
            platform=kwargs["platform"],
            hardware=dict(cast(Mapping[str, Any], kwargs["hardware"])),
            state=kwargs["state"],
            first_seen_at=now,
            last_seen_at=now,
            last_session_id=kwargs["session_id"],
            session_epoch=kwargs["session_epoch"],
        )
        self.nodes[rec.node_id] = rec
        return rec

    async def open_session(self, **kwargs: Any) -> None:
        return None

    async def touch_node(self, node_id: str) -> None:
        await self._stall("touch_node")
        self.calls.append(("touch_node", node_id))

    async def set_node_state(
        self, node_id: str, state: NodeState, *, session_id: str
    ) -> bool:
        await self._stall("set_node_state")
        self.calls.append(("set_node_state", state))
        return True

    async def record_rtt(self, session_id: str, **kwargs: Any) -> None:
        await self._stall("record_rtt")
        self.calls.append(("record_rtt", session_id))

    async def close_session(self, session_id: str, reason: SessionEndReason) -> bool:
        await self._stall("close_session")
        self.calls.append(("close_session", reason))
        return True


def make_manager(reg: StallingRegistry, clock: FakeClock, **kw: Any) -> SessionManager:
    return SessionManager(
        cast(Registry, reg),
        CFG,
        clock=clock,
        wall_clock=lambda: dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
        **kw,
    )


def hello(node_id: str, epoch: int = 1) -> agent_pb2.Hello:
    return agent_pb2.Hello(
        node_id=node_id,
        protocol_version=1,
        agent_version=__version__,
        hostname="laptop",
        session_epoch=epoch,
        os="Ubuntu 24.04",
        platform="linux-x86_64",
    )


async def admit(manager: SessionManager, node_id: str) -> LiveSession:
    result = await manager.admit(hello(node_id), "ipv4:127.0.0.1:1")
    assert isinstance(result, Accepted), result
    return result.live


def heartbeat(live: LiveSession, seq: int) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        heartbeat=agent_pb2.Heartbeat(sequence=seq),
    )


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


# ------------------------------------------------------ B1: stalled database


@pytest.mark.asyncio
async def test_stalled_touch_never_blocks_heartbeats_or_the_lease_timer() -> None:
    """The reviewer's reproduction: ``touch_node`` hangs, 15 heartbeats at 0.1 s
    (on the fake clock) - every one must be handled at once, the session must stay
    ONLINE, and the timer must keep evaluating a *second* session's lease."""
    reg, clock = StallingRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    live = await admit(manager, new_node_id())
    silent = await admit(manager, new_node_id())  # never heartbeats
    await timer.tick(clock.now)  # first pings; nothing else due

    for i in range(1, 16):
        clock.now = NOW0 + i * 0.1
        await asyncio.wait_for(manager.handle_message(live, heartbeat(live, i)), 0.05)
        await timer.tick(clock.now)
    assert live.heartbeats == 15  # all handled, none waited on the database
    assert live.state is NodeState.ONLINE and not live.ended
    await settle()  # let the chain task pick up its one write
    assert reg.started == ["touch_node"]  # the one coalesced write, still hung
    assert reg.calls == []
    assert live.writer.pending == 0  # the hung one is in flight, nothing queued

    # The second session's lease is still evaluated while the first one's write
    # hangs: SUSPECT at +2.5 s, OFFLINE + close at +6 s - all in memory.
    clock.now = NOW0 + 2.5
    await manager.handle_message(live, heartbeat(live, 16))  # keeps heartbeating
    await asyncio.wait_for(timer.tick(clock.now), 0.05)
    assert silent.state is NodeState.SUSPECT
    clock.now = NOW0 + 6.0
    await manager.handle_message(live, heartbeat(live, 17))
    await asyncio.wait_for(timer.tick(clock.now), 0.05)
    assert silent.state is NodeState.OFFLINE
    assert silent.ended and silent.end_reason is SessionEndReason.LEASE_EXPIRED
    assert live.state is NodeState.ONLINE and not live.ended  # heartbeating one
    assert reg.calls == []  # the database has still seen nothing complete

    # When the database comes back every queued write lands, in order.
    reg.gate.set()
    await settle()
    assert ("touch_node", live.node_id) in reg.calls
    silent_writes = [c for c in reg.calls if c[0] != "touch_node"]
    assert silent_writes == [
        ("set_node_state", NodeState.SUSPECT),
        ("set_node_state", NodeState.OFFLINE),
        ("close_session", SessionEndReason.LEASE_EXPIRED),
    ]
    assert silent.writer.done()  # the chain ended after the close
    assert not live.writer.done()  # the live session's chain keeps serving


@pytest.mark.asyncio
async def test_end_session_does_not_wait_for_a_hung_registry() -> None:
    reg, clock = StallingRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, new_node_id())
    await manager.handle_message(live, heartbeat(live, 1))
    await settle()
    assert reg.started == ["touch_node"]
    await asyncio.wait_for(
        manager.end_session(live, SessionEndReason.AGENT_CLOSED), 0.05
    )
    assert live.ended and manager.get(live.node_id) is None
    # STREAM_CLOSED's OFFLINE and the close wait behind the hung touch (D19).
    assert live.writer.pending == 2
    reg.gate.set()
    await settle()
    assert reg.calls == [
        ("touch_node", live.node_id),
        ("set_node_state", NodeState.OFFLINE),
        ("close_session", SessionEndReason.AGENT_CLOSED),
    ]


@pytest.mark.asyncio
async def test_writes_reach_the_registry_in_order_even_when_the_first_is_slow() -> None:
    """SUSPECT -> ONLINE -> close: the database must see exactly that sequence."""
    reg, clock = StallingRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, new_node_id())
    await manager.apply_event(live, LivenessEvent.LEASE_MISSED, silence_s=2.5)
    await settle()
    assert reg.started == ["set_node_state"] and reg.calls == []  # hung
    clock.now += 3.0
    await manager.handle_message(live, heartbeat(live, 1))  # SUSPECT -> ONLINE
    await manager.end_session(live, SessionEndReason.AGENT_CLOSED)
    assert live.state is NodeState.OFFLINE and live.ended  # STREAM_CLOSED (D19)
    assert reg.calls == []  # nothing has landed yet
    # set_state(ONLINE), touch, set_state(OFFLINE) for the clean close (D19), close
    assert live.writer.pending == 4

    reg.gate.set()
    await settle()
    assert [c for c in reg.calls if c[0] != "touch_node"] == [
        ("set_node_state", NodeState.SUSPECT),
        ("set_node_state", NodeState.ONLINE),
        ("set_node_state", NodeState.OFFLINE),
        ("close_session", SessionEndReason.AGENT_CLOSED),
    ]
    assert reg.calls.index(("touch_node", live.node_id)) == 2  # between them
    assert live.writer.done()


@pytest.mark.asyncio
async def test_failed_write_is_logged_and_the_chain_continues() -> None:
    reg, clock = StallingRegistry(), FakeClock()
    reg.gate.set()  # not stalled: fails instead
    reg.fail_next = RegistryError("db gone", details={"operation": "set_node_state"})
    manager = make_manager(reg, clock)
    live = await admit(manager, new_node_id())
    with structlog.testing.capture_logs() as logs:
        await manager.apply_event(live, LivenessEvent.LEASE_MISSED, silence_s=2.5)
        await manager.end_session(live, SessionEndReason.LEASE_EXPIRED)
        await settle()
    (failed,) = [e for e in logs if e["event"] == "registry.write_failed"]
    assert failed["log_level"] == "error"
    assert failed["op"] == "set_node_state"
    assert failed["node_id"] == live.node_id
    assert failed["session_id"] == live.session_id
    assert failed["error"] == "db gone"
    # The failure did not stop the close behind it.
    assert reg.calls == [("close_session", SessionEndReason.LEASE_EXPIRED)]
    assert live.writer.failed == 1 and live.writer.completed == 1
    assert live.writer.done()


@pytest.mark.asyncio
async def test_shutdown_drains_the_chains_within_the_budget() -> None:
    reg, clock = StallingRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    lives = [await admit(manager, new_node_id()) for _ in range(3)]
    for live in lives:
        await manager.handle_message(live, heartbeat(live, 1))
    await settle()
    assert len(reg.started) == 3  # one hung touch per session

    async def release_soon() -> None:
        await asyncio.sleep(0.01)
        reg.gate.set()

    releaser = asyncio.create_task(release_soon())
    await asyncio.wait_for(manager.shutdown(drain_s=1.0), 0.5)
    await releaser
    closes = [c for c in reg.calls if c[0] == "close_session"]
    assert closes == [("close_session", SessionEndReason.CONTROLLER_SHUTDOWN)] * 3
    assert all(live.writer.done() for live in lives)


@pytest.mark.asyncio
async def test_shutdown_abandons_chains_past_the_budget_and_logs() -> None:
    reg, clock = StallingRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, new_node_id())
    await manager.handle_message(live, heartbeat(live, 1))
    await settle()
    with structlog.testing.capture_logs() as logs:
        await asyncio.wait_for(manager.shutdown(drain_s=0.02), 0.5)
    (abandoned,) = [e for e in logs if e["event"] == "registry.write_abandoned"]
    assert abandoned["session_id"] == live.session_id
    # the hung touch + STREAM_CLOSED's OFFLINE (D19) + the queued close
    assert abandoned["pending"] == 3
    assert live.writer.done()  # cancelled, not leaked
    assert reg.calls == []
    drained = [e for e in logs if e["event"] == "session.writes_drained"]
    assert drained and drained[0]["abandoned"] == 1


# ------------------------------------------------------ WriteChain in isolation


async def _noop() -> None:
    return None


@pytest.mark.asyncio
async def test_overflow_evicts_oldest_touch_but_never_state_or_end() -> None:
    chain = WriteChain(node_id="node_x", session_id="sess_x", capacity=3)
    # Not started: nothing is consumed, so the queue really fills.
    with structlog.testing.capture_logs() as logs:
        assert chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        assert chain.enqueue("set_node_state", WriteKind.STATE, _noop)
        assert chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        assert chain.pending == 3
        # Full: a state write evicts the oldest touch.
        assert chain.enqueue("set_node_state", WriteKind.STATE, _noop)
        assert chain.pending == 3
        # Full again: the second touch goes; then nothing droppable is left...
        assert chain.enqueue("set_node_state", WriteKind.STATE, _noop)
        assert chain.pending == 3
        # ...so a new touch is dropped, while state/end writes are still kept.
        assert not chain.enqueue("touch_node", WriteKind.TOUCH, _noop)
        assert chain.pending == 3
        assert chain.enqueue("set_node_state", WriteKind.STATE, _noop)
        assert chain.enqueue("close_session", WriteKind.END, _noop)
        assert chain.pending == 5
        # After END nothing more is accepted.
        assert not chain.enqueue("set_node_state", WriteKind.STATE, _noop)
    assert chain.dropped == 4  # 2 evicted + 1 newest touch + 1 after close
    events = [e["event"] for e in logs]
    assert events.count("registry.write_dropped") == 3
    assert events.count("registry.write_queue_over_capacity") == 2
    assert events.count("registry.write_after_close") == 1
    over = [e for e in logs if e["event"] == "registry.write_queue_over_capacity"]
    assert all(e["log_level"] == "error" for e in over)
    kinds = [e["kind"] for e in logs if e["event"] == "registry.write_dropped"]
    assert kinds == ["touch", "touch", "touch"]  # never a state/end write

    executed: list[str] = []

    async def record(name: str) -> None:
        executed.append(name)

    # Everything left is executed in order when the consumer starts.
    chain2 = WriteChain(node_id="n", session_id="s", capacity=10)
    for name in ("a", "b", "c"):
        chain2.enqueue(name, WriteKind.STATE, lambda n=name: record(n))
    chain2.enqueue("end", WriteKind.END, lambda: record("end"))
    chain2.start()
    await asyncio.wait_for(chain2.drain(1.0), 0.5)
    assert executed == ["a", "b", "c", "end"]
    assert chain2.completed == 4 and chain2.done()


@pytest.mark.asyncio
async def test_unexpected_exception_in_a_write_does_not_kill_the_chain() -> None:
    chain = WriteChain(node_id="n", session_id="s")
    executed: list[str] = []

    async def boom() -> None:
        raise RuntimeError("bug in one write")

    async def ok() -> None:
        executed.append("ok")

    chain.start()
    with structlog.testing.capture_logs() as logs:
        chain.enqueue("boom", WriteKind.STATE, boom)
        chain.enqueue("ok", WriteKind.END, ok)
        assert await asyncio.wait_for(chain.drain(1.0), 0.5)
    assert executed == ["ok"]
    (failed,) = [e for e in logs if e["event"] == "registry.write_failed"]
    assert failed["failure_type"] == "internal_error" and failed["op"] == "boom"


def test_write_chain_rejects_a_zero_capacity() -> None:
    with pytest.raises(ValueError, match="capacity"):
        WriteChain(node_id="n", session_id="s", capacity=0)
