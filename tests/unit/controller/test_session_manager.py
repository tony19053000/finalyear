"""V0.3 G2 unit tests: SessionManager dispatch/dedupe/fencing and the LeaseTimer.

No database, no sockets, no wall clock: the registry is an in-memory fake that
records calls (and can be told to fail with the typed ``RegistryError``), the clock
is a number the test moves by hand, and the only real asyncio is the event loop
the manager's lock/queue/tasks need. No ``sleep`` longer than 50 ms.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import hashlib
from collections.abc import AsyncIterator, Mapping
from typing import Any, cast

import grpc
import pytest
import structlog
from ulid import ULID

from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig, LivenessEvent, NodeState
from meshforge.controller.registry import (
    NodeRecord,
    Registry,
    RegistryError,
    SessionEndReason,
    SessionRecord,
)
from meshforge.controller.session import epoch as epoch_module
from meshforge.controller.session import manager as manager_module
from meshforge.controller.session.errors import (
    OutboundOverflowError,
    SessionProtocolError,
)
from meshforge.controller.session.leases import LeaseTimer
from meshforge.controller.session.manager import (
    Accepted,
    LiveSession,
    Rejected,
    SessionManager,
    update_ewma,
)

pytestmark = pytest.mark.unit

RejectCode = agent_pb2.HelloAck.RejectCode
CFG = LivenessConfig()  # 2 s / +0.5 s / x3
NOW0 = 1000.0


class FakeClock:
    def __init__(self, start: float = NOW0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


class FakeRegistry:
    """Duck-typed stand-in for :class:`Registry` that records every call."""

    def __init__(self) -> None:
        self.nodes: dict[str, NodeRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.fail_with: RegistryError | None = None

    def _record(self, name: str, *args: Any, **kwargs: Any) -> None:
        self.calls.append((name, args, kwargs))
        if self.fail_with is not None:
            raise self.fail_with

    def calls_named(self, name: str) -> list[tuple[Any, ...]]:
        return [(a, k) for n, a, k in self.calls if n == name]

    async def get_node(self, node_id: str) -> NodeRecord | None:
        self._record("get_node", node_id)
        return self.nodes.get(node_id)

    async def upsert_node_on_hello(
        self,
        *,
        node_id: str,
        hostname: str,
        os: str,
        platform: str,
        hardware: Mapping[str, Any],
        session_epoch: int,
        session_id: str,
        state: NodeState,
    ) -> NodeRecord:
        self._record("upsert_node_on_hello", node_id=node_id, state=state)
        now = dt.datetime.now(dt.UTC)
        prev = self.nodes.get(node_id)
        rec = NodeRecord(
            node_id=node_id,
            hostname=hostname,
            os=os,
            platform=platform,
            hardware=dict(hardware),
            state=state,
            first_seen_at=prev.first_seen_at if prev else now,
            last_seen_at=now,
            last_session_id=session_id,
            session_epoch=session_epoch,
        )
        self.nodes[node_id] = rec
        return rec

    async def open_session(
        self,
        *,
        session_id: str,
        node_id: str,
        session_epoch: int,
        peer_address: str,
        agent_version: str,
        protocol_version: int,
    ) -> SessionRecord:
        self._record("open_session", session_id=session_id, node_id=node_id)
        rec = SessionRecord(
            session_id=session_id,
            node_id=node_id,
            session_epoch=session_epoch,
            started_at=dt.datetime.now(dt.UTC),
            ended_at=None,
            end_reason=None,
            peer_address=peer_address,
            agent_version=agent_version,
            protocol_version=protocol_version,
            last_rtt_ms=None,
            rtt_ewma_ms=None,
        )
        self.sessions[session_id] = rec
        return rec

    async def close_session(
        self, session_id: str, end_reason: SessionEndReason
    ) -> bool:
        self._record("close_session", session_id, end_reason)
        rec = self.sessions[session_id]
        if rec.ended_at is not None:
            return False
        self.sessions[session_id] = dataclasses.replace(
            rec, ended_at=dt.datetime.now(dt.UTC), end_reason=end_reason
        )
        return True

    async def set_node_state(self, node_id: str, state: NodeState) -> None:
        self._record("set_node_state", node_id, state)
        rec = self.nodes[node_id]
        self.nodes[node_id] = dataclasses.replace(rec, state=state)

    async def touch_node(self, node_id: str) -> None:
        self._record("touch_node", node_id)

    async def record_rtt(
        self, session_id: str, *, last_ms: float, ewma_ms: float
    ) -> None:
        self._record("record_rtt", session_id, last_ms=last_ms, ewma_ms=ewma_ms)


def make_manager(
    registry: FakeRegistry, clock: FakeClock, **kwargs: Any
) -> SessionManager:
    return SessionManager(
        cast(Registry, registry),
        CFG,
        clock=clock,
        wall_clock=lambda: dt.datetime(2026, 9, 20, tzinfo=dt.UTC),
        **kwargs,
    )


def hello(node_id: str, epoch: int = 1, version: int = 1) -> agent_pb2.Hello:
    return agent_pb2.Hello(
        node_id=node_id,
        protocol_version=version,
        agent_version="0.1.0.dev0",
        hostname="laptop",
        session_epoch=epoch,
        os="Ubuntu 24.04",
        platform="linux-x86_64",
        hardware=agent_pb2.HardwareSummary(cpu_model="cpu", cpu_cores=4),
    )


async def admit(manager: SessionManager, h: agent_pb2.Hello) -> LiveSession:
    result = await manager.admit(h, "ipv4:127.0.0.1:1")
    assert isinstance(result, Accepted), result
    return result.live


def mid(label: str) -> str:
    """A valid, canonical ``cmd_<ULID>`` derived deterministically from ``label``
    (the same label always gives the same id, so duplicates can be staged)."""
    digest = hashlib.sha256(label.encode()).digest()[:16]
    return f"cmd_{ULID.from_bytes(digest)}"


def heartbeat(
    live: LiveSession, message_id: str, seq: int = 1
) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=mid(message_id),
        session_id=live.session_id,
        heartbeat=agent_pb2.Heartbeat(sequence=seq),
    )


def drain(live: LiveSession) -> list[agent_pb2.ControllerMessage | None]:
    out: list[agent_pb2.ControllerMessage | None] = []
    while not live.outbound.empty():
        out.append(live.outbound.get_nowait())
    return out


class QueueStream:
    """Async iterator fed by the test; ``close()`` ends it like a half-closed stream."""

    def __init__(self) -> None:
        self._q: asyncio.Queue[agent_pb2.AgentMessage | None] = asyncio.Queue()

    def feed(self, msg: agent_pb2.AgentMessage) -> None:
        self._q.put_nowait(msg)

    def close(self) -> None:
        self._q.put_nowait(None)

    def __aiter__(self) -> AsyncIterator[agent_pb2.AgentMessage]:
        return self

    async def __anext__(self) -> agent_pb2.AgentMessage:
        item = await self._q.get()
        if item is None:
            raise StopAsyncIteration
        return item


async def settle() -> None:
    """Let queued callbacks/tasks run (no timing dependence)."""
    for _ in range(5):
        await asyncio.sleep(0)


# ---------------------------------------------------------------------- admit


@pytest.mark.asyncio
async def test_first_hello_enrolls_and_ack_carries_liveness_intervals() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    result = await manager.admit(hello(node_id), "ipv4:127.0.0.1:5")
    assert isinstance(result, Accepted)
    live = result.live
    assert live.state is NodeState.ONLINE
    assert reg.nodes[node_id].state is NodeState.ONLINE
    assert reg.nodes[node_id].hardware == {"cpu_model": "cpu", "cpu_cores": 4}
    assert reg.sessions[live.session_id].peer_address == "ipv4:127.0.0.1:5"
    ack = result.hello_ack.hello_ack
    assert ack.accepted is True
    assert ack.session_id == live.session_id
    assert ack.heartbeat_interval_ms == 2000
    assert ack.lease_ms == 6000  # offline_after_s from LivenessConfig
    assert ack.controller_time.ToDatetime(dt.UTC) == dt.datetime(
        2026, 9, 20, tzinfo=dt.UTC
    )
    assert manager.wake.is_set()
    assert len(manager) == 1
    assert live.next_ping_at == clock.now  # first RTT sample on the next tick


@pytest.mark.asyncio
async def test_protocol_version_out_of_range_is_rejected_before_any_db_write() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg, FakeClock(), accepted_protocol_versions=(1, 1))
    for version in (0, 2, 99):
        result = await manager.admit(hello(new_node_id(), version=version), "p")
        assert isinstance(result, Rejected)
        assert result.reject_code == RejectCode.REJECT_CODE_PROTOCOL_VERSION
        assert result.abort_status is None
    assert reg.calls == []
    assert len(manager) == 0


@pytest.mark.asyncio
async def test_invalid_node_id_aborts_with_invalid_argument() -> None:
    manager = make_manager(FakeRegistry(), FakeClock())
    result = await manager.admit(hello("not-a-node-id"), "p")
    assert isinstance(result, Rejected)
    assert result.abort_status is grpc.StatusCode.INVALID_ARGUMENT


@pytest.mark.asyncio
async def test_stale_or_equal_epoch_is_rejected_and_live_session_untouched() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id, epoch=5))
    for epoch in (5, 4, 0):
        result = await manager.admit(hello(node_id, epoch=epoch), "p2")
        assert isinstance(result, Rejected)
        assert result.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH
        assert result.abort_status is None
    assert manager.get(node_id) is live
    assert not live.ended
    assert len(reg.sessions) == 1


@pytest.mark.asyncio
async def test_epoch_fencing_uses_the_persisted_high_water_mark() -> None:
    """ADR-0002 Updates §1 across a disconnect: with no live session left, a Hello
    whose epoch is <= the highest epoch the registry ever stored is still stale;
    the next strictly higher one is a normal reconnect. One get_node per Hello."""
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    winner = await admit(manager, hello(node_id, epoch=7))
    await manager.end_session(winner, SessionEndReason.AGENT_CLOSED)
    await settle()
    assert manager.get(node_id) is None

    gets_before = len(reg.calls_named("get_node"))
    for epoch in (7, 3, 0):  # equal is stale too (agent increments per stream)
        result = await manager.admit(hello(node_id, epoch=epoch), "p2")
        assert isinstance(result, Rejected), epoch
        assert result.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH
        assert result.abort_status is None
    assert len(reg.calls_named("get_node")) - gets_before == 3
    assert reg.nodes[node_id].session_epoch == 7  # never moved backwards
    assert len(reg.sessions) == 1  # nothing persisted for the stale Hellos

    live = await admit(manager, hello(node_id, epoch=8))
    assert live.state is NodeState.ONLINE  # OFFLINE -> RECONNECTED -> ONLINE
    assert reg.nodes[node_id].session_epoch == 8


@pytest.mark.asyncio
async def test_max_session_epoch_is_the_wire_domain_and_a_fresh_node_may_use_it() -> (
    None
):
    assert epoch_module.MAX_SESSION_EPOCH == 2**64 - 1  # protobuf uint64
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id, epoch=2**64 - 1))
    assert live.session_epoch == 2**64 - 1
    assert reg.nodes[node_id].session_epoch == 2**64 - 1


@pytest.mark.asyncio
async def test_epoch_above_the_bound_is_invalid_argument_and_never_persisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bound is unreachable from a protobuf uint64, so it is lowered here to
    prove the check runs before any registry call and names both numbers."""
    monkeypatch.setattr(manager_module, "MAX_SESSION_EPOCH", 10)
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    result = await manager.admit(hello(new_node_id(), epoch=11), "p")
    assert isinstance(result, Rejected)
    assert result.abort_status is grpc.StatusCode.INVALID_ARGUMENT
    assert result.reason == "session_epoch 11 exceeds the controller's maximum 10"
    assert reg.calls == [] and reg.nodes == {} and reg.sessions == {}
    assert isinstance(
        await manager.admit(hello(new_node_id(), epoch=10), "p"), Accepted
    )


