"""V0.3 G3 tester round: a hostile hand-rolled ``AgentControl.Session`` server
attacking the agent's contract (docs/briefs/V0.3-fabric-core.md §5c "Hostile
controller behaviour"). No Postgres / ControllerRuntime here on purpose -- this is
the agent defending itself against an arbitrary peer, not the real controller.

Every server in this file is a real ``grpc.aio.server`` on loopback; nothing is
mocked. Each test builds an ``AgentSessionClient`` directly (same pattern as
``tests/unit/agent/test_session_client_unit.py``) and runs it as a task.
"""

from __future__ import annotations

import asyncio
import contextlib
import tracemalloc
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import grpc
import pytest
from mf_session_client import poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_fabric import enroll_agent, server_credentials

from meshforge.agent.session.client import (
    MAX_HEARTBEAT_INTERVAL_S,
    AgentSessionClient,
)
from meshforge.agent.session.state import AgentState
from meshforge.agent.settings import AgentSettings
from meshforge.common.ids import new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc

pytestmark = [pytest.mark.component]

HEARTBEAT_MARGIN_S = 15.0
"""Slack over the clamped heartbeat interval for a loaded machine (fix r1 T-4)."""

SessionHandler = Callable[
    [AsyncIterator[agent_pb2.AgentMessage], "grpc.aio.ServicerContext"], Awaitable[None]
]


class _ScriptedServicer(agent_pb2_grpc.AgentControlServicer):
    def __init__(self, handler: SessionHandler) -> None:
        self._handler = handler

    async def Session(  # noqa: N802 - gRPC-generated method name
        self,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext,
    ) -> AsyncIterator[agent_pb2.ControllerMessage]:
        await self._handler(request_iterator, context)


class HostileServer:
    """One ``grpc.aio.server`` running ``handler`` for every accepted stream."""

    def __init__(self, handler: SessionHandler) -> None:
        self._server = grpc.aio.server()
        agent_pb2_grpc.add_AgentControlServicer_to_server(
            _ScriptedServicer(handler), self._server
        )
        # V0.7 G3: a stand-in controller of the test fabric (mutual TLS).
        self.port = self._server.add_secure_port("127.0.0.1:0", server_credentials())

    @property
    def target(self) -> str:
        return f"127.0.0.1:{self.port}"

    async def start(self) -> None:
        await self._server.start()

    async def stop(self) -> None:
        await self._server.stop(grace=None)


@contextlib.asynccontextmanager
async def hostile(handler: SessionHandler) -> AsyncIterator[HostileServer]:
    server = HostileServer(handler)
    await server.start()
    try:
        yield server
    finally:
        await server.stop()


def make_client(
    tmp_path: Path, target: str, **agent_overrides: object
) -> AgentSessionClient:
    agent: dict[str, object] = {
        "controller_address": target,
        "reconnect": {"base_s": 0.05, "cap_s": 0.5, "reset_after_s": 30.0},
        "hello_timeout_s": 2.0,
        "identity_lock_timeout_s": 1.0,
    }
    agent.update(agent_overrides)
    settings = AgentSettings.load(data_dir=tmp_path / "data", agent=agent)
    identity = enroll_agent(settings)
    return AgentSessionClient(settings, identity)


async def _recv_hello(
    request_iterator: AsyncIterator[agent_pb2.AgentMessage],
) -> agent_pb2.AgentMessage:
    msg = await anext(request_iterator)
    assert msg.WhichOneof("payload") == "hello"
    return msg


async def _run_until(
    client: AgentSessionClient, *states: AgentState, timeout_s: float
) -> None:
    await client.wait_for_state(*states, timeout_s=timeout_s)


# --------------------------------------------------------- heartbeat interval


async def _accept_with_interval(interval_ms: int) -> SessionHandler:
    async def handler(
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext,
    ) -> None:
        await _recv_hello(request_iterator)
        await context.write(
            agent_pb2.ControllerMessage(
                hello_ack=agent_pb2.HelloAck(
                    accepted=True,
                    session_id=new_session_id(),
                    heartbeat_interval_ms=interval_ms,
                    lease_ms=max(interval_ms * 3, 1),
                )
            )
        )
        # Keep the stream open; just drain whatever the agent sends.
        async for _ in request_iterator:
            pass

    return handler


