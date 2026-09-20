"""V0.3 G2 component tests: ``AgentControl.Session`` end to end (0.3.6 / 0.3.7 / 0.3.10).

Real PostgreSQL (``pg_settings``), the real :class:`ControllerRuntime` (grpc.aio +
uvicorn in this loop) on ephemeral loopback ports, and a hand-rolled agent on the
generated stub. LivenessSettings(heartbeat 0.2 s): SUSPECT at 0.25 s, OFFLINE at
0.6 s. Every persisted fact is read back through an independent
:class:`~meshforge.controller.registry.Registry` on its own engine.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from typing import Any

import grpc
import pytest
from mf_session_client import FakeAgent, make_hello, poll_until

from conftest import FAST_LIVENESS, HELLO_TIMEOUT_S
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

RejectCode = agent_pb2.HelloAck.RejectCode
SUSPECT_AT = FAST_LIVENESS.to_config().suspect_after_s  # 0.25
OFFLINE_AT = FAST_LIVENESS.to_config().offline_after_s  # 0.6


async def node_state(registry: Registry, node_id: str) -> NodeState | None:
    rec = await registry.get_node(node_id)
    return None if rec is None else rec.state


async def wait_state(
    registry: Registry, node_id: str, state: NodeState, timeout_s: float
) -> float:
    async def is_state() -> bool:
        return await node_state(registry, node_id) is state

    return await poll_until(is_state, timeout_s=timeout_s)


async def wait_closed(
    registry: Registry, session_id: str, timeout_s: float = 5.0
) -> SessionEndReason:
    async def closed() -> bool:
        rec = await registry.get_session(session_id)
        return rec is not None and rec.ended_at is not None

    await poll_until(closed, timeout_s=timeout_s)
    rec = await registry.get_session(session_id)
    assert rec is not None and rec.end_reason is not None
    return rec.end_reason


# ---------------------------------------------------------------- admission


async def test_hello_is_accepted_and_persisted(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents(epoch=3)
    ack = await agent.connect()

    assert ack.session_id.startswith("sess_")
    assert ack.heartbeat_interval_ms == 200
    assert ack.lease_ms == 600
    assert ack.controller_time.seconds > 0
    assert runtime.manager is not None and len(runtime.manager) == 1

    node = await registry.get_node(agent.node_id)
    assert node is not None
    assert node.state is NodeState.ONLINE
    assert node.hostname == "component-test-laptop"
    assert node.session_epoch == 3
    assert node.last_session_id == ack.session_id
    assert node.hardware == {"cpu_model": "test-cpu", "cpu_cores": 8}
    session = await registry.get_session(ack.session_id)
    assert session is not None and session.is_open
    assert session.node_id == agent.node_id
    assert session.session_epoch == 3
    assert session.protocol_version == common_pb2.PROTOCOL_VERSION_1
    assert session.peer_address.startswith("ipv4:127.0.0.1:")


async def test_missing_hello_is_closed_failed_precondition(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    agent.open()
    loop = asyncio.get_running_loop()
    start = loop.time()
    assert await agent.read() is None  # EOF after the Hello timeout
    elapsed = loop.time() - start
    assert agent.end_code is grpc.StatusCode.FAILED_PRECONDITION
    assert "Hello" in agent.end_details
    assert HELLO_TIMEOUT_S * 0.8 <= elapsed <= HELLO_TIMEOUT_S + 2.0
    assert await registry.list_sessions() == []


async def test_first_message_not_hello_is_failed_precondition(
    runtime: ControllerRuntime, agents: Any
) -> None:
    agent: FakeAgent = agents()
    agent.open()
    await agent.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(), heartbeat=agent_pb2.Heartbeat(sequence=1)
        )
    )
    assert await agent.read() is None
    assert agent.end_code is grpc.StatusCode.FAILED_PRECONDITION


async def test_bad_protocol_version_gets_hello_ack_rejected(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    reply = await agent.hello(protocol_version=99)
    assert reply is not None and reply.WhichOneof("payload") == "hello_ack"
    assert reply.hello_ack.accepted is False
    assert reply.hello_ack.reject_code == RejectCode.REJECT_CODE_PROTOCOL_VERSION
    assert await agent.read() is None
    assert agent.end_code is grpc.StatusCode.OK
    assert await registry.get_node(agent.node_id) is None


async def test_malformed_node_id_is_invalid_argument(
    runtime: ControllerRuntime, agents: Any
) -> None:
    agent: FakeAgent = agents(node_id="not-a-node-id")
    assert await agent.hello() is None
    assert agent.end_code is grpc.StatusCode.INVALID_ARGUMENT


async def test_stale_epoch_is_rejected_and_live_stream_untouched(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    live: FakeAgent = agents(epoch=5)
    ack = await live.connect()
    live.start_heartbeats(0.1)

    for epoch in (5, 4):
        stale: FakeAgent = agents(node_id=live.node_id, epoch=epoch)
        reply = await stale.hello()
        assert reply is not None and reply.WhichOneof("payload") == "hello_ack"
        assert reply.hello_ack.accepted is False
        assert reply.hello_ack.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH
        assert await stale.read() is None  # controller closed it cleanly
        assert stale.end_code is grpc.StatusCode.OK

    assert not live.ended.is_set()
    session = await registry.get_session(ack.session_id)
    assert session is not None and session.is_open
    assert len(await registry.list_sessions(live.node_id)) == 1


async def test_higher_epoch_supersedes_and_old_stream_ends(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    old: FakeAgent = agents(epoch=1)
    old_ack = await old.connect()
    old.start_heartbeats(0.1)

    new: FakeAgent = agents(node_id=old.node_id, epoch=2)
    new_ack = await new.connect()
    new.start_heartbeats(0.1)

    await old.wait_ended()
    # Review NB1: a non-clean end is ABORTED with the reason, so the zombie can
    # tell "replaced" from a normal close without a second round-trip.
    assert old.end_code is grpc.StatusCode.ABORTED
    assert old.end_details == SessionEndReason.SUPERSEDED.value
    # The zombie got nothing after it was superseded except the end of stream.
    assert all(m.session_id in ("", old_ack.session_id) for m in old.received)

    assert (
        await wait_closed(registry, old_ack.session_id) is SessionEndReason.SUPERSEDED
    )
    new_session = await registry.get_session(new_ack.session_id)
    assert new_session is not None and new_session.is_open
    node = await registry.get_node(old.node_id)
    assert node is not None
    assert node.state is NodeState.ONLINE
    assert node.session_epoch == 2
    assert node.last_session_id == new_ack.session_id
    assert runtime.manager is not None and len(runtime.manager) == 1


# ------------------------------------------------------------------ liveness


async def test_heartbeats_hold_online_and_are_acked(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    await agent.connect()
    agent.start_heartbeats(FAST_LIVENESS.heartbeat_interval_s)
    await asyncio.sleep(OFFLINE_AT * 2)  # well past the lease if heartbeats failed
    assert not agent.ended.is_set()
    assert await node_state(registry, agent.node_id) is NodeState.ONLINE
    acked = {m.ack.acked_message_id for m in agent.messages("ack")}
    assert agent.heartbeats_sent >= 5
    assert len(acked) >= agent.heartbeats_sent - 1  # the last one may be in flight
    live = runtime.manager.get(agent.node_id) if runtime.manager else None
    assert live is not None and live.heartbeats >= 5


async def test_silence_walks_suspect_then_offline_and_persists_both(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    ack = await agent.connect()
    loop = asyncio.get_running_loop()
    t0 = loop.time()  # no heartbeat is ever sent

    suspect_after = await wait_state(registry, agent.node_id, NodeState.SUSPECT, 2.0)
    suspect_at = loop.time() - t0
    assert SUSPECT_AT * 0.9 <= suspect_at < OFFLINE_AT, (suspect_after, suspect_at)

    await wait_state(registry, agent.node_id, NodeState.OFFLINE, 2.0)
    offline_at = loop.time() - t0
    assert OFFLINE_AT * 0.9 <= offline_at <= OFFLINE_AT + 1.0, offline_at

    assert await wait_closed(registry, ack.session_id) is SessionEndReason.LEASE_EXPIRED
    await agent.wait_ended()
    assert agent.end_code is grpc.StatusCode.ABORTED  # review NB1: not a clean end
    assert agent.end_details == SessionEndReason.LEASE_EXPIRED.value
    assert runtime.manager is not None and len(runtime.manager) == 0


async def test_reconnect_after_offline_is_online_again(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    first: FakeAgent = agents(epoch=1)
    await first.connect()
    await wait_state(registry, first.node_id, NodeState.OFFLINE, 3.0)
    await first.wait_ended()

    second: FakeAgent = agents(node_id=first.node_id, epoch=2)
    ack = await second.connect()
    second.start_heartbeats(FAST_LIVENESS.heartbeat_interval_s)
    node = await registry.get_node(first.node_id)
    assert node is not None and node.state is NodeState.ONLINE
    assert node.last_session_id == ack.session_id
    sessions = await registry.list_sessions(first.node_id)
    assert [s.end_reason for s in sessions] == [SessionEndReason.LEASE_EXPIRED, None]


async def test_heartbeat_while_suspect_returns_to_online(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    await agent.connect()
    await wait_state(registry, agent.node_id, NodeState.SUSPECT, 2.0)
    await agent.heartbeat()
    await wait_state(registry, agent.node_id, NodeState.ONLINE, 2.0)
    assert not agent.ended.is_set()


async def test_stale_session_id_is_dropped_and_counted(
    runtime: ControllerRuntime, agents: Any
) -> None:
    agent: FakeAgent = agents()
    await agent.connect()
    stale_id = await agent.heartbeat(session_id="sess_00000000000000000000000000")
    real_id = await agent.heartbeat()  # a real one so we can order the acks
    assert runtime.manager is not None
    live = runtime.manager.get(agent.node_id)
    assert live is not None

    async def one_real_heartbeat() -> bool:
        return live.heartbeats == 1

    await poll_until(one_real_heartbeat, timeout_s=2.0)
    assert live.stale_messages == 1
    assert runtime.manager.stale_messages_total == 1
    acked = {m.ack.acked_message_id for m in agent.messages("ack")}
    assert real_id in acked
    assert stale_id not in acked  # dropped, never processed, never acked


async def test_second_hello_on_open_stream_is_protocol_error(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    ack = await agent.connect()
    await agent.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=ack.session_id,
            hello=make_hello(agent.node_id, 1),
        )
    )
    await agent.wait_ended()
    assert (
        await wait_closed(registry, ack.session_id) is SessionEndReason.PROTOCOL_ERROR
    )
    assert agent.end_code is grpc.StatusCode.ABORTED  # review NB1
    assert agent.end_details == SessionEndReason.PROTOCOL_ERROR.value


async def test_agent_disconnect_closes_session_as_agent_closed(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    ack = await agent.connect()
    await agent.close()  # half-close + cancel: the transport tears the stream down
    assert await wait_closed(registry, ack.session_id) is SessionEndReason.AGENT_CLOSED

    async def unregistered() -> bool:
        return runtime.manager is not None and len(runtime.manager) == 0

    await poll_until(unregistered, timeout_s=2.0)


# ------------------------------------------------------------------------ RTT


async def test_ping_pong_records_rtt_and_ewma(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    ack = await agent.connect()
    agent.start_heartbeats(FAST_LIVENESS.heartbeat_interval_s)

    async def two_pongs() -> bool:
        return agent.pongs_sent >= 2

    # The first ping goes out on the timer tick after admission, the second one a
    # lease interval (0.6 s) later.
    await poll_until(two_pongs, timeout_s=OFFLINE_AT * 3)

    async def rtt_persisted() -> bool:
        rec = await registry.get_session(ack.session_id)
        return rec is not None and rec.last_rtt_ms is not None

    await poll_until(rtt_persisted, timeout_s=2.0)
    rec = await registry.get_session(ack.session_id)
    assert rec is not None and rec.last_rtt_ms is not None and rec.rtt_ewma_ms
    assert 0.0 < rec.last_rtt_ms < 1000.0  # loopback; measured, not skew
    assert 0.0 < rec.rtt_ewma_ms < 1000.0
    live = runtime.manager.get(agent.node_id) if runtime.manager else None
    assert live is not None and live.pongs_matched >= 2
    assert live.last_rtt_ms is not None and live.rtt_ewma_ms is not None


async def test_unanswered_pings_do_not_kill_a_heartbeating_session(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents(auto_pong=False)
    ack = await agent.connect()
    agent.start_heartbeats(FAST_LIVENESS.heartbeat_interval_s)
    await asyncio.sleep(OFFLINE_AT * 2)
    assert len(agent.messages("ping")) >= 2
    assert not agent.ended.is_set()
    assert await node_state(registry, agent.node_id) is NodeState.ONLINE
    rec = await registry.get_session(ack.session_id)
    assert rec is not None and rec.last_rtt_ms is None


# ------------------------------------------------------------- one process


async def test_healthz_over_real_uvicorn(
    runtime: ControllerRuntime, agents: Any
) -> None:
    url = f"http://127.0.0.1:{runtime.http_port}/healthz"

    def get() -> dict[str, Any]:
        with urllib.request.urlopen(url, timeout=5) as resp:
            assert resp.status == 200
            return dict(json.loads(resp.read()))

    assert await asyncio.to_thread(get) == {"status": "ok", "sessions": 0}
    agent: FakeAgent = agents()
    await agent.connect()
    assert await asyncio.to_thread(get) == {"status": "ok", "sessions": 1}


async def test_graceful_stop_closes_sessions_with_controller_shutdown(
    settings: Any, agents_factory: Any, registry: Registry
) -> None:
    async with ControllerRuntime.start(settings) as rt:
        target = f"127.0.0.1:{rt.grpc_port}"
        async with grpc.aio.insecure_channel(target) as ch:
            a: FakeAgent = agents_factory(ch)
            b: FakeAgent = agents_factory(ch)
            acks = [await a.connect(), await b.connect()]
            a.start_heartbeats(0.1)
            b.start_heartbeats(0.1)
            await rt.stop()
            await a.wait_ended()
            await b.wait_ended()
            assert a.end_code is grpc.StatusCode.OK
            for ack in acks:
                rec = await registry.get_session(ack.session_id)
                assert rec is not None
                assert rec.end_reason is SessionEndReason.CONTROLLER_SHUTDOWN
            await a.close()
            await b.close()
    # Idempotent: the context manager's second stop() is a no-op.


async def test_stop_refuses_new_hellos(settings: Any, registry: Registry) -> None:
    async with ControllerRuntime.start(settings) as rt:
        target = f"127.0.0.1:{rt.grpc_port}"
        assert rt.manager is not None
        rt.manager.closing = True  # what stop() does first
        async with grpc.aio.insecure_channel(target) as ch:
            agent = FakeAgent(ch)
            assert await agent.hello() is None
            assert agent.end_code is grpc.StatusCode.UNAVAILABLE
            await agent.close()


@pytest.fixture
def agents_factory() -> Any:
    def factory(channel: grpc.aio.Channel, **kwargs: Any) -> FakeAgent:
        return FakeAgent(channel, **kwargs)

    return factory