@pytest.mark.asyncio
async def test_higher_epoch_supersedes_and_cancels_old_reader() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    old = await admit(manager, hello(node_id, epoch=1))
    stream = QueueStream()
    reader = manager.start_reader(old, stream)
    await settle()

    new = await admit(manager, hello(node_id, epoch=2))
    await settle()
    assert old.ended and old.end_reason is SessionEndReason.SUPERSEDED
    assert reader.cancelled()
    assert drain(old) == [None]  # the old stream gets nothing more, just the end
    assert reg.sessions[old.session_id].end_reason is SessionEndReason.SUPERSEDED
    assert reg.sessions[new.session_id].is_open
    assert manager.get(node_id) is new
    assert new.state is NodeState.ONLINE  # ONLINE + HEARTBEAT self-loop, no change log
    assert reg.calls_named("set_node_state") == []


@pytest.mark.asyncio
async def test_hello_after_offline_is_reconnected() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id, epoch=1))
    clock.now += 6.0
    for event in (LivenessEvent.LEASE_MISSED, LivenessEvent.LEASE_EXPIRED):
        await manager.apply_event(live, event, silence_s=6.0)
    await manager.end_session(live, SessionEndReason.LEASE_EXPIRED)
    await settle()  # the writes ride the session's chain
    assert reg.nodes[node_id].state is NodeState.OFFLINE

    with structlog.testing.capture_logs() as logs:
        again = await admit(manager, hello(node_id, epoch=2))
    assert again.state is NodeState.ONLINE
    change = [e for e in logs if e["event"] == "node.state_changed"]
    assert change and change[0]["from"] == "offline" and change[0]["to"] == "online"
    assert change[0]["liveness_event"] == "reconnected"
    assert (
        change[0]["node_id"] == node_id and change[0]["session_id"] == again.session_id
    )