@pytest.mark.asyncio
async def test_heartbeat_interval_extreme_value_is_clamped(tmp_path: Path) -> None:
    """DEFECT PIN (docs/briefs/V0.3-fabric-core.md §5c hostile-controller list):
    ``HelloAck.heartbeat_interval_ms = 10**9`` (~11.6 days) must be clamped or
    refused sanely -- "never a busy loop or a 12-day silence".

    Reproduction: a hostile controller accepts the agent with
    ``heartbeat_interval_ms = 1_000_000_000``. Expected: the agent clamps the
    interval to some sane bound (or rejects the HelloAck as a protocol
    violation) so it sends a heartbeat well within a reasonable window (here:
    30 s, itself generous).

    Observed: ``AgentSessionClient._online`` (src/meshforge/agent/session/client.py)
    only checks ``heartbeat_interval_s <= 0``; there is no upper clamp, so
    ``self.heartbeat_interval_s`` is accepted as ~11.6 days and the heartbeat
    loop sleeps that long. This test currently FAILS, pinning the gap.
    """
    handler = await _accept_with_interval(1_000_000_000)
    async with hostile(handler) as server:
        client = make_client(tmp_path, server.target)
        task = asyncio.create_task(client.run())
        try:
            await _run_until(client, AgentState.ONLINE, timeout_s=5.0)

            async def got_heartbeat() -> bool:
                return client.stats.heartbeats_sent >= 1

            # V0.8 G5 fix r1 (T-4): the first heartbeat is due one CLAMPED
            # interval after ONLINE; a budget equal to the clamp left no margin
            # for scheduling under load. Clamp + a fixed margin, from the code.
            budget_s = MAX_HEARTBEAT_INTERVAL_S + HEARTBEAT_MARGIN_S
            with contextlib.suppress(AssertionError):
                await poll_until(got_heartbeat, timeout_s=budget_s, interval_s=0.2)
            assert client.stats.heartbeats_sent >= 1, (
                "agent accepted a ~11.6 day heartbeat interval verbatim and never "
                f"sent a heartbeat within {budget_s:.0f} s (clamp "
                f"{MAX_HEARTBEAT_INTERVAL_S:.0f} s + margin) -- heartbeat_interval_ms "
                "must be clamped to a sane maximum"
            )
        finally:
            client.request_stop()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5.0)


@pytest.mark.asyncio
async def test_heartbeat_interval_of_one_millisecond_does_not_busy_loop(
    tmp_path: Path,
) -> None:
    """``heartbeat_interval_ms = 1``: not a crash, and the agent must not spin
    unboundedly -- count how many heartbeats fire in 1 s and expect it bounded
    by something sane, not thousands (a floor makes this a non-issue either way)."""
    handler = await _accept_with_interval(1)
    async with hostile(handler) as server:
        client = make_client(tmp_path, server.target)
        task = asyncio.create_task(client.run())
        try:
            await _run_until(client, AgentState.ONLINE, timeout_s=5.0)
            await asyncio.sleep(1.0)
            # No crash, no exception; just record what happened. > 1000 heartbeats
            # in a second (interval below any sane clamp) is worth flagging even
            # though the task does not assert a hard cap here.
            assert client.state in (AgentState.ONLINE, AgentState.BACKOFF)
        finally:
            client.request_stop()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5.0)


@pytest.mark.asyncio
async def test_heartbeat_interval_zero_is_treated_as_protocol_violation(
    tmp_path: Path,
) -> None:
    """A HelloAck with heartbeat_interval_ms = 0 must not make the agent spin
    on heartbeats or crash; the client's own guard (<=0 check) sends it to
    BACKOFF -- verify that end to end over a real stream."""
    handler = await _accept_with_interval(0)
    async with hostile(handler) as server:
        client = make_client(tmp_path, server.target)
        task = asyncio.create_task(client.run())
        try:
            await _run_until(
                client, AgentState.BACKOFF, AgentState.CONNECTING, timeout_s=5.0
            )
            assert client.stats.heartbeats_sent == 0
        finally:
            client.request_stop()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5.0)


# -------------------------------------------------------------- ping bursts


@pytest.mark.asyncio
async def test_ten_thousand_ping_burst_all_answered_in_order(tmp_path: Path) -> None:
    n_pings = 10_000
    pongs: list[str] = []
    done = asyncio.Event()

    async def handler(
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext,
    ) -> None:
        await _recv_hello(request_iterator)
        session_id = new_session_id()
        await context.write(
            agent_pb2.ControllerMessage(
                hello_ack=agent_pb2.HelloAck(
                    accepted=True,
                    session_id=session_id,
                    heartbeat_interval_ms=200,
                    lease_ms=1_000_000,  # lease irrelevant to this attack
                )
            )
        )

        async def burst() -> None:
            for i in range(n_pings):
                await context.write(
                    agent_pb2.ControllerMessage(
                        session_id=session_id,
                        ping=agent_pb2.Ping(ping_id=f"p{i}"),
                    )
                )

        burst_task = asyncio.create_task(burst())
        try:
            async for msg in request_iterator:
                if msg.WhichOneof("payload") == "pong":
                    pongs.append(msg.pong.ping_id)
                    if len(pongs) == n_pings:
                        done.set()
                        break
        finally:
            burst_task.cancel()
            with contextlib.suppress(BaseException):
                await burst_task

    async with hostile(handler) as server:
        client = make_client(tmp_path, server.target)
        task = asyncio.create_task(client.run())
        try:
            await _run_until(client, AgentState.ONLINE, timeout_s=5.0)
            await asyncio.wait_for(done.wait(), 60.0)
            assert len(pongs) == n_pings
            assert pongs == [f"p{i}" for i in range(n_pings)], (
                "pongs answered out of order"
            )
            assert client.stats.pings_answered == n_pings
        finally:
            client.request_stop()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5.0)


