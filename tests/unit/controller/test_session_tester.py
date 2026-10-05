"""Independent adversarial unit tests for V0.3 G2 (tester round).

Targets ADR-0002 Updates §1-5, ARCHITECTURE §12.3 (amended 2026-09-20), and the G2
brief (docs/briefs/V0.3-fabric-core.md §5b). Written from the success criteria and
the attack list handed to the tester, not from the implementer's own tests
(tests/unit/controller/test_session_manager.py) - read afterwards only to avoid
duplicating exact scenarios. No database, no sockets: registry is a small
duck-typed fake, the clock is a float the test moves by hand, the only real
asyncio is the manager's lock/queue/tasks. No ``sleep`` longer than 50 ms.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
from collections.abc import Mapping
from typing import Any, cast

import pytest
from pydantic import ValidationError

from meshforge.common.errors import ConfigError
from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.netaddr import parse_bind
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import (
    InvalidTransitionError,
    LeaseTracker,
    LivenessConfig,
    LivenessEvent,
    NodeState,
    next_state,
)
from meshforge.controller.registry import NodeRecord, Registry, SessionRecord
from meshforge.controller.session.errors import OutboundOverflowError
from meshforge.controller.session.leases import LeaseTimer
from meshforge.controller.session.manager import (
    Accepted,
    LiveSession,
    Rejected,
    SessionManager,
)
from meshforge.controller.settings import ControllerSection
from meshforge.controller.store.models.node import SessionEndReason

pytestmark = pytest.mark.unit

RejectCode = agent_pb2.HelloAck.RejectCode
CFG = LivenessConfig()  # 2 s / +0.5 s / x3
NOW0 = 1000.0
UINT64_MAX = 2**64 - 1


class FakeClock:
    def __init__(self, start: float = NOW0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class FakeRegistry:
    """Minimal duck-typed stand-in for :class:`Registry`."""

    def __init__(self) -> None:
        self.nodes: dict[str, NodeRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.calls: list[str] = []

    async def get_node(self, node_id: str) -> NodeRecord | None:
        self.calls.append("get_node")
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
        hardware_scores: Mapping[str, Any] | None = None,
        cpu_slots: int | None = None,
        gpu_slots: int | None = None,
    ) -> NodeRecord:
        self.calls.append("upsert_node_on_hello")
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
            hardware_scores=hardware_scores,
            cpu_slots=cpu_slots,
            gpu_slots=gpu_slots,
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
        self.calls.append("open_session")
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
        self.calls.append("close_session")
        rec = self.sessions[session_id]
        if rec.ended_at is not None:
            return False
        self.sessions[session_id] = dataclasses.replace(
            rec, ended_at=dt.datetime.now(dt.UTC), end_reason=end_reason
        )
        return True

    async def set_node_state(
        self, node_id: str, state: NodeState, *, session_id: str
    ) -> bool:
        self.calls.append("set_node_state")
        rec = self.nodes[node_id]
        if rec.last_session_id != session_id:  # fenced like the real one (D20)
            return False
        self.nodes[node_id] = dataclasses.replace(rec, state=state)
        return True

    async def touch_node(self, node_id: str) -> None:
        self.calls.append("touch_node")

    async def record_rtt(
        self, session_id: str, *, last_ms: float, ewma_ms: float
    ) -> None:
        self.calls.append("record_rtt")


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


async def settle() -> None:
    for _ in range(3):
        await asyncio.sleep(0)


# --------------------------------------------------------------------------
# 1. Epoch replay across a disconnect (no *live* session to fence against)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_epoch_is_not_replayable_after_the_session_ends() -> None:
    """DEFECT PIN: ADR-0002 Updates §1 says "a Hello with a lower or equal epoch
    is rejected... a stale process or a replay" - it does not carve out "only
    while a session is live". Once the live session for a node ends (crash,
    lease expiry, agent_closed), ``_admit_locked`` in
    src/meshforge/controller/session/manager.py only compares a new Hello's
    epoch against ``self._live.get(node_id)`` -- which is now gone -- and never
    against the persisted ``NodeRecord.session_epoch`` (the highest epoch ever
    accepted, written by ``upsert_node_on_hello``). A stale/replayed process
    that already lost the fencing race can simply wait for the winner's stream
    to end (or crash it) and then be admitted with its old, already-superseded
    epoch.

    Reproduction: epoch 5 connects and disconnects (agent_closed); epoch 3
    (lower) then dials in with no live competitor and is currently ACCEPTED.

    Expected: epoch 3 must still be rejected STALE_EPOCH, because the registry
    remembers 5 was the highest epoch ever seen for this node_id.
    Observed: it is accepted, a new session_id is issued, and the DB
    ``node.session_epoch`` column is overwritten with 3 - a regression of the
    fencing guarantee across reconnects.
    """
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()

    winner = await admit(manager, hello(node_id, epoch=5))
    await manager.end_session(winner, SessionEndReason.AGENT_CLOSED)
    await settle()
    assert manager.get(node_id) is None  # no live session left to fence against

    result = await manager.admit(hello(node_id, epoch=3), "ipv4:127.0.0.1:2")
    assert isinstance(result, Rejected), (
        "epoch 3 was accepted after epoch 5's session ended - a replayed/stale "
        "epoch must stay fenced even with no live competitor "
        f"(got {result!r})"
    )
    assert result.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH


# --------------------------------------------------------------------------
# 2. uint64 boundary epoch
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_uint64_epoch_is_accepted_and_then_fences_correctly() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()

    live = await admit(manager, hello(node_id, epoch=UINT64_MAX))
    assert live.session_epoch == UINT64_MAX

    # Same (max) epoch again: must be rejected, not wrap around / overflow.
    result = await manager.admit(hello(node_id, epoch=UINT64_MAX), "ipv4:127.0.0.1:2")
    assert isinstance(result, Rejected)
    assert result.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH
    assert manager.get(node_id) is live


# --------------------------------------------------------------------------
# 3. Bounded outbound queue: overflow is a typed close, never a silent drop
#    or an infinite block.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outbound_overflow_raises_typed_error_never_blocks() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock, outbound_queue_size=4)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id))

    # Queue already holds the ack-less HelloAck sentinel path is empty here;
    # fill to capacity with explicit sends.
    for _ in range(4):
        manager.send(live, agent_pb2.ControllerMessage())
    with pytest.raises(OutboundOverflowError):
        manager.send(live, agent_pb2.ControllerMessage())
    # The call returned immediately (no blocking write) and the session is
    # still queryable / not silently corrupted.
    assert manager.get(node_id) is live


# --------------------------------------------------------------------------
# 4. Heartbeat sequence regressions and huge jumps must never affect liveness
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_sequence_going_backwards_still_extends_the_lease() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id))

    clock.advance(1.0)
    seqs = [10, 3, 2**63, 0, 1]
    for seq in seqs:
        msg = agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=live.session_id,
            heartbeat=agent_pb2.Heartbeat(sequence=seq),
        )
        await manager.handle_message(live, msg)
        clock.advance(0.01)
    assert not live.ended
    assert live.state is NodeState.ONLINE
    assert live.heartbeats == len(seqs)


@pytest.mark.asyncio
async def test_agent_time_far_in_past_or_future_does_not_affect_liveness() -> None:
    """``Heartbeat.agent_time`` is display/skew only (ADR-0002 Updates §5); the
    session layer at G2 does not even read it for liveness - confirm a
    wildly-skewed wall clock in the field has zero effect on the tracker."""
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id))

    for ts in (
        dt.datetime(1970, 1, 1, tzinfo=dt.UTC),
        dt.datetime(2099, 1, 1, tzinfo=dt.UTC),
    ):
        hb = agent_pb2.Heartbeat(sequence=1)
        hb.agent_time.FromDatetime(ts)
        msg = agent_pb2.AgentMessage(
            message_id=new_command_id(), session_id=live.session_id, heartbeat=hb
        )
        await manager.handle_message(live, msg)
    assert live.state is NodeState.ONLINE
    assert live.tracker.silence(clock.now) == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------
# 5. Pong abuse: unmatched / replayed / before any ping
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pong_before_any_ping_is_ignored_not_a_crash() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id))
    assert live.outstanding_ping is None

    msg = agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        pong=agent_pb2.Pong(ping_id="never-sent"),
    )
    await manager.handle_message(live, msg)  # must not raise
    assert live.last_rtt_ms is None
    assert live.pongs_matched == 0
    assert not live.ended


@pytest.mark.asyncio
async def test_replayed_pong_for_an_already_matched_ping_is_ignored() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id))

    await manager.send_ping(live, clock.now)
    ping_id = live.outstanding_ping[0]  # type: ignore[index]
    clock.advance(0.01)
    pong = agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        pong=agent_pb2.Pong(ping_id=ping_id),
    )
    await manager.handle_message(live, pong)
    first_rtt = live.last_rtt_ms
    assert first_rtt is not None

    # Replay the exact same Pong again (duplicate delivery / malicious replay).
    clock.advance(5.0)
    pong2 = agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        pong=agent_pb2.Pong(ping_id=ping_id),
    )
    await manager.handle_message(live, pong2)
    assert live.last_rtt_ms == first_rtt  # not overwritten by the stale replay
    assert live.pongs_matched == 1


# --------------------------------------------------------------------------
# 6. Ack for a message_id the controller never sent
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_for_unknown_message_id_does_not_crash_the_session() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id))

    msg = agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        ack=agent_pb2.Ack(acked_message_id="cmd-that-was-never-sent"),
    )
    await manager.handle_message(live, msg)  # must not raise
    assert not live.ended
    assert live.state is NodeState.ONLINE


# --------------------------------------------------------------------------
# 7. 10 000 duplicate message_ids: dedupe holds, no unbounded memory, each
#    still gets exactly one Ack.
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ten_thousand_duplicate_message_ids_are_all_deduped() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock, outbound_queue_size=50_000)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id))

    dispatched_before = len(reg.calls)
    same_id_forever = new_command_id()
    for _ in range(10_000):
        msg = agent_pb2.AgentMessage(
            message_id=same_id_forever,
            session_id=live.session_id,
            heartbeat=agent_pb2.Heartbeat(sequence=1),
        )
        await manager.handle_message(live, msg)
    await settle()  # the one write rides the session's chain
    # Only the *first* occurrence dispatched (one touch_node registry call);
    # the other 9 999 were recognised as duplicates.
    assert reg.calls[dispatched_before:].count("touch_node") == 1
    assert live.duplicates == 9_999
    assert live.heartbeats == 1


# --------------------------------------------------------------------------
# 8. LEASE_MISSED at exactly suspect_after_s never flaps back and forth; a
#    heartbeat stream just under the threshold never trips SUSPECT.
# --------------------------------------------------------------------------


def test_lease_tracker_heartbeat_stream_just_under_threshold_never_flaps() -> None:
    cfg = LivenessConfig(
        heartbeat_interval_s=0.2, miss_allowance_s=0.05, lease_multiplier=3
    )
    clock = FakeClock(0.0)
    tracker = LeaseTracker(cfg, clock)
    # heartbeat exactly every 0.24 s, i.e. strictly under suspect_after_s=0.25 s
    for _ in range(200):
        clock.advance(0.24)
        assert tracker.check(clock.now) is None, (
            f"false SUSPECT/OFFLINE at silence={tracker.silence(clock.now)}"
        )
        tracker.observe_heartbeat(clock.now)


def test_lease_tracker_one_gap_then_heartbeat_never_produces_offline_later() -> None:
    cfg = LivenessConfig(
        heartbeat_interval_s=0.2, miss_allowance_s=0.05, lease_multiplier=3
    )
    clock = FakeClock(0.0)
    tracker = LeaseTracker(cfg, clock)
    clock.advance(0.3)  # > suspect_after_s (0.25), < offline_after_s (0.6)
    assert tracker.check(clock.now) is LivenessEvent.LEASE_MISSED
    tracker.observe_heartbeat(clock.now)  # agent recovers
    # No matter how much time now passes without another check() call in
    # between, the tracker must not silently remember the old expired-ness.
    for _ in range(20):
        clock.advance(0.24)
        assert tracker.check(clock.now) is None
        tracker.observe_heartbeat(clock.now)


# --------------------------------------------------------------------------
# 9. InvalidTransitionError must never kill the lease timer loop
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lease_timer_keeps_running_after_an_invalid_transition() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()
    live = await admit(manager, hello(node_id))

    # Force the session into a state that has no LEASE_MISSED arrow (REVOKED),
    # bypassing the public API to provoke next_state's KeyError path exactly
    # like a bug in the caller would.
    live.state = NodeState.REVOKED
    with pytest.raises(InvalidTransitionError):
        next_state(NodeState.REVOKED, LivenessEvent.LEASE_MISSED)

    timer = LeaseTimer(manager, clock=clock)
    clock.advance(10.0)  # well past both lease thresholds
    # apply_event must swallow InvalidTransitionError (logged, not raised) so
    # one bad session cannot wedge the single shared timer task.
    next_deadline = await timer.tick(clock.now)
    assert live.state is NodeState.REVOKED  # left unchanged, not crashed
    assert timer.ticks == 1
    # the timer computed *something* (did not raise) for the remaining state
    assert next_deadline is None or isinstance(next_deadline, float)


# --------------------------------------------------------------------------
# 10. asyncio task hygiene: shutdown leaves no session-owned tasks behind
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_shutdown_leaves_no_leaked_tasks_across_many_sessions() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)

    baseline = {t for t in asyncio.all_tasks() if not t.done()}

    class _EmptyStream:
        def __aiter__(self) -> _EmptyStream:
            return self

        async def __anext__(self) -> agent_pb2.AgentMessage:
            await asyncio.sleep(3600)  # never yields; cancelled by shutdown
            raise StopAsyncIteration

    lives = []
    for _ in range(20):
        live = await admit(manager, hello(new_node_id()))
        manager.start_reader(live, _EmptyStream())
        lives.append(live)

    await manager.shutdown()
    for live in lives:
        assert live.ended
        assert live.end_reason is SessionEndReason.CONTROLLER_SHUTDOWN

    await settle()
    leaked = {t for t in asyncio.all_tasks() if not t.done()} - baseline
    assert not leaked, f"tasks leaked after shutdown: {[t.get_name() for t in leaked]}"


# --------------------------------------------------------------------------
# 11. Public/non-LAN bind is refused before any socket work (settings layer)
# --------------------------------------------------------------------------


def test_public_bind_refused_without_allow_any_bind() -> None:
    # ControllerSection wraps the ConfigError raised by check_bind_address inside
    # a pydantic model_validator, so it surfaces as ValidationError at this layer;
    # the underlying cause is still ConfigError.
    with pytest.raises(ValidationError) as excinfo:
        ControllerSection(grpc_bind="8.8.8.8:47100")
    assert isinstance(excinfo.value.errors()[0]["ctx"]["error"], ConfigError)


def test_public_bind_allowed_with_allow_any_bind() -> None:
    section = ControllerSection(grpc_bind="8.8.8.8:47100", allow_any_bind=True)
    bind = parse_bind(section.grpc_bind, allow_any=section.allow_any_bind)
    assert bind.host == "8.8.8.8"


def test_wildcard_bind_refused_without_allow_any_bind() -> None:
    with pytest.raises(ValidationError):
        ControllerSection(grpc_bind="0.0.0.0:47100")


# --------------------------------------------------------------------------
# 12. Concurrent racing Hellos for one node_id must leave exactly one winner
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_racing_epochs_leave_exactly_one_live_session() -> None:
    reg, clock = FakeRegistry(), FakeClock()
    manager = make_manager(reg, clock)
    node_id = new_node_id()

    results = await asyncio.gather(
        *(
            manager.admit(hello(node_id, epoch=epoch), f"ipv4:127.0.0.1:{epoch}")
            for epoch in (1, 2, 3)
        )
    )
    accepted = [r for r in results if isinstance(r, Accepted)]
    rejected = [r for r in results if isinstance(r, Rejected)]
    # Exactly one Hello ends up as the live session (whichever epoch was
    # admitted last under the lock); the others are either rejected outright
    # or accepted-then-immediately-superseded. Never more than one live entry.
    assert len(manager._live) == 1
    live = next(iter(manager._live.values()))
    assert live.session_epoch == 3
    # every non-live accepted session must be recorded as superseded
    for r in accepted:
        if r.live is not live:
            assert r.live.end_reason is SessionEndReason.SUPERSEDED
    assert len(accepted) + len(rejected) == 3