@pytest.mark.asyncio
async def test_hello_while_suspect_is_reconnected_too() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id, epoch=1))
    await manager.apply_event(live, LivenessEvent.LEASE_MISSED, silence_s=2.6)
    assert live.state is NodeState.SUSPECT
    new = await admit(manager, hello(node_id, epoch=2))
    assert new.state is NodeState.ONLINE
    assert live.ended and live.end_reason is SessionEndReason.SUPERSEDED


@pytest.mark.asyncio
async def test_revoked_node_is_refused_with_permission_denied() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg, FakeClock())
    node_id = new_node_id()
    await admit(manager, hello(node_id))
    await manager.shutdown()
    await reg.set_node_state(node_id, NodeState.REVOKED)
    manager2 = make_manager(reg, FakeClock())
    result = await manager2.admit(hello(node_id, epoch=2), "p")
    assert isinstance(result, Rejected)
    assert result.abort_status is grpc.StatusCode.PERMISSION_DENIED


@pytest.mark.asyncio
async def test_hello_during_shutdown_is_unavailable() -> None:
    manager = make_manager(FakeRegistry(), FakeClock())
    await manager.shutdown()
    result = await manager.admit(hello(new_node_id()), "p")
    assert isinstance(result, Rejected)
    assert result.abort_status is grpc.StatusCode.UNAVAILABLE


