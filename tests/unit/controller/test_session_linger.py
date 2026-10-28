"""V0.3 G6 unit tests: how a session ends decides the liveness arrow (D19), the
lingering lease, and fenced state writes (D20).

ARCHITECTURE §12.3 (2026-09-24 amendment) is the spec. No database, no sockets, no
wall clock: an in-memory registry that is fenced exactly like the real one
(``set_node_state`` lands only while ``last_session_id`` matches) and can hold its
state writes on a gate, a clock the test moves by hand, and a
:class:`~meshforge.controller.session.leases.LeaseTimer` ticked explicitly. No
``sleep`` longer than 50 ms.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

import grpc
import pytest
import structlog

from meshforge import __version__
from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, LivenessEvent, NodeState
from meshforge.controller.registry import (
    NodeRecord,
    Registry,
    SessionEndReason,
)
from meshforge.controller.session.leases import LeaseTimer
from meshforge.controller.session.manager import (
    Accepted,
    LiveSession,
    Rejected,
    SessionManager,
    end_event,
)

pytestmark = pytest.mark.unit

RejectCode = agent_pb2.HelloAck.RejectCode
CFG = LivenessConfig()  # heartbeat 2 s -> SUSPECT at 2.5 s, OFFLINE at 6 s
NOW0 = 1000.0
EPS = 1e-6


class FakeClock:
    def __init__(self, start: float = NOW0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FencedRegistry:
    """In-memory registry with the real D20 fence and an optional state-write gate.

    ``writes`` records every *attempted* write in order as ``(op, session_id,
    value, landed)``; ``gate`` (set by default) holds ``set_node_state`` calls
    until the test releases it, so a write can be left queued on a chain.
    """

    def __init__(self) -> None:
        self.nodes: dict[str, NodeRecord] = {}
        self.writes: list[tuple[str, str, Any, bool]] = []
        self.gate = asyncio.Event()
        self.gate.set()
        self.state_calls_started = 0

    def states_written(self, node_id: str) -> list[tuple[str, NodeState, bool]]:
        return [(s, v, ok) for op, s, v, ok in self.writes if op == "set_node_state"]

    async def get_node(self, node_id: str) -> NodeRecord | None:
        return self.nodes.get(node_id)

    async def upsert_node_on_hello(self, **kw: Any) -> NodeRecord:
        now = dt.datetime.now(dt.UTC)
        prev = self.nodes.get(kw["node_id"])
        rec = NodeRecord(
            node_id=kw["node_id"],
            hostname=kw["hostname"],
            os=kw["os"],
            platform=kw["platform"],
            hardware=dict(cast(Mapping[str, Any], kw["hardware"])),
            state=kw["state"],
            first_seen_at=prev.first_seen_at if prev else now,
            last_seen_at=now,
            last_session_id=kw["session_id"],
            session_epoch=max(kw["session_epoch"], prev.session_epoch if prev else 0),
        )
        self.nodes[rec.node_id] = rec
        self.writes.append(("upsert", kw["session_id"], kw["state"], True))
        return rec

    async def open_session(self, **kw: Any) -> None:
        return None

    async def set_node_state(
        self, node_id: str, state: NodeState, *, session_id: str
    ) -> bool:
        self.state_calls_started += 1
        await self.gate.wait()
        rec = self.nodes[node_id]
        landed = rec.last_session_id == session_id
        if landed:
            self.nodes[node_id] = dataclasses.replace(rec, state=state)
        self.writes.append(("set_node_state", session_id, state, landed))
        return landed

    async def close_session(self, session_id: str, reason: SessionEndReason) -> bool:
        self.writes.append(("close_session", session_id, reason, True))
        return True

    async def touch_node(self, node_id: str) -> None:
        return None

    async def record_rtt(self, session_id: str, **kw: Any) -> None:
        return None


def make(
    clock: FakeClock | None = None, **kw: Any
) -> tuple[SessionManager, FencedRegistry, FakeClock, LeaseTimer]:
    reg = FencedRegistry()
    clk = clock or FakeClock()
    manager = SessionManager(
        cast(Registry, reg),
        CFG,
        clock=clk,
        wall_clock=lambda: dt.datetime(2026, 9, 24, tzinfo=dt.UTC),
        **kw,
    )
    return manager, reg, clk, LeaseTimer(manager, clock=clk)


def hello(node_id: str, epoch: int) -> agent_pb2.Hello:
    return agent_pb2.Hello(
        node_id=node_id,
        protocol_version=1,
        agent_version=__version__,
        hostname="laptop",
        session_epoch=epoch,
        os="Ubuntu 24.04",
        platform="linux-x86_64",
    )


async def admit(manager: SessionManager, node_id: str, epoch: int) -> LiveSession:
    result = await manager.admit(hello(node_id, epoch), "ipv4:127.0.0.1:1")
    assert isinstance(result, Accepted), result
    return result.live


def heartbeat(live: LiveSession) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        heartbeat=agent_pb2.Heartbeat(sequence=1),
    )


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


# ------------------------------------------------------------ classification


@pytest.mark.parametrize(
    ("reason", "event"),
    [
        (SessionEndReason.AGENT_CLOSED, LivenessEvent.STREAM_CLOSED),
        (SessionEndReason.CONTROLLER_SHUTDOWN, LivenessEvent.STREAM_CLOSED),
        (SessionEndReason.STREAM_LOST, LivenessEvent.STREAM_LOST),
        (SessionEndReason.PROTOCOL_ERROR, LivenessEvent.STREAM_LOST),
        (SessionEndReason.INTERNAL_ERROR, LivenessEvent.STREAM_LOST),
        (SessionEndReason.SUPERSEDED, None),
        (SessionEndReason.LEASE_EXPIRED, None),
        (SessionEndReason.CONTROLLER_RESTART, None),
        # V0.7 G3 fix r1 (D155): the certificate can never be used again -
        # OFFLINE now (node-loss path at once), no linger.
        (SessionEndReason.CERT_SUPERSEDED, LivenessEvent.STREAM_CLOSED),
        (SessionEndReason.CERT_EXPIRED, LivenessEvent.STREAM_CLOSED),
    ],
)
def test_every_end_reason_maps_to_exactly_one_arrow_or_none(
    reason: SessionEndReason, event: LivenessEvent | None
) -> None:
    assert end_event(reason) is event


def test_the_mapping_covers_every_end_reason() -> None:
    # A new end reason must be classified on purpose, not by falling through.
    classified = {
        SessionEndReason.AGENT_CLOSED,
        SessionEndReason.CONTROLLER_SHUTDOWN,
        SessionEndReason.STREAM_LOST,
        SessionEndReason.PROTOCOL_ERROR,
        SessionEndReason.INTERNAL_ERROR,
        SessionEndReason.SUPERSEDED,
        SessionEndReason.LEASE_EXPIRED,
        SessionEndReason.CONTROLLER_RESTART,
        SessionEndReason.REVOKED,  # V0.7 G3: REVOKE applied before the cut
        SessionEndReason.CERT_SUPERSEDED,  # V0.7 G3 fix r1: OFFLINE now
        SessionEndReason.CERT_EXPIRED,  # V0.7 G3 fix r1: OFFLINE now
    }
    assert classified == set(SessionEndReason)


# ------------------------------------------------------------------ clean end


@pytest.mark.asyncio
async def test_clean_close_writes_offline_before_the_close_on_the_same_chain() -> None:
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    live = await admit(manager, node, 1)
    await manager.end_session(live, SessionEndReason.AGENT_CLOSED)
    assert live.state is NodeState.OFFLINE
    assert not live.lingering and manager.get_lingering(node) is None
    await settle()
    assert reg.writes[1:] == [
        ("set_node_state", live.session_id, NodeState.OFFLINE, True),
        ("close_session", live.session_id, SessionEndReason.AGENT_CLOSED, True),
    ]
    assert live.writer.done()
    assert reg.nodes[node].state is NodeState.OFFLINE


@pytest.mark.asyncio
async def test_a_clean_close_from_suspect_is_offline_too() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    await manager.apply_event(live, LivenessEvent.LEASE_MISSED, silence_s=2.5)
    await manager.end_session(live, SessionEndReason.AGENT_CLOSED)
    assert live.state is NodeState.OFFLINE and not live.lingering


@pytest.mark.asyncio
async def test_first_reason_wins_a_late_stream_lost_cannot_relabel() -> None:
    manager, reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    await manager.end_session(live, SessionEndReason.AGENT_CLOSED)
    manager.end_session_nowait(live, SessionEndReason.STREAM_LOST)  # servicer finally
    await settle()
    assert live.end_reason is SessionEndReason.AGENT_CLOSED
    assert live.state is NodeState.OFFLINE and not live.lingering
    closes = [w for w in reg.writes if w[0] == "close_session"]
    assert closes == [
        ("close_session", live.session_id, SessionEndReason.AGENT_CLOSED, True)
    ]


# ----------------------------------------------------------------- abrupt end


@pytest.mark.asyncio
async def test_abrupt_end_is_suspect_now_and_offline_exactly_at_the_lease_edge() -> (
    None
):
    manager, reg, clock, timer = make()
    node = new_node_id()
    live = await admit(manager, node, 1)
    clock.now = NOW0 + 1.0
    await manager.handle_message(live, heartbeat(live))  # last contact: NOW0 + 1
    clock.now = NOW0 + 1.5
    with structlog.testing.capture_logs() as logs:
        await manager.end_session(live, SessionEndReason.STREAM_LOST)
    assert live.state is NodeState.SUSPECT
    assert live.lingering and manager.get_lingering(node) is live
    assert manager.get(node) is None  # connected = false
    await settle()
    assert not live.writer.done()  # the chain stays open for the linger
    assert [w for w in reg.writes if w[0] != "upsert"] == [
        ("set_node_state", live.session_id, NodeState.SUSPECT, True),
        ("close_session", live.session_id, SessionEndReason.STREAM_LOST, True),
    ]  # the close reason is persisted promptly, not when the linger resolves
    (closed,) = [e for e in logs if e["event"] == "session.closed"]
    assert closed["lingering"] is True and closed["liveness_event"] == "stream_lost"

    edge = NOW0 + 1.0 + CFG.offline_after_s  # from the last heartbeat
    assert await timer.tick(NOW0 + 1.0 + CFG.suspect_after_s) == edge  # no MISSED
    assert await timer.tick(edge - EPS) == edge
    assert live.state is NodeState.SUSPECT and live.lingering
    assert await timer.tick(edge) is None
    assert live.state is NodeState.OFFLINE
    assert not live.lingering and manager.get_lingering(node) is None
    await settle()
    assert live.writer.done()
    assert reg.writes[-1] == (
        "set_node_state",
        live.session_id,
        NodeState.OFFLINE,
        True,
    )
    assert reg.nodes[node].state is NodeState.OFFLINE


@pytest.mark.asyncio
async def test_hello_just_before_the_edge_is_online_and_offline_is_never_written() -> (
    None
):
    manager, reg, clock, timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    await manager.end_session(old, SessionEndReason.STREAM_LOST)
    edge = NOW0 + CFG.offline_after_s
    clock.now = edge - EPS
    await timer.tick(clock.now)
    assert old.lingering
    with structlog.testing.capture_logs() as logs:
        new = await admit(manager, node, 2)
    assert new.state is NodeState.ONLINE
    (change,) = [e for e in logs if e["event"] == "node.state_changed"]
    assert (change["from"], change["to"]) == ("suspect", "online")
    assert change["liveness_event"] == "reconnected"
    assert not old.lingering and manager.get_lingering(node) is None
    # Well past the old edge: nothing fires for the resolved entry.
    for t in (edge, edge + 1.0, edge + 100.0):
        clock.now = t
        await manager.handle_message(new, heartbeat(new))
        await timer.tick(t)
    await settle()
    assert old.writer.done()
    offline = [w for w in reg.writes if w[2] is NodeState.OFFLINE]
    assert offline == []
    assert reg.nodes[node].state is NodeState.ONLINE


@pytest.mark.asyncio
async def test_a_stale_epoch_hello_does_not_resolve_the_linger() -> None:
    manager, _reg, _clock, timer = make()
    node = new_node_id()
    old = await admit(manager, node, 5)
    await manager.end_session(old, SessionEndReason.STREAM_LOST)
    for stale in (5, 4):
        result = await manager.admit(hello(node, stale), "p")
        assert isinstance(result, Rejected)
        assert result.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH
    assert old.lingering and manager.get_lingering(node) is old
    assert await timer.tick(NOW0 + CFG.offline_after_s) is None
    assert old.state is NodeState.OFFLINE and not old.lingering


@pytest.mark.asyncio
async def test_the_lingering_epoch_counts_as_highest_seen() -> None:
    """Even if the persisted high-water mark lagged (a failed upsert, a
    restored backup), the lingering entry's epoch fences a Hello."""
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 7)
    await manager.end_session(old, SessionEndReason.STREAM_LOST)
    reg.nodes[node] = dataclasses.replace(reg.nodes[node], session_epoch=0)
    result = await manager.admit(hello(node, 7), "p")
    assert isinstance(result, Rejected)
    assert result.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH
    assert old.lingering
    await admit(manager, node, 8)
    assert not old.lingering


