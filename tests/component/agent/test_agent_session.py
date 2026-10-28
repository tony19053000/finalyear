"""V0.3 G3 component tests: the real agent client against the real controller runtime.

Every test runs a ``ControllerRuntime`` (gRPC + Postgres) in this event loop and an
``AgentSessionClient`` as a task in the same loop; messages cross a real loopback
socket. Nothing is mocked; failures are injected by stopping the runtime, connecting
a competing agent, or handing the agent a Hello the controller must refuse.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import grpc
import pytest
from mf_agent_harness import (
    FAST_LIVENESS,
    AgentFactory,
    make_controller_settings,
)
from mf_controller_harness import tolerant_liveness
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.identity import NodeIdentity, load_or_create
from meshforge.agent.session.errors import AgentContractError, SupersededError
from meshforge.agent.session.state import AgentState
from meshforge.common.errors import ExitCode, FailureType
from meshforge.common.ids import new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker]

ONLINE_TIMEOUT_S = 10.0
LEASE_S = FAST_LIVENESS.heartbeat_interval_s * FAST_LIVENESS.lease_multiplier


async def _node_state(registry: Registry, node_id: str) -> NodeState | None:
    node = await registry.get_node(node_id)
    return None if node is None else node.state


# ------------------------------------------------------------------ connect


@pytest.mark.asyncio
async def test_connect_puts_the_node_online_in_the_registry(
    runtime: ControllerRuntime,
    registry: Registry,
    agents: AgentFactory,
    grpc_target: str,
    identity: NodeIdentity,
) -> None:
    running = await agents(grpc_target, identity=identity)
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)

    assert running.client.session_id.startswith("sess_")
    assert running.client.heartbeat_interval_s == FAST_LIVENESS.heartbeat_interval_s
    assert running.client.lease_s == pytest.approx(LEASE_S)

    node = await registry.get_node(str(identity.node_id))
    assert node is not None
    assert node.state is NodeState.ONLINE
    assert node.last_session_id == running.client.session_id
    assert node.session_epoch == running.client.identity.session_epoch == 1
    assert node.hardware["capabilities"] == ["v0.3"]
    assert node.hardware.get("gpus", []) == []
    assert 0 < len(node.hostname) <= 253

    live = runtime.manager.get(str(identity.node_id)) if runtime.manager else None
    assert live is not None and live.session_id == running.client.session_id


@pytest.mark.asyncio
async def test_heartbeats_hold_online_for_three_lease_intervals(
    runtime: ControllerRuntime,
    registry: Registry,
    agents: AgentFactory,
    grpc_target: str,
    identity: NodeIdentity,
) -> None:
    running = await agents(grpc_target, identity=identity)
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    node_id = str(identity.node_id)
    assert runtime.manager is not None

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 3 * LEASE_S
    while loop.time() < deadline:
        live = runtime.manager.get(node_id)
        assert live is not None and live.state is NodeState.ONLINE
        assert running.client.state is AgentState.ONLINE
        await asyncio.sleep(0.05)

    hb_expected = (3 * LEASE_S) / FAST_LIVENESS.heartbeat_interval_s
    assert running.client.stats.heartbeats_sent >= hb_expected * 0.6
    assert await _node_state(registry, node_id) is NodeState.ONLINE
    assert running.client.stats.stream_ends == []  # never dropped


@pytest.mark.asyncio
async def test_pings_are_answered_and_rtt_recorded(
    runtime: ControllerRuntime,
    registry: Registry,
    agents: AgentFactory,
    grpc_target: str,
    identity: NodeIdentity,
) -> None:
    running = await agents(grpc_target, identity=identity)
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)

    async def rtt_recorded() -> bool:
        rec = await registry.get_session(running.client.session_id)
        return rec is not None and rec.last_rtt_ms is not None

    await poll_until(rtt_recorded, timeout_s=3 * LEASE_S + 2.0)
    rec = await registry.get_session(running.client.session_id)
    assert rec is not None and rec.last_rtt_ms is not None
    assert 0.0 <= rec.last_rtt_ms < 1000.0  # loopback
    assert rec.rtt_ewma_ms is not None
    assert running.client.stats.pings_answered >= 1
    assert runtime.manager is not None
    live = runtime.manager.get(str(identity.node_id))
    assert live is not None and live.pongs_matched >= 1


# ---------------------------------------------------------------- reconnect


@pytest.mark.asyncio
async def test_controller_restart_on_same_port_reconnects_within_15s(
    pg_settings: DatabaseSettings,
    registry: Registry,
    agents: AgentFactory,
    identity: NodeIdentity,
) -> None:
    """V0.3 criterion 3 (in-process): stop the runtime, restart it on the same
    port with the same database; the agent must be ONLINE again in < 15 s."""
    first = ControllerRuntime(make_controller_settings(pg_settings))
    await first._start()
    port = first.grpc_port
    target = f"127.0.0.1:{port}"
    running = await agents(target, identity=identity)
    try:
        await running.client.wait_for_state(
            AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
        )
        first_session = running.client.session_id
        await first.stop()

        # The stream ended OK (controller_shutdown) -> BACKOFF, not a fatal error.
        await running.client.wait_for_state(
            AgentState.BACKOFF,
            AgentState.DISCOVERING,
            AgentState.CONNECTING,
            timeout_s=5.0,
        )
        assert running.client.stats.stream_ends[0].code is grpc.StatusCode.OK
        old = await registry.get_session(first_session)
        assert (
            old is not None and old.end_reason is SessionEndReason.CONTROLLER_SHUTDOWN
        )

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        async with ControllerRuntime.start(
            make_controller_settings(pg_settings, grpc_bind=f"127.0.0.1:{port}")
        ) as second:
            await running.client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)
            took = loop.time() - t0
            assert took < 15.0, took
            assert running.client.session_id != first_session
            assert running.client.stats.sessions_accepted == 2
            assert running.client.identity.session_epoch >= 2  # one bump per attempt
            assert second.manager is not None
            assert second.manager.get(str(identity.node_id)) is not None
            node = await registry.get_node(str(identity.node_id))
            assert node is not None and node.state is NodeState.ONLINE
    finally:
        await first.stop()


@pytest.mark.asyncio
async def test_reconnect_sends_reconcile_and_replays_unacked_reports(
    pg_settings: DatabaseSettings,
    agents: AgentFactory,
    identity: NodeIdentity,
) -> None:
    first = ControllerRuntime(make_controller_settings(pg_settings))
    await first._start()
    port = first.grpc_port
    running = await agents(f"127.0.0.1:{port}", identity=identity)
    try:
        await running.client.wait_for_state(
            AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S
        )
        await first.stop()
        await running.client.wait_for_state(
            AgentState.BACKOFF,
            AgentState.DISCOVERING,
            AgentState.CONNECTING,
            timeout_s=5.0,
        )
        # Reports produced while disconnected are buffered, not lost.
        mids = [
            await running.client.send_report(
                "resource_update", agent_pb2.ResourceUpdate(cpu_percent=float(i))
            )
            for i in range(3)
        ]
        assert len(running.client.ring) == 3
        async with ControllerRuntime.start(
            make_controller_settings(pg_settings, grpc_bind=f"127.0.0.1:{port}")
        ):
            await running.client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)

            async def all_acked() -> bool:
                return len(running.client.ring) == 0

            await poll_until(all_acked, timeout_s=5.0)
            assert running.client.stats.replayed == 3
            assert running.client.stats.reports_acked == 3
            assert len(mids) == 3
    finally:
        await first.stop()


@pytest.mark.asyncio
async def test_agent_starts_before_controller_and_connects_when_it_appears(
    pg_settings: DatabaseSettings, agents: AgentFactory, identity: NodeIdentity
) -> None:
    from mf_session_client import free_port_pair

    port = free_port_pair()[0]
    running = await agents(f"127.0.0.1:{port}", identity=identity)
    await asyncio.sleep(0.3)  # a few UNAVAILABLE cycles
    assert running.client.state is not AgentState.ONLINE
    assert running.client.stats.cycles >= 1
    assert all(
        e.code is grpc.StatusCode.UNAVAILABLE for e in running.client.stats.stream_ends
    )
    async with ControllerRuntime.start(
        make_controller_settings(pg_settings, grpc_bind=f"127.0.0.1:{port}")
    ):
        await running.client.wait_for_state(AgentState.ONLINE, timeout_s=15.0)
    # Epochs were persisted for every attempt, never reused.
    assert running.client.identity.session_epoch == running.client.stats.cycles


# --------------------------------------------------------------- epoch rules


@pytest.mark.asyncio
async def test_stale_epoch_is_bumped_until_admitted(
    pg_settings: DatabaseSettings,
    registry: Registry,
    agents: AgentFactory,
    identity: NodeIdentity,
) -> None:
    """The registry already knows a higher epoch (a previous life of this node);
    the agent's first Hello is STALE_EPOCH, it bumps and retries at once.

    The subject is the epoch rule, not liveness: TOLERANT_LIVENESS, so a slow
    admission on a loaded runner (FAST_LIVENESS allows only 0.5 s) cannot turn
    the admitted Hello into UNAVAILABLE + BACKOFF and an extra epoch."""
    async with ControllerRuntime.start(
        make_controller_settings(pg_settings, **tolerant_liveness())
    ) as runtime:
        await _stale_epoch_is_bumped_until_admitted(
            registry, agents, f"127.0.0.1:{runtime.grpc_port}", identity
        )


async def _stale_epoch_is_bumped_until_admitted(
    registry: Registry,
    agents: AgentFactory,
    grpc_target: str,
    identity: NodeIdentity,
) -> None:
    node_id = str(identity.node_id)
    await registry.upsert_node_on_hello(
        node_id=node_id,
        hostname="earlier-life",
        os="x",
        platform="y",
        hardware={},
        session_epoch=2,
        session_id=new_session_id(),
        state=NodeState.OFFLINE,
    )
    running = await agents(grpc_target, identity=identity)
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    # epochs tried: 1 (stale), 2 (equal -> stale), 3 (admitted)
    assert running.client.identity.session_epoch == 3
    assert running.client.stats.stale_epoch_retries == 2
    assert running.client.stats.cycles == 1  # never went to BACKOFF
    node = await registry.get_node(node_id)
    assert (
        node is not None and node.session_epoch == 3 and node.state is NodeState.ONLINE
    )


@pytest.mark.asyncio
async def test_second_agent_with_same_identity_supersedes_first_which_exits_conflict(
    runtime: ControllerRuntime,
    registry: Registry,
    agents: AgentFactory,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    first = await agents(grpc_target, identity=identity)
    await first.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    first_session = first.client.session_id

    # Same identity directory, loaded again (as a second process would).
    second = await agents(grpc_target, identity=load_or_create(data_dir / "identity"))
    await second.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)

    exc = await first.outcome(timeout_s=10.0)
    assert isinstance(exc, SupersededError), exc
    assert exc.exit_code == ExitCode.CONFLICT
    assert exc.failure_type is FailureType.CONFLICT
    assert "another meshforge-agent" in str(exc)
    assert first.client.state is AgentState.STOPPED
    end = first.client.stats.stream_ends[-1]
    assert end.code is grpc.StatusCode.ABORTED and end.details == "superseded"

    old = await registry.get_session(first_session)
    assert old is not None and old.end_reason is SessionEndReason.SUPERSEDED
    assert second.client.state is AgentState.ONLINE
    assert second.client.identity.session_epoch > first.client.identity.session_epoch


# ------------------------------------------------------------- fatal replies


@pytest.mark.asyncio
async def test_protocol_version_rejection_is_fatal_config_error(
    pg_settings: DatabaseSettings, agents: AgentFactory, identity: NodeIdentity
) -> None:
    async with ControllerRuntime.start(
        make_controller_settings(pg_settings, accepted_protocol_versions=(2, 2))
    ) as rt:
        running = await agents(f"127.0.0.1:{rt.grpc_port}", identity=identity)
        exc = await running.outcome(timeout_s=10.0)
    assert isinstance(exc, AgentContractError), exc
    assert exc.exit_code == ExitCode.CONFIG_ERROR
    assert "PROTOCOL_VERSION" in str(exc)
    assert running.client.state is AgentState.STOPPED
    assert running.client.stats.cycles == 1  # no retry loop on a fatal reply


@pytest.mark.asyncio
async def test_malformed_hello_invalid_argument_is_fatal_config_error(
    runtime: ControllerRuntime,
    grpc_target: str,
    identity: NodeIdentity,
    data_dir: Path,
) -> None:
    from mf_agent_harness import make_agent_settings

    from meshforge.agent.session.client import AgentSessionClient

    settings = make_agent_settings(data_dir, grpc_target)
    # A hostname over the controller's cap: our own Hello is malformed.
    client = AgentSessionClient(settings, identity, host_facts=("h" * 300, "os", "p"))
    with pytest.raises(AgentContractError) as info:
        await asyncio.wait_for(client.run(), 10.0)
    assert info.value.exit_code == ExitCode.CONFIG_ERROR
    assert client.stats.stream_ends[-1].code is grpc.StatusCode.INVALID_ARGUMENT
    assert client.state is AgentState.STOPPED


# ------------------------------------------------------------------- stop


@pytest.mark.asyncio
async def test_requested_stop_is_clean_and_controller_records_agent_closed(
    runtime: ControllerRuntime,
    registry: Registry,
    agents: AgentFactory,
    grpc_target: str,
    identity: NodeIdentity,
) -> None:
    running = await agents(grpc_target, identity=identity)
    await running.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    session_id = running.client.session_id

    exc = await running.stop(timeout_s=10.0)
    assert exc is None
    assert running.client.state is AgentState.STOPPED
    assert running.client.stats.stream_ends[-1].code is grpc.StatusCode.OK

    async def closed() -> bool:
        rec = await registry.get_session(session_id)
        return rec is not None and rec.end_reason is SessionEndReason.AGENT_CLOSED

    await poll_until(closed, timeout_s=5.0)


@pytest.mark.asyncio
async def test_restart_keeps_node_id(
    runtime: ControllerRuntime,
    registry: Registry,
    agents: AgentFactory,
    grpc_target: str,
    data_dir: Path,
) -> None:
    """V0.3 criterion 4: run twice against the same data_dir -> one node, two sessions."""
    first = await agents(grpc_target)
    await first.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    node_id = first.client.node_id
    assert await first.stop() is None

    second = await agents(grpc_target)  # loads the identity again from data_dir
    await second.client.wait_for_state(AgentState.ONLINE, timeout_s=ONLINE_TIMEOUT_S)
    assert second.client.node_id == node_id
    assert second.client.identity.session_epoch == 2
    sessions = await registry.list_sessions(node_id)
    assert len(sessions) == 2
    assert len(await registry.list_nodes()) == 1


@pytest.mark.asyncio
async def test_stop_during_backoff_returns_promptly(
    agents: AgentFactory, identity: NodeIdentity
) -> None:
    from mf_session_client import free_port_pair

    port = free_port_pair()[0]
    running = await agents(
        f"127.0.0.1:{port}",
        identity=identity,
        reconnect={"base_s": 5.0, "cap_s": 5.0, "reset_after_s": 30.0},
    )
    await running.client.wait_for_state(AgentState.BACKOFF, timeout_s=5.0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await running.stop(timeout_s=5.0) is None
    assert loop.time() - t0 < 1.0
    assert running.client.state is AgentState.STOPPED