# ------------------------------------------------------------------- dispatch


@pytest.mark.asyncio
async def test_heartbeat_resets_lease_and_is_acked() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    clock.now += 1.5
    await manager.handle_message(live, heartbeat(live, "m-1"))
    assert live.tracker.last_heartbeat == clock.now
    assert live.heartbeats == 1
    out = drain(live)
    assert len(out) == 1 and out[0] is not None
    assert out[0].WhichOneof("payload") == "ack"
    assert out[0].ack.acked_message_id == mid("m-1")
    assert out[0].session_id == live.session_id
    await settle()
    assert reg.calls_named("touch_node") == [((live.node_id,), {})]


@pytest.mark.asyncio
async def test_heartbeat_burst_in_one_interval_costs_exactly_one_touch_write() -> None:
    """Coalescing: the first heartbeat of a window writes ``last_seen_at``; the
    next 999 within ``heartbeat_interval_s`` only extend the lease in memory."""
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    for i in range(1000):
        clock.now = NOW0 + i * (CFG.heartbeat_interval_s / 1000)  # all inside 2 s
        await manager.handle_message(live, heartbeat(live, f"hb-{i}", seq=i))
    assert live.heartbeats == 1000
    assert live.tracker.last_heartbeat == clock.now  # every one extended the lease
    await settle()
    assert reg.calls_named("touch_node") == [((live.node_id,), {})]
    assert live.touch_dirty is True
    assert (
        live.touch_due_at(CFG.heartbeat_interval_s) == NOW0 + CFG.heartbeat_interval_s
    )
    assert len(drain(live)) == 1000  # every heartbeat still acked


@pytest.mark.asyncio
async def test_pending_touch_is_flushed_by_the_timer_once_the_window_closes() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    live = await admit(manager, hello(new_node_id()))
    await manager.handle_message(live, heartbeat(live, "a"))  # writes (window opens)
    clock.now += 0.5
    await manager.handle_message(live, heartbeat(live, "b"))  # coalesced -> dirty
    await settle()
    assert len(reg.calls_named("touch_node")) == 1

    # The timer schedules the flush, and a tick before it is due does nothing.
    next_deadline = await timer.tick(clock.now)
    assert next_deadline == NOW0 + CFG.heartbeat_interval_s
    assert len(reg.calls_named("touch_node")) == 1 and live.touch_dirty

    clock.now = NOW0 + CFG.heartbeat_interval_s
    await timer.tick(clock.now)
    await settle()
    assert len(reg.calls_named("touch_node")) == 2
    assert not live.touch_dirty
    assert live.touch_due_at(CFG.heartbeat_interval_s) is None

    # A clean session is not written again by later ticks...
    clock.now += CFG.heartbeat_interval_s
    await timer.tick(clock.now)
    await settle()
    assert len(reg.calls_named("touch_node")) == 2
    # ...and a heartbeat after the window writes immediately again.
    await manager.handle_message(live, heartbeat(live, "c"))
    await settle()
    assert len(reg.calls_named("touch_node")) == 3


@pytest.mark.asyncio
async def test_touch_is_not_flushed_for_an_ended_session() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    await manager.handle_message(live, heartbeat(live, "a"))
    clock.now += 0.1
    await manager.handle_message(live, heartbeat(live, "b"))
    assert live.touch_dirty
    await manager.end_session(live, SessionEndReason.AGENT_CLOSED)
    clock.now += CFG.heartbeat_interval_s
    await manager.flush_touch(live, clock.now)
    await settle()
    assert len(reg.calls_named("touch_node")) == 1