@pytest.mark.asyncio
async def test_protocol_and_internal_errors_are_abrupt_with_their_own_reason() -> None:
    manager, _reg, _clock, _timer = make()
    for reason in (SessionEndReason.PROTOCOL_ERROR, SessionEndReason.INTERNAL_ERROR):
        live = await admit(manager, new_node_id(), 1)
        await manager.end_session(live, reason)
        assert live.end_reason is reason
        assert live.state is NodeState.SUSPECT and live.lingering


@pytest.mark.asyncio
async def test_a_stream_lost_while_suspect_lingers_without_an_invalid_transition() -> (
    None
):
    manager, _reg, _clock, timer = make()
    live = await admit(manager, new_node_id(), 1)
    await timer.tick(NOW0 + CFG.suspect_after_s)  # LEASE_MISSED on the live one
    assert live.state is NodeState.SUSPECT
    with structlog.testing.capture_logs() as logs:
        await manager.end_session(live, SessionEndReason.STREAM_LOST)
        await timer.tick(NOW0 + CFG.offline_after_s)
    assert live.state is NodeState.OFFLINE
    assert not [e for e in logs if e["event"] == "liveness.invalid_transition"]


@pytest.mark.asyncio
async def test_supersession_writes_no_state_for_the_old_session() -> None:
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    old = await admit(manager, node, 1)
    new = await admit(manager, node, 2)
    await settle()
    assert old.end_reason is SessionEndReason.SUPERSEDED
    assert not old.lingering and manager.get_lingering(node) is None
    assert [w for w in reg.writes if w[1] == old.session_id and w[0] != "upsert"] == [
        ("close_session", old.session_id, SessionEndReason.SUPERSEDED, True)
    ]
    assert new.state is NodeState.ONLINE


