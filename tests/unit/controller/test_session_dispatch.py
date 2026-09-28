"""V0.4 G1 (D42): the session dispatch table.

``handle_message`` does the envelope work and calls the handler registered for the
payload; unregistered payloads keep the V0.3 behaviour (logged, acked, no effect);
a later version registers a handler (G4: ``attempt_status``) without touching the
manager's control flow. In-memory fenced registry from ``test_session_linger``.
"""

from __future__ import annotations

import pytest
import structlog
from test_session_linger import admit, make

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.session.dispatch import AGENT_PAYLOADS, DispatchTable
from meshforge.controller.session.live import LiveSession

pytestmark = pytest.mark.unit


def attempt_status(live: LiveSession, message_id: str) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=message_id,
        session_id=live.session_id,
        attempt_status=agent_pb2.AttemptStatus(attempt_id="att_x", fencing_token=1),
    )


def acks(live: LiveSession) -> list[str]:
    out: list[str] = []
    while not live.outbound.empty():
        msg = live.outbound.get_nowait()
        if msg is not None and msg.WhichOneof("payload") == "ack":
            out.append(msg.ack.acked_message_id)
    return out


@pytest.mark.asyncio
async def test_an_unregistered_payload_is_still_acked_and_logged() -> None:
    manager, _reg, _clock, _timer = make()
    live = await admit(manager, new_node_id(), 1)
    acks(live)  # drop nothing-yet
    message_id = new_command_id()
    with structlog.testing.capture_logs() as logs:
        await manager.handle_message(live, attempt_status(live, message_id))
    assert acks(live) == [message_id]
    (event,) = [e for e in logs if e["event"] == "session.message"]
    assert event["payload"] == "attempt_status"
    assert event["session_id"] == live.session_id
    assert not live.ended


@pytest.mark.asyncio
async def test_a_registered_handler_runs_once_and_the_message_is_still_acked() -> None:
    manager, _reg, _clock, _timer = make()
    seen: list[tuple[str, str]] = []

    async def on_status(live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        seen.append((live.session_id, msg.attempt_status.attempt_id))

    manager.dispatch.register("attempt_status", on_status)
    live = await admit(manager, new_node_id(), 1)
    message_id = new_command_id()
    await manager.handle_message(live, attempt_status(live, message_id))
    await manager.handle_message(live, attempt_status(live, message_id))  # replay
    assert seen == [(live.session_id, "att_x")]  # deduped: no second effect
    assert acks(live) == [message_id, message_id]  # acked both times
    assert live.duplicates == 1


@pytest.mark.asyncio
async def test_a_stale_session_message_reaches_no_handler() -> None:
    manager, _reg, _clock, _timer = make()
    seen: list[str] = []

    async def on_status(_live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        seen.append(msg.message_id)

    manager.dispatch.register("attempt_status", on_status)
    live = await admit(manager, new_node_id(), 1)
    msg = attempt_status(live, new_command_id())
    msg.session_id = "ses_01J00000000000000000000000"
    await manager.handle_message(live, msg)
    assert seen == []
    assert live.stale_messages == 1
    assert acks(live) == []


async def _noop(_live: LiveSession, _msg: agent_pb2.AgentMessage) -> None:
    return None


def test_register_refuses_unknown_payloads() -> None:
    table = DispatchTable(_noop)
    with pytest.raises(ValueError, match="not an AgentMessage payload"):
        table.register("not_a_payload", _noop)


def test_register_refuses_hello() -> None:
    table = DispatchTable(_noop)
    with pytest.raises(ValueError, match="admission"):
        table.register("hello", _noop)


def test_register_refuses_a_second_handler_for_one_payload() -> None:
    table = DispatchTable(_noop)
    table.register("log_chunk", _noop)
    with pytest.raises(ValueError, match="already registered"):
        table.register("log_chunk", _noop)


def test_the_payload_names_are_the_proto_oneof() -> None:
    assert {"heartbeat", "attempt_status", "log_chunk", "hello"} <= AGENT_PAYLOADS


def test_the_manager_registers_the_v03_payloads() -> None:
    manager, _reg, _clock, _timer = make()
    assert manager.dispatch.registered() == {
        "heartbeat",
        "pong",
        "ack",
        "resource_update",
        "reconcile",
        "hardware_scores",
    }