@pytest.mark.asyncio
async def test_duplicate_message_id_is_processed_once_but_re_acked() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    await manager.handle_message(live, heartbeat(live, "dup", seq=1))
    await manager.handle_message(live, heartbeat(live, "dup", seq=2))
    assert live.heartbeats == 1
    assert live.duplicates == 1
    acks = [
        m for m in drain(live) if m is not None and m.WhichOneof("payload") == "ack"
    ]
    assert [a.ack.acked_message_id for a in acks] == [mid("dup"), mid("dup")]


@pytest.mark.asyncio
async def test_dedupe_window_forgets_oldest_ids() -> None:
    manager = make_manager(FakeRegistry(), FakeClock(), dedupe_window=3)
    live = await admit(manager, hello(new_node_id()))
    for mid in ("a", "b", "c", "d"):  # "a" falls out of the window of 3
        await manager.handle_message(live, heartbeat(live, mid))
    await manager.handle_message(live, heartbeat(live, "a"))
    assert live.duplicates == 0
    assert live.heartbeats == 5
    await manager.handle_message(live, heartbeat(live, "d"))
    assert live.duplicates == 1


@pytest.mark.asyncio
async def test_stale_session_id_is_dropped_counted_and_not_acked() -> None:
    manager = make_manager(FakeRegistry(), FakeClock())
    live = await admit(manager, hello(new_node_id()))
    stale = agent_pb2.AgentMessage(
        message_id="s-1", session_id="sess_OTHER", heartbeat=agent_pb2.Heartbeat()
    )
    with structlog.testing.capture_logs() as logs:
        await manager.handle_message(live, stale)
        await manager.handle_message(live, stale)
    assert live.stale_messages == 2
    assert manager.stale_messages_total == 2
    assert live.heartbeats == 0
    assert drain(live) == []
    events = [e for e in logs if e["event"] == "session.stale_message"]
    assert len(events) == 2
    assert events[0]["session_id"] == live.session_id
    assert events[0]["message_session_id"] == "sess_OTHER"
    assert not live.ended  # the session itself is unaffected


@pytest.mark.asyncio
async def test_second_hello_on_open_stream_is_a_protocol_error() -> None:
    manager = make_manager(FakeRegistry(), FakeClock())
    live = await admit(manager, hello(new_node_id()))
    msg = agent_pb2.AgentMessage(message_id="h2", hello=hello(live.node_id, epoch=2))
    with pytest.raises(SessionProtocolError) as info:
        await manager.handle_message(live, msg)
    assert info.value.details == {
        "session_id": live.session_id,
        "node_id": live.node_id,
    }


@pytest.mark.asyncio
async def test_missing_message_id_or_payload_is_a_protocol_error() -> None:
    manager = make_manager(FakeRegistry(), FakeClock())
    live = await admit(manager, hello(new_node_id()))
    with pytest.raises(SessionProtocolError, match="message_id"):
        await manager.handle_message(
            live,
            agent_pb2.AgentMessage(
                session_id=live.session_id, heartbeat=agent_pb2.Heartbeat()
            ),
        )
    with pytest.raises(SessionProtocolError, match="payload"):
        await manager.handle_message(
            live,
            agent_pb2.AgentMessage(message_id=mid("x"), session_id=live.session_id),
        )


@pytest.mark.asyncio
async def test_reader_closes_session_as_protocol_error_on_second_hello() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg, FakeClock())
    live = await admit(manager, hello(new_node_id()))
    stream = QueueStream()
    reader = manager.start_reader(live, stream)
    stream.feed(heartbeat(live, "ok-1"))
    stream.feed(agent_pb2.AgentMessage(message_id="h2", hello=hello(live.node_id, 9)))
    await asyncio.wait_for(reader, 1.0)
    assert live.ended and live.end_reason is SessionEndReason.PROTOCOL_ERROR
    assert reg.sessions[live.session_id].end_reason is SessionEndReason.PROTOCOL_ERROR
    assert live.heartbeats == 1
    assert drain(live)[-1] is None  # writer unblocked


@pytest.mark.asyncio
async def test_reader_end_of_stream_closes_as_agent_closed() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg, FakeClock())
    live = await admit(manager, hello(new_node_id()))
    stream = QueueStream()
    reader = manager.start_reader(live, stream)
    stream.close()
    await asyncio.wait_for(reader, 1.0)
    assert live.end_reason is SessionEndReason.AGENT_CLOSED
    assert manager.get(live.node_id) is None


@pytest.mark.asyncio
async def test_agent_ack_is_not_acked_back() -> None:
    manager = make_manager(FakeRegistry(), FakeClock())
    live = await admit(manager, hello(new_node_id()))
    await manager.handle_message(
        live,
        agent_pb2.AgentMessage(
            message_id=mid("a-1"),
            session_id=live.session_id,
            ack=agent_pb2.Ack(acked_message_id="c"),
        ),
    )
    assert drain(live) == []