@pytest.mark.asyncio
async def test_shutdown_writes_offline_for_live_and_lingering_and_ends_chains() -> None:
    manager, reg, _clock, _timer = make()
    live_node, gone_node = new_node_id(), new_node_id()
    live = await admit(manager, live_node, 1)
    gone = await admit(manager, gone_node, 1)
    await manager.end_session(gone, SessionEndReason.STREAM_LOST)
    await asyncio.wait_for(manager.shutdown(drain_s=0.05), 0.5)
    assert live.end_reason is SessionEndReason.CONTROLLER_SHUTDOWN
    assert live.state is NodeState.OFFLINE and gone.state is NodeState.OFFLINE
    assert not gone.lingering and manager.lingering() == []
    assert live.writer.done() and gone.writer.done()
    assert reg.nodes[live_node].state is NodeState.OFFLINE
    assert reg.nodes[gone_node].state is NodeState.OFFLINE


@pytest.mark.asyncio
async def test_the_timer_sleeps_until_the_lingering_edge() -> None:
    manager, _reg, _clock, timer = make()
    live = await admit(manager, new_node_id(), 1)
    await manager.end_session(live, SessionEndReason.STREAM_LOST)
    assert manager.wake.is_set()  # a new linger wakes the timer
    assert await timer.tick(NOW0) == NOW0 + CFG.offline_after_s


