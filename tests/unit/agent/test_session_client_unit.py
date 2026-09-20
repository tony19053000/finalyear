"""V0.3 G3 (0.3.8): Hello construction and the message-handling idempotency rules of
``AgentSessionClient`` without a network (a recording stand-in for the gRPC call).

The real stream is exercised in ``tests/component/agent``; here only the pure
handler paths are pinned: stale ``session_id`` dropped and counted, duplicate
``message_id`` acked again but counted, Pong before anything else, Ack for every
command, duplicate Ack ignored, reports buffered while offline.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from meshforge.agent.identity import load_or_create
from meshforge.agent.session.client import (
    MAX_HEARTBEAT_INTERVAL_S,
    MIN_HEARTBEAT_INTERVAL_S,
    AgentSessionClient,
    clamp_heartbeat_interval_s,
    grpc_channel_options,
)
from meshforge.agent.session.hello import (
    CAPABILITIES_V03,
    HOSTNAME_MAX_LENGTH,
    OS_MAX_LENGTH,
    PLATFORM_MAX_LENGTH,
    basic_hardware,
    build_hello,
    host_facts,
)
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSettings
from meshforge.common.ids import new_command_id, new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))


# ------------------------------------------------------------------------ hello


def test_host_facts_are_clipped_and_non_empty() -> None:
    hostname, os_text, plat = host_facts()
    assert 0 < len(hostname) <= HOSTNAME_MAX_LENGTH
    assert 0 < len(os_text) <= OS_MAX_LENGTH
    assert 0 < len(plat) <= PLATFORM_MAX_LENGTH


def test_basic_hardware_has_no_gpus_and_v03_capability() -> None:
    hw = basic_hardware()
    assert list(hw.gpus) == []
    assert list(hw.capabilities) == list(CAPABILITIES_V03)
    assert hw.cpu_threads >= hw.cpu_cores >= 0
    assert hw.ram_bytes >= 0  # 0 on a base install without psutil


def test_build_hello_fields() -> None:
    hw = agent_pb2.HardwareSummary(cpu_model="x", cpu_cores=2)
    hello = build_hello(
        node_id="node_01HZX0000000000000000000AB",
        session_epoch=2**64 - 1,
        agent_version="1.2.3",
        hardware=hw,
        facts=("h", "o", "p"),
    )
    assert hello.node_id == "node_01HZX0000000000000000000AB"
    assert hello.session_epoch == 2**64 - 1  # full uint64 range survives
    assert hello.protocol_version == common_pb2.PROTOCOL_VERSION_1
    assert hello.agent_version == "1.2.3"
    assert (hello.hostname, hello.os, hello.platform) == ("h", "o", "p")
    assert hello.hardware == hw


def test_channel_options_carry_keepalive() -> None:
    opts = dict(
        grpc_channel_options(keepalive_time_ms=10_000, keepalive_timeout_ms=5_000)
    )
    assert opts["grpc.keepalive_time_ms"] == 10_000
    assert opts["grpc.keepalive_timeout_ms"] == 5_000
    assert opts["grpc.keepalive_permit_without_calls"] == 1


# ----------------------------------------------------------------- handler rules


class RecordingCall:
    def __init__(self) -> None:
        self.written: list[agent_pb2.AgentMessage] = []

    async def write(self, msg: agent_pb2.AgentMessage) -> None:
        self.written.append(msg)


@pytest.fixture
def client(tmp_path: Path) -> AgentSessionClient:
    settings = AgentSettings.load(
        data_dir=tmp_path / "data", agent={"controller_address": "127.0.0.1:1"}
    )
    identity = load_or_create(settings.identity_dir)
    return AgentSessionClient(settings, identity)


def _online(client: AgentSessionClient) -> RecordingCall:
    call = RecordingCall()
    client._call = call
    client.session_id = new_session_id()
    client.state = AgentState.ONLINE
    return call


def _command(
    session_id: str, message_id: str | None = None
) -> agent_pb2.ControllerMessage:
    return agent_pb2.ControllerMessage(
        message_id=message_id or new_command_id(),
        session_id=session_id,
        probe_request=agent_pb2.ProbeRequest(probe_id="p1"),
    )


@pytest.mark.asyncio
async def test_ping_is_answered_with_pong_carrying_session_id(
    client: AgentSessionClient,
) -> None:
    call = _online(client)
    ping_id = new_command_id()
    await client._handle(
        agent_pb2.ControllerMessage(
            message_id=ping_id,
            session_id=client.session_id,
            ping=agent_pb2.Ping(ping_id=ping_id),
        )
    )
    assert len(call.written) == 1
    pong = call.written[0]
    assert pong.WhichOneof("payload") == "pong"
    assert pong.pong.ping_id == ping_id
    assert pong.session_id == client.session_id
    assert pong.message_id.startswith("cmd_")
    assert pong.sent_at.seconds > 0
    assert client.stats.pings_answered == 1


@pytest.mark.asyncio
async def test_command_is_acked_once_per_delivery_but_counted_as_duplicate(
    client: AgentSessionClient,
) -> None:
    call = _online(client)
    cmd = _command(client.session_id)
    await client._handle(cmd)
    await client._handle(cmd)  # redelivered after a reconnect
    acks = [m for m in call.written if m.WhichOneof("payload") == "ack"]
    assert len(acks) == 2  # the controller may have missed the first Ack
    assert {a.ack.acked_message_id for a in acks} == {cmd.message_id}
    assert client.stats.commands_acked == 2
    assert client.stats.duplicates == 1


@pytest.mark.asyncio
async def test_foreign_session_id_is_dropped_and_counted(
    client: AgentSessionClient,
) -> None:
    call = _online(client)
    await client._handle(_command(new_session_id()))
    await client._handle(_command(""))
    assert call.written == []
    assert client.stats.stale_messages == 2
    assert client.stats.commands_acked == 0


@pytest.mark.asyncio
async def test_receipts_are_not_acked_and_hello_ack_repeat_is_ignored(
    client: AgentSessionClient,
) -> None:
    call = _online(client)
    await client._handle(
        agent_pb2.ControllerMessage(
            message_id=new_command_id(),
            session_id=client.session_id,
            ack=agent_pb2.Ack(acked_message_id="cmd_nothing"),
        )
    )
    await client._handle(
        agent_pb2.ControllerMessage(
            message_id=new_command_id(), hello_ack=agent_pb2.HelloAck(accepted=True)
        )
    )
    await client._handle(agent_pb2.ControllerMessage(message_id=new_command_id()))
    assert call.written == []
    assert client.stats.commands_acked == 0


@pytest.mark.asyncio
async def test_report_buffered_offline_then_sent_online_and_acked(
    client: AgentSessionClient,
) -> None:
    # Offline: buffered only.
    mid = await client.send_report(
        "resource_update", agent_pb2.ResourceUpdate(cpu_percent=5.0)
    )
    assert len(client.ring) == 1 and client.stats.reports_sent == 0
    assert client.ring.pending()[0].message.resource_update.sequence == 1

    call = _online(client)
    mid2 = await client.send_report(
        "resource_update", agent_pb2.ResourceUpdate(cpu_percent=6.0)
    )
    assert client.stats.reports_sent == 1
    assert call.written[-1].message_id == mid2
    assert call.written[-1].session_id == client.session_id
    assert [e.sequence for e in client.ring.pending()] == [1, 2]

    for acked in (mid, mid2, mid2):  # the last one is a duplicate Ack
        await client._handle(
            agent_pb2.ControllerMessage(
                message_id=new_command_id(),
                session_id=client.session_id,
                ack=agent_pb2.Ack(acked_message_id=acked),
            )
        )
    assert len(client.ring) == 0
    assert client.stats.reports_acked == 2


@pytest.mark.asyncio
async def test_send_report_rejects_non_reports(client: AgentSessionClient) -> None:
    with pytest.raises(ValueError, match="not a report"):
        await client.send_report("heartbeat", agent_pb2.Heartbeat())


@pytest.mark.asyncio
async def test_ring_overflow_counts_drops(tmp_path: Path) -> None:
    settings = AgentSettings.load(
        data_dir=tmp_path / "d",
        agent={"controller_address": "127.0.0.1:1", "replay_ring_size": 2},
    )
    client = AgentSessionClient(settings, load_or_create(settings.identity_dir))
    for i in range(3):
        await client.send_report(
            "resource_update", agent_pb2.ResourceUpdate(cpu_percent=i)
        )
    assert client.ring.dropped == 1
    assert [e.sequence for e in client.ring.pending()] == [2, 3]


@pytest.mark.asyncio
async def test_wait_for_state_times_out_with_a_readable_message(
    client: AgentSessionClient,
) -> None:
    with pytest.raises(TimeoutError, match="DISCOVERING"):
        await client.wait_for_state(AgentState.ONLINE, timeout_s=0.02)


@pytest.mark.asyncio
async def test_run_twice_concurrently_is_refused(client: AgentSessionClient) -> None:
    client._running = True
    with pytest.raises(RuntimeError, match="already running"):
        await client.run()
    await asyncio.sleep(0)


# ------------------------------------------------------ heartbeat interval clamp
@pytest.mark.parametrize(
    ("requested_s", "expected_s"),
    [
        (2.0, 2.0),  # the production default passes through untouched
        (0.2, 0.2),  # the fast test setting passes through untouched
        (0.001, MIN_HEARTBEAT_INTERVAL_S),  # 1 ms would spin
        (1_000_000.0, MAX_HEARTBEAT_INTERVAL_S),  # 10**9 ms is ~11.6 days of silence
        (MAX_HEARTBEAT_INTERVAL_S, MAX_HEARTBEAT_INTERVAL_S),
        (MIN_HEARTBEAT_INTERVAL_S, MIN_HEARTBEAT_INTERVAL_S),
    ],
)
def test_heartbeat_interval_is_clamped_to_sane_bounds(
    requested_s: float, expected_s: float
) -> None:
    """Tester defect (G3): a hostile/buggy HelloAck must never buy days of
    silence or a busy loop; the controller owns the interval only within bounds."""
    assert clamp_heartbeat_interval_s(requested_s) == expected_s


def test_heartbeat_bounds_keep_criterion_2_reachable() -> None:
    """OFFLINE <= 8 s needs heartbeats well under 8 s; the default 2 s and the
    lease multiplier 3 fit inside the clamp with room, and the max is below any
    interval that could make a healthy node look OFFLINE for a whole minute."""
    assert MIN_HEARTBEAT_INTERVAL_S < 2.0 < MAX_HEARTBEAT_INTERVAL_S
    assert MAX_HEARTBEAT_INTERVAL_S <= 60.0


# ------------------------------------------------------- G3 review follow-ups
@pytest.mark.asyncio
async def test_reconcile_replay_from_sequence_is_zero_when_nothing_is_replayed(
    client: AgentSessionClient,
) -> None:
    """Wire contract (agent.proto Reconcile): ``replay_from_sequence`` is 0 when the
    ring is empty. The G3 implementation sent ``_report_sequence + 1`` with zero
    messages following, which a gap-detecting controller would read as a lost
    report (reviewer blocking finding)."""
    call = _online(client)
    await client._reconcile_and_replay()
    reconciles = [m for m in call.written if m.WhichOneof("payload") == "reconcile"]
    assert len(reconciles) == 1
    assert reconciles[0].reconcile.replay_from_sequence == 0
    assert len(call.written) == 1  # nothing replayed


@pytest.mark.asyncio
async def test_reconcile_replay_from_sequence_is_the_first_pending_report(
    client: AgentSessionClient,
) -> None:
    call = _online(client)
    first = await client.send_report(
        "resource_update", agent_pb2.ResourceUpdate(cpu_percent=1.0)
    )
    await client.send_report(
        "resource_update", agent_pb2.ResourceUpdate(cpu_percent=2.0)
    )
    # the controller acked the first one; only the second is pending
    await client._handle(
        agent_pb2.ControllerMessage(
            message_id=new_command_id(),
            session_id=client.session_id,
            ack=agent_pb2.Ack(acked_message_id=first),
        )
    )
    call.written.clear()
    await client._reconcile_and_replay()
    assert call.written[0].reconcile.replay_from_sequence == 2
    replayed = [
        m for m in call.written[1:] if m.WhichOneof("payload") == "resource_update"
    ]
    assert [m.resource_update.cpu_percent for m in replayed] == [2.0]


@pytest.mark.asyncio
async def test_a_ping_with_a_foreign_session_id_is_dropped_not_answered(
    client: AgentSessionClient,
) -> None:
    """ADR-0002 Updates §2: messages carrying another session's id are dropped
    and counted - Pings included (reviewer non-blocking #1)."""
    call = _online(client)
    ping_id = new_command_id()
    await client._handle(
        agent_pb2.ControllerMessage(
            message_id=ping_id,
            session_id=new_session_id(),  # not ours
            ping=agent_pb2.Ping(ping_id=ping_id),
        )
    )
    assert call.written == []
    assert client.stats.pings_answered == 0
    assert client.stats.stale_messages == 1


def test_reconnect_cap_cannot_exceed_criterion_3(tmp_path: Path) -> None:
    from meshforge.common.errors import ConfigError

    with pytest.raises(ConfigError, match="cap_s"):
        AgentSettings.load(
            data_dir=tmp_path,
            agent={"controller_address": "127.0.0.1:1", "reconnect": {"cap_s": 16.0}},
        )