@pytest.mark.asyncio
async def test_other_reports_are_acked_and_left_for_later_versions() -> None:
    manager = make_manager(FakeRegistry(), FakeClock())
    live = await admit(manager, hello(new_node_id()))
    await manager.handle_message(
        live,
        agent_pb2.AgentMessage(
            message_id=mid("r-1"),
            session_id=live.session_id,
            resource_update=agent_pb2.ResourceUpdate(sequence=1, cpu_percent=3.0),
        ),
    )
    out = drain(live)
    assert (
        len(out) == 1
        and out[0] is not None
        and out[0].ack.acked_message_id == mid("r-1")
    )


# ------------------------------------------------------------------- overflow


@pytest.mark.asyncio
async def test_outbound_overflow_is_typed_and_closes_the_session() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg, FakeClock(), outbound_queue_size=2)
    live = await admit(manager, hello(new_node_id()))
    await manager.handle_message(live, heartbeat(live, "1"))
    await manager.handle_message(live, heartbeat(live, "2"))
    with pytest.raises(OutboundOverflowError) as info:
        await manager.handle_message(live, heartbeat(live, "3"))
    assert info.value.details["capacity"] == 2
    assert info.value.failure_type.value == "resource_busy"
    # From the timer path the overflow closes the session instead of raising.
    await manager.send_ping(live, NOW0)
    assert live.ended and live.end_reason is SessionEndReason.PROTOCOL_ERROR
    await settle()
    assert reg.sessions[live.session_id].end_reason is SessionEndReason.PROTOCOL_ERROR
    assert drain(live) == [None]  # queue cleared, sentinel delivered


# ------------------------------------------------------------------------ RTT


def test_ewma_math() -> None:
    assert update_ewma(None, 10.0) == 10.0
    assert update_ewma(10.0, 20.0) == pytest.approx(13.0)  # 0.3*20 + 0.7*10
    assert update_ewma(13.0, 20.0) == pytest.approx(15.1)
    assert update_ewma(10.0, 10.0, alpha=0.5) == 10.0


@pytest.mark.asyncio
async def test_ping_pong_measures_rtt_on_the_monotonic_clock_only() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    await manager.send_ping(live, clock.now)
    out = drain(live)
    assert (
        len(out) == 1 and out[0] is not None and out[0].WhichOneof("payload") == "ping"
    )
    ping_id = out[0].ping.ping_id
    assert live.outstanding_ping == (ping_id, NOW0)
    assert live.next_ping_at == NOW0 + CFG.offline_after_s

    clock.now += 0.0125  # 12.5 ms later; agent_time is deliberately absurd (skew)
    pong = agent_pb2.AgentMessage(
        message_id=mid("p-1"),
        session_id=live.session_id,
        pong=agent_pb2.Pong(ping_id=ping_id),
    )
    await manager.handle_message(live, pong)
    assert live.last_rtt_ms == pytest.approx(12.5)
    assert live.rtt_ewma_ms == pytest.approx(12.5)
    assert live.outstanding_ping is None
    await settle()
    ((args, kwargs),) = reg.calls_named("record_rtt")
    assert args == (live.session_id,)
    assert kwargs["last_ms"] == pytest.approx(12.5)

    # second round: EWMA blends
    await manager.send_ping(live, clock.now)
    ping2 = next(
        m for m in drain(live) if m is not None and m.WhichOneof("payload") == "ping"
    )
    clock.now += 0.0325
    await manager.handle_message(
        live,
        agent_pb2.AgentMessage(
            message_id=mid("p-2"),
            session_id=live.session_id,
            pong=agent_pb2.Pong(ping_id=ping2.ping.ping_id),
        ),
    )
    assert live.last_rtt_ms == pytest.approx(32.5)
    assert live.rtt_ewma_ms == pytest.approx(0.3 * 32.5 + 0.7 * 12.5)


@pytest.mark.asyncio
async def test_unmatched_or_expired_pong_is_ignored() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    await manager.send_ping(live, clock.now)
    first = drain(live)[0]
    assert first is not None
    await manager.send_ping(
        live, clock.now + 6.0
    )  # only the latest ping is outstanding
    drain(live)
    await manager.handle_message(
        live,
        agent_pb2.AgentMessage(
            message_id=mid("late"),
            session_id=live.session_id,
            pong=agent_pb2.Pong(ping_id=first.ping.ping_id),
        ),
    )
    assert live.last_rtt_ms is None
    assert reg.calls_named("record_rtt") == []
    assert live.outstanding_ping is not None  # the newer ping still waits


# ------------------------------------------------------------------- liveness


