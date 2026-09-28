"""V0.4 G4 (D64/D66): commands, their Acks, deferred acks and the OFFLINE hook."""

from __future__ import annotations

from typing import Any

import pytest
from test_session_linger import CFG, admit, make, settle

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessEvent, NodeState
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.live import MAX_AWAITING_ACK

pytestmark = pytest.mark.unit


def _drain(live: Any) -> list[Any]:
    out = []
    while not live.outbound.empty():
        out.append(live.outbound.get_nowait())
    return out


def _msg(live: Any, **payload: Any) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(), session_id=live.session_id, **payload
    )


@pytest.mark.asyncio
async def test_send_command_queues_on_the_session_and_the_ack_resolves_it() -> None:
    manager, _reg, _clock, _timer = make()
    node = new_node_id()
    live = await admit(manager, node, 1)
    acked: list[tuple[str, str]] = []
    manager.hooks.on_command_acked = lambda _l, mid, p: acked.append((mid, p.payload))
    mid = manager.send_command(
        node, "cancel_attempt", agent_pb2.CancelAttempt(attempt_id="a", fencing_token=1)
    )
    assert mid is not None
    (sent,) = _drain(live)
    assert sent.message_id == mid and sent.session_id == live.session_id
    assert sent.WhichOneof("payload") == "cancel_attempt"
    assert mid in live.awaiting_ack
    await manager.handle_message(
        live, _msg(live, ack=agent_pb2.Ack(acked_message_id=mid))
    )
    assert acked == [(mid, "cancel_attempt")]
    assert live.awaiting_ack == {} and live.commands_acked == 1
    # an unknown / repeated Ack resolves nothing and fires no hook
    await manager.handle_message(
        live, _msg(live, ack=agent_pb2.Ack(acked_message_id=mid))
    )
    assert len(acked) == 1


@pytest.mark.asyncio
async def test_send_command_without_a_live_session_returns_none() -> None:
    manager, _reg, _clock, _timer = make()
    assert (
        manager.send_command(new_node_id(), "cancel_attempt", agent_pb2.CancelAttempt())
        is None
    )


@pytest.mark.asyncio
async def test_awaiting_ack_is_bounded() -> None:
    manager, _reg, _clock, _timer = make(outbound_queue_size=MAX_AWAITING_ACK + 10)
    node = new_node_id()
    live = await admit(manager, node, 1)
    for _ in range(MAX_AWAITING_ACK + 1):
        manager.send_command(node, "cancel_attempt", agent_pb2.CancelAttempt())
    assert len(live.awaiting_ack) == MAX_AWAITING_ACK


@pytest.mark.asyncio
async def test_outbound_overflow_on_a_command_ends_the_session() -> None:
    manager, _reg, _clock, _timer = make(outbound_queue_size=1)
    node = new_node_id()
    live = await admit(manager, node, 1)
    assert manager.send_command(node, "cancel_attempt", agent_pb2.CancelAttempt())
    assert (
        manager.send_command(node, "cancel_attempt", agent_pb2.CancelAttempt()) is None
    )
    assert live.ended and live.end_reason is SessionEndReason.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_a_deferred_ack_payload_is_not_acked_by_the_manager_even_as_a_duplicate() -> (
    None
):
    manager, _reg, _clock, _timer = make()
    calls: list[str] = []

    async def handler(_live: Any, msg: agent_pb2.AgentMessage) -> None:
        calls.append(msg.message_id)

    manager.dispatch.register("attempt_status", handler)
    manager.defer_ack("attempt_status")
    live = await admit(manager, new_node_id(), 1)
    msg = _msg(live, attempt_status=agent_pb2.AttemptStatus())
    await manager.handle_message(live, msg)
    await manager.handle_message(live, msg)  # duplicate: handler again, no Ack
    assert calls == [msg.message_id, msg.message_id]
    assert _drain(live) == []
    assert manager.ack_message(live, msg.message_id)
    (ack,) = _drain(live)
    assert ack.ack.acked_message_id == msg.message_id


def test_defer_ack_refuses_never_acked_payloads() -> None:
    manager, _reg, _clock, _timer = make()
    with pytest.raises(ValueError, match="never acked"):
        manager.defer_ack("goodbye")


@pytest.mark.asyncio
async def test_offline_hook_fires_for_lease_expiry_and_goodbye_only() -> None:
    manager, _reg, clock, timer = make()
    seen: list[tuple[str, LivenessEvent]] = []
    manager.hooks.on_node_offline = lambda live, ev: seen.append((live.node_id, ev))

    lost = new_node_id()
    live = await admit(manager, lost, 1)
    await manager.end_session(live, SessionEndReason.STREAM_LOST)
    assert live.state is NodeState.SUSPECT and seen == []  # SUSPECT never fires
    clock.now += CFG.offline_after_s + 1
    await timer.tick(clock.now)
    assert seen == [(lost, LivenessEvent.LEASE_EXPIRED)]

    bye = new_node_id()
    live2 = await admit(manager, bye, 1)
    await manager.handle_message(
        live2,
        _msg(
            live2, goodbye=agent_pb2.Goodbye(reason=agent_pb2.Goodbye.REASON_SHUTDOWN)
        ),
    )
    assert seen[-1] == (bye, LivenessEvent.STREAM_CLOSED)

    kept = new_node_id()
    await admit(manager, kept, 1)
    before = len(seen)
    await manager.shutdown(drain_s=0.1)
    await settle()
    assert len(seen) == before  # a controller shutdown is not the agent leaving


@pytest.mark.asyncio
async def test_a_raising_hook_never_breaks_the_session() -> None:
    manager, _reg, _clock, _timer = make()
    node = new_node_id()
    live = await admit(manager, node, 1)

    def boom(*_a: Any) -> None:
        raise RuntimeError("job layer bug")

    manager.hooks.on_command_acked = boom
    mid = manager.send_command(node, "cancel_attempt", agent_pb2.CancelAttempt())
    assert mid is not None
    await manager.handle_message(
        live, _msg(live, ack=agent_pb2.Ack(acked_message_id=mid))
    )
    assert not live.ended