# ------------------------------------------------------------------- reader


class _Stream:
    """An async iterator the test ends normally or with an error."""

    def __init__(self, error: BaseException | None = None) -> None:
        self._end = asyncio.Event()
        self._error = error

    def end(self) -> None:
        self._end.set()

    def __aiter__(self) -> AsyncIterator[agent_pb2.AgentMessage]:
        return self

    async def __anext__(self) -> agent_pb2.AgentMessage:
        await self._end.wait()
        if self._error is not None:
            raise self._error
        raise StopAsyncIteration


class _Rpc(grpc.RpcError):
    pass


@pytest.mark.asyncio
async def test_reader_normal_end_without_goodbye_is_stream_lost_at_once() -> None:
    """V0.4 D40 (was: "agent_closed after the confirmation"): with no Goodbye a
    normal end of the iterator is abrupt - SUSPECT now, the lease lingers."""
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = _Stream()
    task = manager.start_reader(live, stream)
    stream.end()
    await asyncio.wait_for(task, 0.5)
    assert live.end_reason is SessionEndReason.STREAM_LOST
    assert live.state is NodeState.SUSPECT and live.lingering


@pytest.mark.asyncio
async def test_a_cancel_after_a_normal_end_changes_nothing() -> None:
    """The old race (a cancel arriving inside the confirmation window) is gone:
    the reader decides ``stream_lost`` on the iterator's end without waiting, and
    the handler's later ``stream_lost`` (the servicer's ``finally``) is a no-op."""
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = _Stream()
    task = manager.start_reader(live, stream)
    stream.end()
    await asyncio.wait_for(task, 0.5)
    assert not task.cancelled()  # it finished on its own - nothing to wait for
    await manager.end_session(live, SessionEndReason.STREAM_LOST)  # the handler
    assert live.end_reason is SessionEndReason.STREAM_LOST
    assert live.state is NodeState.SUSPECT and live.lingering


