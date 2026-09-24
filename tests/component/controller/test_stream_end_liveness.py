"""V0.3 G6 component tests: how a session ends decides ``node.state`` (D19) and
session state writes are fenced (D20).

Real :class:`~meshforge.controller.server.ControllerRuntime` + real PostgreSQL +
the hand-rolled :class:`FakeAgent` over real gRPC sockets (``FAST_LIVENESS``:
heartbeat 0.2 s, SUSPECT at 0.25 s, OFFLINE at 0.6 s). The D20 race drives a
:class:`SessionManager` over a real :class:`Registry` with one write held on an
``asyncio.Event``.

Abrupt paths exercised for real here: the agent's call cancelled without a
half-close, its channel closed without a half-close, and a cancel immediately
after a half-close. (A killed agent *process* is in
``tests/component/agent/test_agent_process.py``.) A TCP reset or a silent
blackhole needs G6-B's fault proxy and is not attempted here.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
import structlog
from mf_session_client import FakeAgent, make_hello, poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.liveness import LivenessConfig, NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.session.manager import Accepted, SessionManager

pytestmark = pytest.mark.component

LEASE_S = 0.6  # FAST_LIVENESS: 0.2 s x 3


async def node_state(registry: Registry, node_id: str) -> NodeState | None:
    rec = await registry.get_node(node_id)
    return None if rec is None else rec.state


async def end_reason(registry: Registry, session_id: str) -> SessionEndReason | None:
    rec = await registry.get_session(session_id)
    return None if rec is None else rec.end_reason


def changes_for(logs: list[dict[str, Any]], node_id: str) -> list[tuple[str, str]]:
    return [
        (e["from"], e["to"])
        for e in logs
        if e["event"] == "node.state_changed" and e.get("node_id") == node_id
    ]


async def wait_state(
    registry: Registry, node_id: str, state: NodeState, timeout_s: float = 5.0
) -> None:
    async def reached() -> bool:
        return await node_state(registry, node_id) is state

    await poll_until(reached, timeout_s=timeout_s, interval_s=0.01)


@contextlib.asynccontextmanager
async def own_channel(target: str) -> AsyncIterator[grpc.aio.Channel]:
    async with grpc.aio.insecure_channel(target) as ch:
        yield ch


# ------------------------------------------------------------------- clean


@pytest.mark.asyncio
async def test_graceful_close_is_offline_at_once_and_agent_closed(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    ack = await agent.connect()
    await agent.close(graceful=True)
    assert agent.end_code is grpc.StatusCode.OK
    await wait_state(registry, agent.node_id, NodeState.OFFLINE, timeout_s=LEASE_S)
    assert await end_reason(registry, ack.session_id) is SessionEndReason.AGENT_CLOSED
    assert runtime.manager is not None
    assert runtime.manager.get(agent.node_id) is None
    assert runtime.manager.get_lingering(agent.node_id) is None


# ------------------------------------------------------------------ abrupt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "how", ["cancel_call", "close_channel", "half_close_then_cancel"]
)
async def test_abrupt_end_is_suspect_then_offline_at_the_lease_edge(
    runtime: ControllerRuntime, grpc_target: str, registry: Registry, how: str
) -> None:
    async with own_channel(grpc_target) as ch:
        agent = FakeAgent(ch)
        ack = await agent.connect()
        await agent.heartbeat()
        with structlog.testing.capture_logs() as logs:
            if how == "cancel_call":
                await agent.abort()
            elif how == "close_channel":
                agent.stop_heartbeats()
                await ch.close()
            else:
                # A cancel right behind the half-close: the controller cannot tell
                # this from a drop (grpc.aio), so it must not claim agent_closed.
                await agent.close()

            async def lost() -> bool:
                return await end_reason(registry, ack.session_id) is (
                    SessionEndReason.STREAM_LOST
                )

            await poll_until(lost, timeout_s=5.0, interval_s=0.01)
            assert await node_state(registry, agent.node_id) in (
                NodeState.SUSPECT,
                NodeState.OFFLINE,  # only if this machine stalled past the lease
            )
            await wait_state(registry, agent.node_id, NodeState.OFFLINE)
        assert changes_for(logs, agent.node_id) == [
            ("online", "suspect"),
            ("suspect", "offline"),
        ]
        expired = [e for e in logs if e["event"] == "node.lease_expired"]
        assert [e["session_id"] for e in expired] == [ack.session_id]
        assert expired[0]["lingering"] is True
        assert expired[0]["silence_s"] >= LEASE_S
        assert runtime.manager is not None
        assert runtime.manager.get_lingering(agent.node_id) is None


@pytest.mark.asyncio
async def test_reconnect_before_the_edge_is_online_with_no_offline_ever(
    runtime: ControllerRuntime, grpc_target: str, registry: Registry
) -> None:
    async with own_channel(grpc_target) as ch:
        first = FakeAgent(ch, epoch=1)
        ack1 = await first.connect()
        with structlog.testing.capture_logs() as logs:
            await first.abort()

            async def lost() -> bool:
                return await end_reason(registry, ack1.session_id) is (
                    SessionEndReason.STREAM_LOST
                )

            await poll_until(lost, timeout_s=5.0, interval_s=0.01)
            second = FakeAgent(ch, node_id=first.node_id, epoch=2)
            await second.connect()
            second.start_heartbeats(0.1)
            await asyncio.sleep(LEASE_S * 3)  # well past the old lease edge
            assert await node_state(registry, first.node_id) is NodeState.ONLINE
        await second.close(graceful=True)
        assert changes_for(logs, first.node_id) == [
            ("online", "suspect"),  # the loss
            ("suspect", "online"),  # the reconnect - and nothing else
        ]
        assert not [
            e
            for e in logs
            if e["event"] == "node.lease_expired" and e["session_id"] == ack1.session_id
        ]


@pytest.mark.asyncio
async def test_a_stale_epoch_hello_does_not_rescue_a_lingering_node(
    runtime: ControllerRuntime, grpc_target: str, registry: Registry
) -> None:
    async with own_channel(grpc_target) as ch:
        first = FakeAgent(ch, epoch=5)
        await first.connect()
        await first.abort()
        stale = FakeAgent(ch, node_id=first.node_id, epoch=5)
        reply = await stale.hello()
        assert reply is not None and not reply.hello_ack.accepted
        await wait_state(registry, first.node_id, NodeState.OFFLINE)
        await stale.close()


@pytest.mark.asyncio
async def test_supersession_writes_no_state_from_the_old_session(
    runtime: ControllerRuntime, grpc_target: str, registry: Registry
) -> None:
    async with own_channel(grpc_target) as ch:
        old = FakeAgent(ch, epoch=1)
        old_ack = await old.connect()
        old.start_heartbeats(0.1)
        with structlog.testing.capture_logs() as logs:
            new = FakeAgent(ch, node_id=old.node_id, epoch=2)
            await new.connect()
            new.start_heartbeats(0.1)
            await old.wait_ended()
            await asyncio.sleep(LEASE_S * 2)
        assert old.end_code is grpc.StatusCode.ABORTED
        assert (
            await end_reason(registry, old_ack.session_id)
            is SessionEndReason.SUPERSEDED
        )
        assert not [
            e
            for e in logs
            if e.get("session_id") == old_ack.session_id
            and e["event"] in ("node.state_changed", "registry.state_write_fenced")
        ]
        assert await node_state(registry, old.node_id) is NodeState.ONLINE
        await new.close(graceful=True)
        await old.close()


# ------------------------------------------------------- controller shutdown


@pytest.mark.asyncio
async def test_controller_graceful_stop_leaves_live_and_lingering_rows_offline(
    runtime: ControllerRuntime, grpc_target: str, registry: Registry
) -> None:
    async with own_channel(grpc_target) as ch:
        live = FakeAgent(ch)
        live_ack = await live.connect()
        live.start_heartbeats(0.1)
        gone = FakeAgent(ch)
        gone_ack = await gone.connect()
        await gone.abort()

        async def lingering() -> bool:
            return runtime.manager is not None and bool(
                runtime.manager.get_lingering(gone.node_id)
            )

        await poll_until(lingering, timeout_s=5.0, interval_s=0.01)
        await runtime.stop()  # the fixture's own stop is then a no-op
        await live.wait_ended()
        assert live.end_code is grpc.StatusCode.OK
        for node_id in (live.node_id, gone.node_id):
            assert await node_state(registry, node_id) is NodeState.OFFLINE
        assert (
            await end_reason(registry, live_ack.session_id)
            is SessionEndReason.CONTROLLER_SHUTDOWN
        )
        assert (
            await end_reason(registry, gone_ack.session_id)
            is SessionEndReason.STREAM_LOST
        )
        await live.close()


# ------------------------------------------------------------- D20 race


class _GatedRegistry:
    """The real registry, with one session's ``set_node_state`` held on an Event."""

    def __init__(self, inner: Registry) -> None:
        self._inner = inner
        self.hold_session: str | None = None
        self.held = asyncio.Event()
        self.release = asyncio.Event()
        self.results: list[tuple[str, NodeState, bool]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def set_node_state(
        self, node_id: str, state: NodeState, *, session_id: str
    ) -> bool:
        if session_id == self.hold_session:
            self.held.set()
            await self.release.wait()
        ok = await self._inner.set_node_state(node_id, state, session_id=session_id)
        self.results.append((session_id, state, ok))
        return ok


@pytest.mark.asyncio
async def test_d20_a_held_old_write_cannot_overwrite_a_newer_sessions_online(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """Block the old session's chain on its OFFLINE (a clean close, as on every
    agent restart), admit a newer session, release: the row must read ONLINE and
    the node must be connected. Drop the ``last_session_id`` condition from
    ``Registry.set_node_state`` and this fails (mutation-checked)."""
    gated = _GatedRegistry(registry)
    manager = SessionManager(
        gated,  # type: ignore[arg-type]
        LivenessConfig(heartbeat_interval_s=30.0),  # no lease events in this test
        clock=asyncio.get_running_loop().time,
        half_close_confirm_s=0.0,
    )
    node_id = new_node_id()
    try:
        first = await manager.admit(make_hello(node_id, 1), "ipv4:127.0.0.1:1")
        assert isinstance(first, Accepted)
        old = first.live
        gated.hold_session = old.session_id
        await manager.end_session(old, SessionEndReason.AGENT_CLOSED)
        await asyncio.wait_for(gated.held.wait(), 5.0)  # OFFLINE is stuck

        second = await manager.admit(make_hello(node_id, 2), "ipv4:127.0.0.1:2")
        assert isinstance(second, Accepted)
        assert await node_state(registry, node_id) is NodeState.ONLINE

        with structlog.testing.capture_logs() as logs:
            gated.release.set()
            assert old.writer.task is not None
            await asyncio.wait_for(old.writer.task, 5.0)
        assert (old.session_id, NodeState.OFFLINE, False) in gated.results
        (fenced,) = [e for e in logs if e["event"] == "registry.state_write_fenced"]
        assert fenced["session_id"] == old.session_id
        assert fenced["log_level"] == "info"
        assert await node_state(registry, node_id) is NodeState.ONLINE
        assert manager.get(node_id) is second.live  # connected (D16) is true
    finally:
        await manager.shutdown(drain_s=1.0)


# ------------------------------------------- the grpc.aio fact D19 rests on


class _Probe(agent_pb2_grpc.AgentControlServicer):
    def __init__(self) -> None:
        self.iterator_end: str | None = None
        self.handler_cancelled = asyncio.Event()
        self.done = asyncio.Event()

    async def Session(  # noqa: N802
        self, request_iterator: Any, context: Any
    ) -> AsyncIterator[agent_pb2.ControllerMessage]:
        async def read() -> None:
            try:
                async for _ in request_iterator:
                    pass
                self.iterator_end = "normal"
            except Exception as exc:  # noqa: BLE001 - record any other outcome
                self.iterator_end = type(exc).__name__
            finally:
                self.done.set()

        reader = asyncio.create_task(read())
        yield agent_pb2.ControllerMessage(message_id=new_command_id())
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            self.handler_cancelled.set()
            raise
        finally:
            reader.cancel()


@pytest.mark.asyncio
async def test_grpc_aio_ends_the_request_iterator_normally_on_a_client_cancel() -> None:
    """Canary for SessionManager's half-close confirmation: grpc.aio shows a
    cancelled call as a *normal* end of the request iterator, and only the
    handler's cancellation tells it apart from a half-close. If a grpcio upgrade
    changes this, revisit ``HALF_CLOSE_CONFIRM_S`` (it could become unnecessary)."""
    probe = _Probe()
    server = grpc.aio.server()
    agent_pb2_grpc.add_AgentControlServicer_to_server(probe, server)
    port = server.add_insecure_port("127.0.0.1:0")
    await server.start()
    try:
        async with grpc.aio.insecure_channel(f"127.0.0.1:{port}") as ch:
            call = agent_pb2_grpc.AgentControlStub(ch).Session()
            await call.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(), hello=make_hello(new_node_id(), 1)
                )
            )
            await call.read()
            call.cancel()
            await asyncio.wait_for(probe.done.wait(), 5.0)
            await asyncio.wait_for(probe.handler_cancelled.wait(), 5.0)
        assert probe.iterator_end == "normal"
    finally:
        await server.stop(0)