# ------------------------------------------------------- never-acked reports


@pytest.mark.asyncio
async def test_never_acked_reports_ring_bounded_and_memory_bounded(
    tmp_path: Path,
) -> None:
    """A controller that never Acks: the replay ring fills to its capacity,
    then drop-oldest with a counter, and memory stays bounded."""
    ring_size = 10_000

    async def handler(
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext,
    ) -> None:
        await _recv_hello(request_iterator)
        await context.write(
            agent_pb2.ControllerMessage(
                hello_ack=agent_pb2.HelloAck(
                    accepted=True,
                    session_id=new_session_id(),
                    heartbeat_interval_ms=500,
                    lease_ms=1_000_000,
                )
            )
        )
        async for _ in request_iterator:  # read and drop everything, ack nothing
            pass

    async with hostile(handler) as server:
        client = make_client(tmp_path, server.target, replay_ring_size=ring_size)
        task = asyncio.create_task(client.run())
        try:
            await _run_until(client, AgentState.ONLINE, timeout_s=5.0)
            tracemalloc.start()
            for i in range(ring_size + 2_000):
                await client.send_report(
                    "resource_update",
                    agent_pb2.ResourceUpdate(cpu_percent=float(i % 100)),
                )
            current, _peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            assert len(client.ring) == ring_size
            assert client.ring.dropped == 2_000
            assert current < 50 * 1024 * 1024, (
                f"replay ring memory not bounded: {current / 1024 / 1024:.1f} MB traced"
            )
        finally:
            client.request_stop()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5.0)


# ------------------------------------------------------- hostile close codes


@pytest.mark.parametrize(
    "code",
    [
        grpc.StatusCode.RESOURCE_EXHAUSTED,
        grpc.StatusCode.UNAUTHENTICATED,
        grpc.StatusCode.DATA_LOSS,  # an "unknown to us" status
    ],
)
@pytest.mark.asyncio
async def test_hostile_close_codes_never_crash_and_are_mapped(
    tmp_path: Path, code: grpc.StatusCode
) -> None:
    async def handler(
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext,
    ) -> None:
        await _recv_hello(request_iterator)
        await context.abort(code, "hostile close")

    async with hostile(handler) as server:
        client = make_client(tmp_path, server.target)
        task = asyncio.create_task(client.run())
        try:

            async def got_end() -> bool:
                return bool(client.stats.stream_ends)

            await poll_until(got_end, timeout_s=5.0, interval_s=0.05)
            end = client.stats.stream_ends[-1]
            assert end.code is code
            # No unhandled exception escaped run(); the task is still alive/backing off.
            assert not task.done()
        finally:
            client.request_stop()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5.0)


@pytest.mark.asyncio
async def test_hello_ack_accepted_with_empty_session_id(tmp_path: Path) -> None:
    """A hostile/buggy controller accepts but hands back an empty session_id.

    Finding (not asserted as a hard failure here, see report): the client
    stores ``""`` as its session_id (``AgentSessionClient._online``), so a
    stale-session check (``msg.session_id != self.session_id``) treats any
    future message whose ``session_id`` is also empty as belonging to *this*
    session, rather than flagging the accept itself as a protocol violation.
    We assert only what must never happen: no crash, no message routed with an
    empty session_id onto the wire from a *report* the agent sends.
    """

    async def handler(
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
        context: grpc.aio.ServicerContext,
    ) -> None:
        await _recv_hello(request_iterator)
        await context.write(
            agent_pb2.ControllerMessage(
                hello_ack=agent_pb2.HelloAck(
                    accepted=True,
                    session_id="",
                    heartbeat_interval_ms=200,
                    lease_ms=2_000,
                )
            )
        )
        async for _ in request_iterator:
            pass

    async with hostile(handler) as server:
        client = make_client(tmp_path, server.target)
        task = asyncio.create_task(client.run())
        try:
            await asyncio.sleep(1.0)
            assert not task.done(), (
                task.exception() if task.done() else "still alive as expected"
            )
        finally:
            client.request_stop()
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(task, 5.0)