@pytest.mark.asyncio
async def test_apply_event_persists_and_logs_a_change() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    with structlog.testing.capture_logs() as logs:
        state = await manager.apply_event(
            live, LivenessEvent.LEASE_MISSED, silence_s=2.5
        )
    assert state is NodeState.SUSPECT and live.state is NodeState.SUSPECT
    await settle()
    assert reg.nodes[live.node_id].state is NodeState.SUSPECT
    (entry,) = [e for e in logs if e["event"] == "node.state_changed"]
    assert entry["from"] == "online" and entry["to"] == "suspect"
    assert entry["silence_s"] == 2.5


@pytest.mark.asyncio
async def test_invalid_transition_is_logged_not_raised() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg, FakeClock())
    live = await admit(manager, hello(new_node_id()))
    with structlog.testing.capture_logs() as logs:
        state = await manager.apply_event(
            live, LivenessEvent.LEASE_EXPIRED, silence_s=9
        )
    assert state is NodeState.ONLINE  # unchanged: ONLINE has no LEASE_EXPIRED arrow
    (entry,) = [e for e in logs if e["event"] == "liveness.invalid_transition"]
    assert entry["log_level"] == "error"
    assert entry["state"] == "online" and entry["liveness_event"] == "lease_expired"
    assert reg.calls_named("set_node_state") == []


@pytest.mark.asyncio
async def test_heartbeat_while_suspect_returns_to_online() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    await manager.apply_event(live, LivenessEvent.LEASE_MISSED, silence_s=2.5)
    clock.now += 3.0
    await manager.handle_message(live, heartbeat(live, "hb"))
    assert live.state is NodeState.ONLINE
    assert reg.nodes[live.node_id].state is NodeState.ONLINE


@pytest.mark.asyncio
async def test_registry_write_survives_cancellation_of_the_reader() -> None:
    """A reader cancelled mid-write (supersede) must not abandon the DB statement."""
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    old = await admit(manager, hello(node_id, epoch=1))

    gate = asyncio.Event()
    touched: list[str] = []

    async def slow_touch(nid: str) -> None:
        await gate.wait()  # "the statement is on the wire"
        touched.append(nid)

    reg.touch_node = slow_touch  # type: ignore[method-assign]
    stream = QueueStream()
    reader = manager.start_reader(old, stream)
    stream.feed(heartbeat(old, "hb-1"))
    await settle()
    assert not touched  # blocked inside the registry write

    await admit(manager, hello(node_id, epoch=2))  # supersede: cancels the reader
    await settle()
    assert reader.cancelled()
    assert not touched  # still pending, not abandoned
    gate.set()
    await settle()
    assert touched == [node_id]  # the shielded write completed on its own


@pytest.mark.asyncio
async def test_registry_failure_is_logged_and_never_kills_the_session() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    live = await admit(manager, hello(new_node_id()))
    reg.fail_with = RegistryError("db gone", details={"operation": "x"})
    with structlog.testing.capture_logs() as logs:
        await manager.handle_message(live, heartbeat(live, "hb"))
        await manager.apply_event(live, LivenessEvent.LEASE_MISSED, silence_s=2.5)
        await settle()
    assert live.heartbeats == 1 and live.state is NodeState.SUSPECT
    failed = [e for e in logs if e["log_level"] == "error"]
    assert [e["event"] for e in failed] == ["registry.write_failed"] * 2
    assert [e["op"] for e in failed] == ["touch_node", "set_node_state"]
    assert all(
        e["node_id"] == live.node_id and e["session_id"] == live.session_id
        for e in failed
    )
    assert failed[0]["error"] == "db gone" and failed[0]["retryable"] is True
    assert not live.ended
    assert live.writer.failed == 2 and not live.writer.done()  # chain continues


# ------------------------------------------------------------------- shutdown


@pytest.mark.asyncio
async def test_shutdown_closes_every_session_and_cancels_readers() -> None:
    reg = FakeRegistry()
    manager = make_manager(reg, FakeClock())
    lives = [await admit(manager, hello(new_node_id())) for _ in range(3)]
    readers = [manager.start_reader(live, QueueStream()) for live in lives]
    await settle()
    await manager.shutdown()
    assert len(manager) == 0
    for live, reader in zip(lives, readers, strict=True):
        assert live.end_reason is SessionEndReason.CONTROLLER_SHUTDOWN
        assert (
            reg.sessions[live.session_id].end_reason
            is SessionEndReason.CONTROLLER_SHUTDOWN
        )
        assert reader.done()
        assert drain(live) == [None]
    # idempotent
    await manager.end_session(lives[0], SessionEndReason.AGENT_CLOSED)
    assert lives[0].end_reason is SessionEndReason.CONTROLLER_SHUTDOWN


# ------------------------------------------------------------------ LeaseTimer