@pytest.mark.asyncio
async def test_reader_rpc_error_is_stream_lost() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    stream = _Stream(_Rpc())
    task = manager.start_reader(live, stream)
    stream.end()
    await asyncio.wait_for(task, 0.5)
    assert live.end_reason is SessionEndReason.STREAM_LOST and live.lingering


def test_the_half_close_confirm_window_is_gone() -> None:
    """V0.4 D40 deleted the timing rule outright (no fallback): the parameter that
    used to tune it is refused, not silently ignored."""
    with pytest.raises(TypeError, match="half_close_confirm_s"):
        make(half_close_confirm_s=0.25)


# ------------------------------------------------------------ D20: the fence


@pytest.mark.asyncio
async def test_a_backlogged_old_write_is_fenced_after_a_reconnect() -> None:
    """The race D20 closes: the old session's OFFLINE is queued on its chain
    (held on the gate) while the newer session is admitted; released, it must
    not overwrite the newer session's ONLINE."""
    manager, reg, _clock, _timer = make()
    node = new_node_id()
    reg.gate.clear()
    old = await admit(manager, node, 1)
    await manager.end_session(old, SessionEndReason.AGENT_CLOSED)  # OFFLINE queued
    await settle()
    assert reg.state_calls_started == 1  # held on the gate
    new = await admit(manager, node, 2)
    assert reg.nodes[node].state is NodeState.ONLINE
    with structlog.testing.capture_logs():
        reg.gate.set()
        await settle()
    assert ("set_node_state", old.session_id, NodeState.OFFLINE, False) in reg.writes
    assert reg.nodes[node].state is NodeState.ONLINE
    assert manager.get(node) is new  # connected


@pytest.mark.asyncio
async def test_every_session_state_write_carries_its_session_id() -> None:
    manager, reg, clock, timer = make()
    node = new_node_id()
    live = await admit(manager, node, 1)
    await timer.tick(NOW0 + CFG.suspect_after_s)
    clock.now = NOW0 + 3.0
    await manager.handle_message(live, heartbeat(live))
    await manager.end_session(live, SessionEndReason.STREAM_LOST)
    await timer.tick(NOW0 + 3.0 + CFG.offline_after_s)
    await settle()
    states = [w for w in reg.writes if w[0] == "set_node_state"]
    assert [w[2] for w in states] == [
        NodeState.SUSPECT,
        NodeState.ONLINE,
        NodeState.SUSPECT,
        NodeState.OFFLINE,
    ]
    assert {w[1] for w in states} == {live.session_id}
    assert all(w[3] for w in states)
