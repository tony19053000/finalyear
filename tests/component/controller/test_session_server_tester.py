"""Independent adversarial component tests for V0.3 G2 (tester round).

Real everything: PostgreSQL (``pg_settings``), a real ``ControllerRuntime`` (grpc.aio
+ uvicorn) on ephemeral loopback ports, real ``AgentControl.Session`` streams driven
by the shared ``FakeAgent`` helper (``tests/_support/mf_session_client.py``) and, for
the attacks that need raw control over the wire, a bare ``AgentControlStub`` call.
No mocks. Targets the tester brief's attack list (protocol abuse, concurrency,
process/DB failures, DB-level constraints) that
``tests/component/controller/test_session_server.py`` does not already cover.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import grpc
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from mf_fabric import NodeChannels
from mf_session_client import FakeAgent, make_hello, poll_until
from sqlalchemy import text

from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.models.node import SessionEndReason

if TYPE_CHECKING:
    from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.component

from mf_controller_harness import make_settings, tolerant_liveness  # noqa: E402

UINT64_MAX = 2**64 - 1


# --------------------------------------------------------------------------
# Protocol abuse over a real stream
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hello_with_max_uint64_epoch_is_handled_not_a_server_crash(
    channel: grpc.aio.Channel, registry: Registry
) -> None:
    """DEFECT PIN: the tester brief explicitly calls for "a Hello with
    session_epoch = 2**64-1" as an attack. ``Hello.session_epoch`` is proto
    ``uint64`` (max 18446744073709551615) but
    ``src/meshforge/controller/store/models/node.py`` stores it as
    ``BigInteger`` (Postgres ``int8``, signed 64-bit, max 9223372036854775807)
    with no range check anywhere on the accept path
    (``SessionManager._admit_locked`` -> ``Registry.upsert_node_on_hello``).

    Reproduction: an agent sends ``Hello{session_epoch = 2**64-1}``.

    Expected: either the value is accepted and round-trips (the column should
    be wide enough for the field's full domain), or the Hello is cleanly
    rejected before any database write (e.g. a typed ``HelloAck{accepted=false}``
    or a clean abort) - either way the controller must keep working for the
    next node.
    Observed: ``asyncpg`` raises ``DataError: value out of int64 range`` deep
    inside ``upsert_node_on_hello``; it surfaces as an *unhandled*
    ``RegistryError`` in ``AgentControlServicer.Session`` (nothing there
    catches it - only the reader loop has a ``try/except``), logged by grpc.aio
    as "Unexpected [RegistryError] raised by servicer method". No HelloAck is
    ever sent; no row is ever committed (the INSERT itself failed) - a stale
    epoch's worth of protection is lost silently instead of a clean
    STALE_EPOCH/PROTOCOL_VERSION-style rejection.
    """
    node_id = new_node_id()
    stub = agent_pb2_grpc.AgentControlStub(channel.for_node(node_id))
    call = stub.Session()
    await call.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(), hello=make_hello(node_id, UINT64_MAX)
        )
    )
    ack = await call.read()
    assert ack.hello_ack.accepted, (
        "Hello with session_epoch=2**64-1 must be handled cleanly (accepted "
        f"with the value intact, or a typed rejection) - got: {ack!r}"
    )
    rec = await registry.get_node(node_id)
    assert rec is not None and rec.session_epoch == UINT64_MAX

    # Whatever happened above, the server must still serve other nodes.
    other = new_node_id()
    stub2 = agent_pb2_grpc.AgentControlStub(channel.for_node(other))
    call2 = stub2.Session()
    await call2.write(
        agent_pb2.AgentMessage(message_id=new_command_id(), hello=make_hello(other, 1))
    )
    ack2 = await call2.read()
    assert ack2.hello_ack.accepted
    await call2.done_writing()


@pytest.mark.asyncio
async def test_second_hello_on_open_stream_with_extreme_epoch_is_protocol_error(
    channel: grpc.aio.Channel, registry: Registry
) -> None:
    node_id = new_node_id()
    stub = agent_pb2_grpc.AgentControlStub(channel.for_node(node_id))
    call = stub.Session()
    await call.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(), hello=make_hello(node_id, 1)
        )
    )
    ack = await call.read()
    assert ack.hello_ack.accepted, ack

    await call.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(), hello=make_hello(node_id, 2)
        )
    )
    # Review NB1: a protocol_error end is ABORTED, so after EOF the next read
    # (or the status) reports it; either way the stream ends with that reason.
    code: grpc.StatusCode | None = None
    try:
        while True:
            msg = await call.read()
            if msg is grpc.aio.EOF:
                code = await call.code()
                break
    except grpc.aio.AioRpcError as exc:
        code = exc.code()
    assert code is grpc.StatusCode.ABORTED, code
    assert await call.details() == SessionEndReason.PROTOCOL_ERROR.value
    session_id = ack.hello_ack.session_id
    rec = await registry.get_session(session_id)
    assert rec is not None and not rec.is_open
    assert rec.end_reason is SessionEndReason.PROTOCOL_ERROR
    node_rec = await registry.get_node(node_id)
    assert node_rec is not None and node_rec.session_epoch == 1


@pytest.mark.asyncio
async def test_agent_message_with_empty_message_id_closes_protocol_error(
    channel: grpc.aio.Channel, registry: Registry, agents: Any
) -> None:
    agent: FakeAgent = agents()
    await agent.connect()
    session_id = agent.session_id

    await agent.write(
        agent_pb2.AgentMessage(
            message_id="",
            session_id=session_id,
            heartbeat=agent_pb2.Heartbeat(sequence=1),
        )
    )
    await agent.wait_ended(timeout_s=5.0)
    rec = await registry.get_session(session_id)
    assert rec is not None
    assert rec.end_reason is SessionEndReason.PROTOCOL_ERROR


@pytest.mark.asyncio
async def test_pong_with_unknown_ping_id_is_ignored_session_stays_alive(
    agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents(auto_pong=False)
    await agent.connect()
    await agent.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=agent.session_id,
            pong=agent_pb2.Pong(ping_id="totally-unknown"),
        )
    )
    agent.start_heartbeats(0.05)
    await asyncio.sleep(0.3)
    assert not agent.ended.is_set()
    rec = await registry.get_node(agent.node_id)
    assert rec is not None and rec.state is NodeState.ONLINE


@pytest.mark.asyncio
async def test_pong_before_any_ping_does_not_crash_the_session(
    agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents(auto_pong=False)
    await agent.connect()
    await agent.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=agent.session_id,
            pong=agent_pb2.Pong(ping_id="never-issued"),
        )
    )
    agent.start_heartbeats(0.05)
    await asyncio.sleep(0.3)
    assert not agent.ended.is_set()


@pytest.mark.asyncio
async def test_heartbeat_sequence_going_backwards_and_jumping_never_crashes(
    agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    await agent.connect()
    for seq in (100, 3, 2**63, 0, 1):
        await agent.write(
            agent_pb2.AgentMessage(
                message_id=new_command_id(),
                session_id=agent.session_id,
                heartbeat=agent_pb2.Heartbeat(sequence=seq),
            )
        )
    await asyncio.sleep(0.1)
    assert not agent.ended.is_set()
    rec = await registry.get_node(agent.node_id)
    assert rec is not None and rec.state is NodeState.ONLINE


@pytest.mark.asyncio
async def test_ack_for_message_id_the_controller_never_sent(
    agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    await agent.connect()
    await agent.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=agent.session_id,
            ack=agent_pb2.Ack(acked_message_id="cmd-that-was-never-sent"),
        )
    )
    await asyncio.sleep(0.1)
    assert not agent.ended.is_set()


@pytest.mark.security
@pytest.mark.asyncio
async def test_burst_of_2000_heartbeats_never_hangs_the_server(
    channel: grpc.aio.Channel, registry: Registry
) -> None:
    """FINDING (not pinned as a hard failure - no documented SLA to violate):
    a client that writes a burst of heartbeats without ever reading the
    ControllerMessage stream, and each accepted heartbeat serialises a
    registry write (``await self._persist(...)`` is *awaited*, one DB
    round-trip per message, before the reader loop moves to the next message
    - see ``SessionManager._dispatch``/``_persist``), can end up slower than
    the lease itself: 2000 heartbeats sent back-to-back on FAST_LIVENESS
    (lease 0.6 s) took long enough in this run that the session was closed
    ``lease_expired`` even though the agent never stopped heartbeating. This
    is a real robustness edge (roadmap attack list: "5000 messages/s for
    3 s") worth a look before the real agent's heartbeat cadence is fixed at
    V0.3 G3/§7 - flagged for the team, not blocking G2. The hard requirement
    this test does check: whatever happens, the server must not hang and the
    session must end with a *typed* reason, never silently."""
    node_id = new_node_id()
    stub = agent_pb2_grpc.AgentControlStub(channel.for_node(node_id))
    call = stub.Session()
    await call.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(), hello=make_hello(node_id, 1)
        )
    )
    ack = await call.read()
    session_id = ack.hello_ack.session_id
    assert ack.hello_ack.accepted

    # (Round 2: message_id must be a cmd_<ULID> - a bare label is a protocol
    # error on the first message, which would end the flood before it starts.)
    for i in range(2000):
        try:
            await call.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    session_id=session_id,
                    heartbeat=agent_pb2.Heartbeat(sequence=i),
                )
            )
        except asyncio.InvalidStateError:
            break  # the controller already ended the stream (typed, below)

    async def closed() -> bool:
        rec = await registry.get_session(session_id)
        return rec is not None and not rec.is_open

    await poll_until(closed, timeout_s=15.0)  # must not hang
    rec = await registry.get_session(session_id)
    assert rec is not None
    assert rec.end_reason in (
        SessionEndReason.PROTOCOL_ERROR,
        SessionEndReason.LEASE_EXPIRED,
        SessionEndReason.AGENT_CLOSED,
    ), rec.end_reason


@pytest.mark.security
@pytest.mark.asyncio
async def test_message_larger_than_default_grpc_max_ends_cleanly(
    channel: grpc.aio.Channel, registry: Registry
) -> None:
    """A payload over gRPC's default 4 MiB max receive size must end the stream
    with a clean status, not crash the controller or wedge the connection for
    the next agent."""
    node_id = new_node_id()
    stub = agent_pb2_grpc.AgentControlStub(channel.for_node(node_id))
    call = stub.Session()
    await call.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(), hello=make_hello(node_id, 1)
        )
    )
    ack = await call.read()
    session_id = ack.hello_ack.session_id
    huge = agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=session_id,
        log_chunk=agent_pb2.LogChunk(
            job_id="j",
            attempt_id="a",
            data=b"x" * (5 * 1024 * 1024),
        ),
    )
    await call.write(huge)
    code = None
    try:
        while True:
            reply = await call.read()
            if reply is grpc.aio.EOF:
                code = await call.code()
                break
    except grpc.aio.AioRpcError as exc:
        code = exc.code()
    assert code == grpc.StatusCode.RESOURCE_EXHAUSTED, code

    # The controller must still be answering new sessions afterwards.
    other = new_node_id()
    stub2 = agent_pb2_grpc.AgentControlStub(channel.for_node(other))
    call2 = stub2.Session()
    await call2.write(
        agent_pb2.AgentMessage(message_id=new_command_id(), hello=make_hello(other, 1))
    )
    ack2 = await call2.read()
    assert ack2.hello_ack.accepted
    await call2.done_writing()


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_twenty_nodes_each_with_three_racing_epochs(
    pg_settings: DatabaseSettings, registry: Registry
) -> None:
    # Higher epoch always wins regardless of arrival order (ADR-0002 Updates
    # §1: never compares across node_ids, and lower-arriving-later is simply
    # rejected, never supersedes) - so the epoch-3 stream is always the
    # eventual sole survivor. It is heartbeated to stay alive while the
    # (slower, DB-serialised-under-one-lock) admissions for all 60 streams
    # settle, isolating the fencing invariant from unrelated lease timing.
    #
    # Its own controller with TOLERANT_LIVENESS: under FAST_LIVENESS the
    # admission deadline is 0.5 s (lease - 1 s, floored), and 60 Hellos
    # admitted one at a time on a loaded machine overran it - the epoch-3
    # Hello was refused with UNAVAILABLE ("retry", D41), which is the
    # admission contract working, not a fencing outcome. The fencing
    # assertions below are unchanged.
    settings = make_settings(pg_settings, hello_timeout_s=5.0, **tolerant_liveness())
    async with (
        ControllerRuntime.start(settings) as runtime,
        NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel,
    ):
        await _twenty_nodes_race(channel, registry)


async def _twenty_nodes_race(channel: grpc.aio.Channel, registry: Registry) -> None:
    node_ids = [new_node_id() for _ in range(20)]
    all_agents: list[FakeAgent] = []
    try:

        async def race(node_id: str) -> list[FakeAgent]:
            agents_for_node = [
                FakeAgent(channel, node_id=node_id, epoch=e) for e in (1, 2, 3)
            ]
            all_agents.extend(agents_for_node)
            await asyncio.gather(*(a.hello() for a in agents_for_node))
            return agents_for_node

        results = await asyncio.gather(*(race(nid) for nid in node_ids))

        for group in results:
            winner = group[-1]  # epoch 3
            assert winner.session_id, "epoch 3 Hello must always be accepted"
            winner.start_reading()
            winner.start_heartbeats(0.05)

        # NOTE: only *accepted* Hellos open a ``node_session`` row - a Hello
        # arriving out of epoch order is rejected before any row is written,
        # so the total row count per node depends on arrival order (e.g. if
        # epoch 3 happens to arrive first, epochs 1 and 2 are rejected
        # outright and never get a row at all). The only order-independent
        # invariant is: exactly one *open* session per node, and it is
        # always epoch 3 (ADR-0002 Updates §1: higher always wins).
        async def all_settled() -> bool:
            for group in results:
                node_id = group[0].node_id
                sessions = await registry.list_sessions(node_id)
                if sum(1 for s in sessions if s.is_open) != 1:
                    return False
            return True

        await poll_until(all_settled, timeout_s=10.0)

        for group in results:
            node_id = group[0].node_id
            sessions = await registry.list_sessions(node_id)
            assert len(sessions) <= 3, sessions
            open_sessions = [s for s in sessions if s.is_open]
            assert len(open_sessions) == 1, (node_id, sessions)
            assert open_sessions[0].session_epoch == 3
            for s in sessions:
                if not s.is_open:
                    assert s.end_reason in (
                        SessionEndReason.SUPERSEDED,
                        SessionEndReason.AGENT_CLOSED,
                    ), (node_id, s)
            rec = await registry.get_node(node_id)
            assert rec is not None and rec.session_epoch == 3
    finally:
        for a in all_agents:
            await a.close()


# --------------------------------------------------------------------------
# Liveness edges on a real clock (FAST_LIVENESS: 0.2 s / +0.05 s / x3)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_just_under_suspect_threshold_never_flaps(
    agents: Any, registry: Registry
) -> None:
    """suspect_after_s = 0.25 s under the fixture's FAST_LIVENESS; a heartbeat
    every 0.2 s (comfortably under) must never observe SUSPECT.

    V0.7 G6 (D161 load flake): the heartbeats run on their own **absolute**
    0.2 s schedule in a separate task. The old loop slept 0.2 s *after* a
    database read, so every gap was 0.2 s + one query - under a loaded full
    component run that alone crossed 0.25 s and the node was rightly marked
    suspect (the test's cadence, not the server, was late)."""
    agent: FakeAgent = agents()
    await agent.connect()
    loop = asyncio.get_running_loop()
    start = loop.time()

    async def beat() -> None:
        tick = 0
        while True:
            await agent.heartbeat()
            tick += 1
            await asyncio.sleep(max(0.0, start + 0.2 * tick - loop.time()))

    beating = asyncio.create_task(beat())
    try:
        for _ in range(15):
            await asyncio.sleep(0.2)
            rec = await registry.get_node(agent.node_id)
            assert rec is not None and rec.state is NodeState.ONLINE, rec.state
            assert not beating.done()
    finally:
        beating.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beating


@pytest.mark.asyncio
async def test_persisted_state_matches_memory_after_each_transition(
    agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    await agent.connect()

    async def state_is(state: NodeState) -> bool:
        rec = await registry.get_node(agent.node_id)
        return rec is not None and rec.state is state

    await poll_until(lambda: state_is(NodeState.SUSPECT), timeout_s=2.0)
    await poll_until(lambda: state_is(NodeState.OFFLINE), timeout_s=2.0)
    # reconnect
    agent2: FakeAgent = FakeAgent(agent.channel, node_id=agent.node_id, epoch=2)
    await agent2.connect()
    rec = await registry.get_node(agent.node_id)
    assert rec is not None and rec.state is NodeState.ONLINE
    await agent2.close()


@pytest.mark.asyncio
async def test_five_reconnects_produce_five_sessions_with_correct_reasons(
    channel: grpc.aio.Channel, registry: Registry
) -> None:
    node_id = new_node_id()
    for epoch in range(1, 6):
        agent = FakeAgent(channel, node_id=node_id, epoch=epoch)
        await agent.connect()
        await agent.close(graceful=True)  # a clean stop each time (D19)
        await poll_until(
            lambda a=agent: _session_closed(registry, a.session_id), timeout_s=5.0
        )
    sessions = await registry.list_sessions(node_id)
    assert len(sessions) == 5
    for s in sessions:
        assert s.end_reason is SessionEndReason.AGENT_CLOSED
        assert not s.is_open
    rec = await registry.get_node(node_id)
    assert rec is not None and rec.session_epoch == 5


def test_migration_0002_upgrade_downgrade_upgrade_is_clean(
    pg_settings: DatabaseSettings,
) -> None:
    """0002 (node/node_session) must round-trip: head -> 0001 -> head, with the
    tables gone after the downgrade and back (matching the model) after the
    re-upgrade. Real Alembic against a real, empty database.

    Alembic's ``env.py`` runs its own ``asyncio.run`` (see
    ``store/migrations/env.py``), so - like every other Alembic test in this
    suite (``tests/component/store/test_store_migrations.py``) - this test is
    synchronous; only the table-inspection helper opens its own short-lived
    asyncio.run.
    """
    import asyncio as _asyncio

    from alembic import command
    from sqlalchemy import inspect as sa_inspect

    from meshforge.controller.store.migrate import alembic_config

    cfg = alembic_config(pg_settings)

    def table_names() -> set[str]:
        async def go() -> set[str]:
            engine = create_engine(pg_settings)
            try:
                async with engine.connect() as conn:
                    return await conn.run_sync(
                        lambda c: set(sa_inspect(c).get_table_names(schema="public"))
                    )
            finally:
                await engine.dispose()

        return _asyncio.run(go())

    command.upgrade(cfg, "head")
    after_up = table_names()
    assert {"node", "node_session"} <= after_up

    command.downgrade(cfg, "0001")
    after_down = table_names()
    assert "node" not in after_down
    assert "node_session" not in after_down

    command.upgrade(cfg, "head")
    after_reup = table_names()
    assert {"node", "node_session"} <= after_reup


async def _session_closed(registry: Registry, session_id: str) -> bool:
    rec = await registry.get_session(session_id)
    return rec is not None and not rec.is_open


# --------------------------------------------------------------------------
# Process / DB
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_end_reason_check_constraint_rejects_unknown_value_at_db_level(
    pg_settings: DatabaseSettings, runtime: ControllerRuntime, agents: Any
) -> None:
    """The application never writes an unknown ``end_reason``, but the CHECK
    constraint is the last line of defence: prove the database itself refuses
    one, independent of any Python-layer validation."""
    agent: FakeAgent = agents()
    await agent.connect()
    session_id = agent.session_id

    engine = create_engine(pg_settings)
    try:
        async with engine.begin() as conn:
            with pytest.raises(Exception) as excinfo:
                await conn.execute(
                    text(
                        "UPDATE node_session SET end_reason = "
                        "'not_a_real_reason' WHERE session_id = :sid"
                    ),
                    {"sid": session_id},
                )
            assert (
                "ck_node_session_end_reason" in str(excinfo.value)
                or "check constraint" in str(excinfo.value).lower()
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_two_controllers_on_the_same_db_second_startup_sweep_closes_first(
    pg_settings: DatabaseSettings, agents: Any
) -> None:
    """Documented behaviour, not exercised as a defect: a second controller
    process pointed at the same database sweeps every open session at startup
    (``sweep_open_sessions(CONTROLLER_RESTART)``), including sessions actually
    still held by a *different, live* first controller. There is currently no
    controller-instance identity to distinguish "my sessions" from "someone
    else's" - acceptable for a single-controller deployment (ARCHITECTURE §3),
    worth flagging if multi-controller ever becomes a real deployment shape."""
    # TOLERANT_LIVENESS: the agent never heartbeats, and starting the second
    # controller can take longer than FAST_LIVENESS's 0.6 s lease - the session
    # then ends lease_expired before the sweep (seen in a full component run).
    settings1 = make_settings(pg_settings, **tolerant_liveness())
    async with ControllerRuntime.start(settings1) as rt1:
        registry1 = rt1.registry
        assert registry1 is not None
        channel1 = NodeChannels(f"127.0.0.1:{rt1.grpc_port}")
        agent = FakeAgent(channel1)
        await agent.connect()
        session_id = agent.session_id
        rec = await registry1.get_session(session_id)
        assert rec is not None and rec.is_open

        settings2 = make_settings(pg_settings)
        async with ControllerRuntime.start(settings2) as rt2:
            registry2 = rt2.registry
            assert registry2 is not None
            rec_after = await registry2.get_session(session_id)
            assert rec_after is not None
            assert not rec_after.is_open
            assert rec_after.end_reason is SessionEndReason.CONTROLLER_RESTART

        await agent.close()
        await channel1.close()