@pytest.mark.asyncio
async def test_timer_tick_returns_min_deadline_and_pings_online_sessions() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    assert await timer.tick(clock.now) is None  # no sessions
    a = await admit(manager, hello(new_node_id()))
    clock.now += 1.0
    b = await admit(manager, hello(new_node_id()))
    nxt = await timer.tick(clock.now)
    # both got their first ping; next deadline is a's SUSPECT edge
    assert [m.WhichOneof("payload") for m in drain(a) if m] == ["ping"]
    assert [m.WhichOneof("payload") for m in drain(b) if m] == ["ping"]
    assert nxt == NOW0 + CFG.suspect_after_s
    assert timer.timeout_until(nxt, clock.now) == pytest.approx(1.5)
    assert timer.timeout_until(None, clock.now) == 1.0  # max idle
    assert timer.timeout_until(clock.now - 5, clock.now) == 0.0  # overdue: now


@pytest.mark.asyncio
async def test_timer_walks_online_suspect_offline_and_ends_the_session() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    live = await admit(manager, hello(new_node_id()))
    await timer.tick(clock.now)
    drain(live)

    clock.now = NOW0 + 2.4
    assert await timer.tick(clock.now) == NOW0 + 2.5
    assert live.state is NodeState.ONLINE

    clock.now = NOW0 + 2.5
    with structlog.testing.capture_logs() as logs:
        nxt = await timer.tick(clock.now)
    assert live.state is NodeState.SUSPECT
    assert nxt == NOW0 + 6.0
    (entry,) = [e for e in logs if e["event"] == "node.state_changed"]
    assert (entry["from"], entry["to"], entry["silence_s"]) == (
        "online",
        "suspect",
        2.5,
    )

    clock.now = NOW0 + 6.0
    nxt = await timer.tick(clock.now)
    assert live.state is NodeState.OFFLINE
    assert live.ended and live.end_reason is SessionEndReason.LEASE_EXPIRED
    await settle()
    assert reg.nodes[live.node_id].state is NodeState.OFFLINE
    assert reg.sessions[live.session_id].end_reason is SessionEndReason.LEASE_EXPIRED
    assert nxt is None
    assert drain(live) == [None]


@pytest.mark.asyncio
async def test_timer_that_stalled_still_walks_every_arrow_in_one_tick() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    live = await admit(manager, hello(new_node_id()))
    clock.now = NOW0 + 60.0
    await timer.tick(clock.now)
    await settle()
    states = [args[1] for name, args, _ in reg.calls if name == "set_node_state"]
    assert states == [NodeState.SUSPECT, NodeState.OFFLINE]
    assert live.end_reason is SessionEndReason.LEASE_EXPIRED


@pytest.mark.asyncio
async def test_heartbeats_keep_online_and_pings_recur_per_lease_interval() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(manager, clock=clock)
    live = await admit(manager, hello(new_node_id()))
    pings = 0
    for i in range(1, 13):  # 12 s of 1 s heartbeats
        clock.now = NOW0 + i
        await manager.handle_message(live, heartbeat(live, f"hb-{i}"))
        await timer.tick(clock.now)
        pings += sum(
            1
            for m in drain(live)
            if m is not None and m.WhichOneof("payload") == "ping"
        )
    assert live.state is NodeState.ONLINE and not live.ended
    assert pings == 2  # t=1 (first) and t=7 (>= 1 + 6 s lease)


@pytest.mark.asyncio
async def test_timer_run_wakes_on_register_and_survives_a_tick_bug() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    timer = LeaseTimer(
        manager, clock=clock, max_idle_s=5.0
    )  # long idle: only wake helps
    task = asyncio.create_task(timer.run())
    try:
        await settle()
        assert timer.ticks == 1  # initial tick, now waiting up to 5 s
        live = await admit(manager, hello(new_node_id()))  # sets manager.wake
        await asyncio.wait_for(_until(lambda: timer.ticks >= 2), 0.05)
        assert live.pings_sent == 1  # the wake-up tick pinged the new session

        # A bug inside a tick must not kill the timer.
        boom = RuntimeError("bug")

        def exploding_snapshot() -> list[LiveSession]:
            raise boom

        manager.snapshot = exploding_snapshot  # type: ignore[method-assign]
        with structlog.testing.capture_logs() as logs:
            manager.wake.set()
            await asyncio.wait_for(_until(lambda: timer.ticks >= 3), 0.05)
            await settle()
        assert any(e["event"] == "lease_timer.tick_failed" for e in logs)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def _until(pred: Any) -> None:
    """Yield to the loop until ``pred()`` holds (callers bound it with wait_for)."""
    while not pred():  # noqa: ASYNC110 - polling a plain predicate, no event exists
        await asyncio.sleep(0)
