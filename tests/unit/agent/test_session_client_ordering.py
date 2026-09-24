"""V0.3 G6 (D21): "Reconcile first" is structural, not a missing ``await``.

Unit level: the per-stream gate on :class:`AgentSessionClient` - a report on an
ONLINE stream waits for the gate, writes once it opens, skips a report the replay
already carried, and **returns** (without a direct write) when the stream dies
while it waits. No network: the call is an in-memory recorder, the clock is
never read, and nothing sleeps.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from meshforge.agent.identity import load_or_create
from meshforge.agent.session.client import AgentSessionClient
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSettings
from meshforge.common.ids import new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2

pytestmark = pytest.mark.unit


class RecordingCall:
    def __init__(self) -> None:
        self.written: list[agent_pb2.AgentMessage] = []

    async def write(self, msg: agent_pb2.AgentMessage) -> None:
        self.written.append(msg)

    def payloads(self) -> list[str]:
        return [m.WhichOneof("payload") or "?" for m in self.written]


@pytest.fixture
def client(tmp_path: Path) -> AgentSessionClient:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data", agent={"controller_address": "127.0.0.1:1"}
    )
    return AgentSessionClient(settings, load_or_create(settings.identity_dir))


def online_not_ready(client: AgentSessionClient) -> RecordingCall:
    """An accepted stream in the window between ONLINE and the Reconcile."""
    call = RecordingCall()
    client._call = call
    client.session_id = new_session_id()
    client._open_gate()
    client.state = AgentState.ONLINE
    return call


def report(cpu: float) -> agent_pb2.ResourceUpdate:
    return agent_pb2.ResourceUpdate(cpu_percent=cpu)


async def settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_a_report_in_the_window_waits_and_reconcile_goes_first(
    client: AgentSessionClient,
) -> None:
    await client.send_report("resource_update", report(1.0))  # offline: ring only
    call = online_not_ready(client)
    pending = asyncio.create_task(client.send_report("resource_update", report(2.0)))
    await settle()
    assert not pending.done() and call.written == []  # held by the gate

    gate = client._gate
    assert gate is not None
    gate.replayed_through = await client._reconcile_and_replay()
    gate.ready.set()
    await asyncio.wait_for(pending, 0.05)
    assert call.payloads()[0] == "reconcile"
    # Both reports went out once, in the replay; the waiting one is not sent twice.
    sent = [m.resource_update.cpu_percent for m in call.written[1:]]
    assert sent == [1.0, 2.0]
    assert client.stats.reports_sent == 0

    await client.send_report("resource_update", report(3.0))  # after: direct
    assert call.written[-1].resource_update.cpu_percent == 3.0
    assert client.stats.reports_sent == 1


@pytest.mark.asyncio
async def test_a_report_waiting_across_a_stream_death_returns_without_writing(
    client: AgentSessionClient,
) -> None:
    """No hang: the monitor must never block forever on a dead stream."""
    call = online_not_ready(client)
    gate = client._gate
    assert gate is not None
    waiting = asyncio.create_task(client.send_report("resource_update", report(9.0)))
    await settle()
    assert not waiting.done()

    client._close_gate(gate)  # what _online's finally does when the stream ends
    message_id = await asyncio.wait_for(waiting, 0.05)
    assert call.written == []
    assert client.stats.reports_sent == 0
    assert [e.message.message_id for e in client.ring.pending()] == [message_id]
    assert client._gate is None


@pytest.mark.asyncio
async def test_a_newer_stream_does_not_let_an_old_waiter_write(
    client: AgentSessionClient,
) -> None:
    call = online_not_ready(client)
    old_gate = client._gate
    assert old_gate is not None
    waiting = asyncio.create_task(client.send_report("resource_update", report(4.0)))
    await settle()
    client._close_gate(old_gate)
    client._open_gate().ready.set()  # the next stream is already open and ready
    await asyncio.wait_for(waiting, 0.05)
    assert call.written == []  # the old generation's waiter skipped


@pytest.mark.asyncio
async def test_not_online_means_ring_only_and_no_wait(
    client: AgentSessionClient,
) -> None:
    client._open_gate()  # a gate exists but the state is not ONLINE
    await asyncio.wait_for(client.send_report("resource_update", report(5.0)), 0.05)
    assert len(client.ring) == 1 and client.stats.reports_sent == 0


@pytest.mark.asyncio
async def test_generation_moves_on_open_and_close(client: AgentSessionClient) -> None:
    start = client._generation
    gate = client._open_gate()
    assert gate.generation == client._generation == start + 1
    client._close_gate(gate)
    assert client._generation == start + 2 and gate.ready.is_set()
    client._close_gate(gate)  # idempotent
    assert client._generation == start + 2


@pytest.mark.asyncio
async def test_the_heartbeat_loop_waits_for_the_gate_too(
    client: AgentSessionClient,
) -> None:
    call = online_not_ready(client)
    gate = client._gate
    assert gate is not None
    client.heartbeat_interval_s = 0.001
    beats = asyncio.create_task(client._heartbeat_loop(call, gate))
    await asyncio.sleep(0.02)
    assert call.written == []  # no heartbeat before the Reconcile
    client._close_gate(gate)
    await asyncio.wait_for(beats, 0.05)  # a closed gate ends the loop
    assert call.written == []
