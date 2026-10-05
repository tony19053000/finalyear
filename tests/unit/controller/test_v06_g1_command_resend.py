"""V0.6 G1 (D113): the controller re-sends an unacked non-attempt command.

Injected clock, real SessionManager + LeaseTimer (the timer drives the re-send):

* a GcRequest / PrepareEnv not acked ``command_resend_s`` after it was sent is
  queued again with the **same envelope** (same ``message_id``) - at most
  ``command_resend_tries`` times, then logged ``session.command_unacked`` and
  left waiting (a late Ack still resolves it and fires its hook once);
* an Ack stops it; attempt commands are never re-sent by the timer;
* the timer's next deadline includes the next re-send;
* settings bounds (max accepted and max + 1 refused).
"""

from __future__ import annotations

from typing import Any

import pytest
import structlog
from pydantic import ValidationError
from test_session_linger import admit, make

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.session.commands import RESEND_ON_TIMEOUT, CommandChannel
from meshforge.controller.settings import ControllerSection

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _drain(live: Any) -> list[agent_pb2.ControllerMessage]:
    out = []
    while not live.outbound.empty():
        msg = live.outbound.get_nowait()
        if msg is not None:  # None = the end-of-stream sentinel
            out.append(msg)
    return out


def _ack(live: Any, message_id: str) -> agent_pb2.AgentMessage:
    return agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=live.session_id,
        ack=agent_pb2.Ack(acked_message_id=message_id),
    )


def _commands(sent: list[agent_pb2.ControllerMessage]) -> list[str]:
    return [m.message_id for m in sent if m.WhichOneof("payload") != "ping"]


async def test_an_unacked_gc_request_is_resent_same_envelope_bounded() -> None:
    manager, _reg, clock, _timer = make(command_resend_s=10.0, command_resend_tries=2)
    node = new_node_id()
    live = await admit(manager, node, 1)
    mid = manager.send_command(node, "gc", agent_pb2.GcRequest(run_now=True))
    assert mid is not None
    (first,) = [m for m in _drain(live) if m.message_id == mid]
    start = clock.now
    clock.now = start + 9.999
    assert manager.commands.resend_due(live, clock.now) == pytest.approx(start + 10)
    assert _commands(_drain(live)) == []
    with structlog.testing.capture_logs() as logs:
        for n in (1, 2):
            clock.now = start + 10.0 * n
            manager.commands.resend_due(live, clock.now)
            (again,) = [m for m in _drain(live) if m.WhichOneof("payload") == "gc"]
            assert again == first  # the same bytes: same message_id, same payload
        clock.now = start + 30.0
        assert manager.commands.resend_due(live, clock.now) is None  # gave up
        assert _commands(_drain(live)) == []
        clock.now = start + 1000.0
        manager.commands.resend_due(live, clock.now)
        assert _commands(_drain(live)) == []
    events = [e["event"] for e in logs]
    assert events.count("session.command_resent") == 2
    assert events.count("session.command_unacked") == 1
    assert live.commands_resent == 2 and manager.commands.unacked == 1
    # a late Ack still resolves it and fires the hook once
    acked: list[str] = []
    manager.hooks.on_command_acked = lambda _l, m, _p: acked.append(m)
    await manager.handle_message(live, _ack(live, mid))
    assert acked == [mid] and mid not in live.awaiting_ack


async def test_an_ack_stops_the_resend() -> None:
    manager, _reg, clock, _timer = make(command_resend_s=10.0)
    node = new_node_id()
    live = await admit(manager, node, 1)
    mid = manager.send_command(
        node, "prepare_env", agent_pb2.PrepareEnv(request_id="r1")
    )
    assert mid is not None
    _drain(live)
    await manager.handle_message(live, _ack(live, mid))
    clock.now += 100
    assert manager.commands.resend_due(live, clock.now) is None
    assert _commands(_drain(live)) == []


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ("start_attempt", agent_pb2.StartAttempt(attempt_id="a", fencing_token=1)),
        ("cancel_attempt", agent_pb2.CancelAttempt(attempt_id="a", fencing_token=1)),
        (
            "refresh_fetch_token",
            agent_pb2.RefreshFetchToken(attempt_id="a", fencing_token=1),
        ),
    ],
)
async def test_attempt_commands_are_never_resent_by_the_timer(
    payload: str, message: Any
) -> None:
    manager, _reg, clock, _timer = make(command_resend_s=10.0)
    node = new_node_id()
    live = await admit(manager, node, 1)
    assert manager.send_command(node, payload, message) is not None
    _drain(live)
    clock.now += 1000
    assert manager.commands.resend_due(live, clock.now) is None
    assert _commands(_drain(live)) == []
    assert payload not in RESEND_ON_TIMEOUT


async def test_the_lease_timer_drives_the_resend() -> None:
    manager, _reg, clock, timer = make(command_resend_s=1.0)
    node = new_node_id()
    live = await admit(manager, node, 1)
    mid = manager.send_command(node, "gc", agent_pb2.GcRequest(run_now=True))
    _drain(live)
    start = clock.now
    clock.now = start + 0.5
    assert await timer.tick(clock.now) == pytest.approx(start + 1.0)  # its deadline
    clock.now = start + 1.0  # before SUSPECT (2.5 s): the session is live
    await timer.tick(clock.now)
    assert not live.ended
    assert mid in [m.message_id for m in _drain(live)]
    assert live.commands_resent == 1


async def test_zero_tries_never_resends() -> None:
    manager, _reg, clock, _timer = make(command_resend_s=10.0, command_resend_tries=0)
    node = new_node_id()
    live = await admit(manager, node, 1)
    manager.send_command(node, "gc", agent_pb2.GcRequest(run_now=True))
    _drain(live)
    clock.now += 10
    with structlog.testing.capture_logs() as logs:
        manager.commands.resend_due(live, clock.now)
    assert _commands(_drain(live)) == []
    assert [e["event"] for e in logs] == ["session.command_unacked"]


async def test_resend_settings_bounds() -> None:
    assert ControllerSection().command_resend_s == 10.0
    assert ControllerSection().command_resend_tries == 5
    assert ControllerSection(command_resend_s=300).command_resend_s == 300
    assert ControllerSection(command_resend_s=1).command_resend_s == 1
    assert ControllerSection(command_resend_tries=20).command_resend_tries == 20
    for field, wrong in (
        ("command_resend_s", 300.001),
        ("command_resend_s", 0.999),
        ("command_resend_tries", 21),
        ("command_resend_tries", -1),
    ):
        with pytest.raises(ValidationError):
            ControllerSection(**{field: wrong})
    with pytest.raises(ValueError, match="resend"):
        CommandChannel(
            live={},
            close=lambda *_a: None,
            closing=lambda: False,
            queue_size=1,
            resend_s=0,
        )
