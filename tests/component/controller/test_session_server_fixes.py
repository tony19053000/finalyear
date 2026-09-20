"""V0.3 G2 fix-round component tests (tester DEFECT-1, DEFECT-2 and the heartbeat
write-coalescing finding), against a real PostgreSQL and a real ``ControllerRuntime``.

Covers what the unit tests cannot: the ``uint64`` epoch really round-trips through
the ``NUMERIC(20, 0)`` column and its CHECK, a registry failure *during* admission
ends the stream with a typed ``INTERNAL`` status (the only monkeypatch here is on
``Registry.upsert_node_on_hello`` - the point is the servicer's handling, not the
registry), and epoch fencing holds across a real disconnect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import grpc
import pytest
import structlog

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from mf_session_client import FakeAgent, poll_until
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from meshforge.common.ids import new_node_id, new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, RegistryError
from meshforge.controller.session.epoch import MAX_SESSION_EPOCH
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.models.node import SessionEndReason

if TYPE_CHECKING:
    from meshforge.controller.server import ControllerRuntime
    from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

RejectCode = agent_pb2.HelloAck.RejectCode


async def wait_closed(registry: Registry, session_id: str) -> SessionEndReason | None:
    async def closed() -> bool:
        rec = await registry.get_session(session_id)
        return rec is not None and not rec.is_open

    await poll_until(closed, timeout_s=3.0)
    rec = await registry.get_session(session_id)
    assert rec is not None
    return rec.end_reason


# ------------------------------------------------------------ DEFECT-1 fencing


async def test_lower_or_equal_epoch_after_disconnect_is_stale_over_the_wire(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    winner: FakeAgent = agents(epoch=5)
    ack = await winner.connect()
    await winner.close()
    assert await wait_closed(registry, ack.session_id) is SessionEndReason.AGENT_CLOSED

    for epoch in (5, 4):
        stale: FakeAgent = agents(node_id=winner.node_id, epoch=epoch)
        reply = await stale.hello()
        assert reply is not None and reply.hello_ack.accepted is False, epoch
        assert reply.hello_ack.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH
        assert await stale.read() is None
        assert stale.end_code is grpc.StatusCode.OK

    rec = await registry.get_node(winner.node_id)
    assert rec is not None and rec.session_epoch == 5  # never lowered
    assert len(await registry.list_sessions(winner.node_id)) == 1

    fresh: FakeAgent = agents(node_id=winner.node_id, epoch=6)
    await fresh.connect()
    rec = await registry.get_node(winner.node_id)
    assert rec is not None and rec.session_epoch == 6 and rec.state is NodeState.ONLINE


async def test_registry_upsert_never_lowers_the_epoch(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    node_id = new_node_id()
    common: dict[str, Any] = {
        "hostname": "h",
        "os": "o",
        "platform": "p",
        "hardware": {},
        "state": NodeState.ONLINE,
    }
    await registry.upsert_node_on_hello(
        node_id=node_id, session_epoch=9, session_id=new_session_id(), **common
    )
    with structlog.testing.capture_logs() as logs:
        rec = await registry.upsert_node_on_hello(
            node_id=node_id, session_epoch=2, session_id=new_session_id(), **common
        )
    assert rec.session_epoch == 9
    stored = await registry.get_node(node_id)
    assert stored is not None and stored.session_epoch == 9
    assert [e["event"] for e in logs] == ["registry.epoch_not_lowered"]
    rec = await registry.upsert_node_on_hello(
        node_id=node_id, session_epoch=10, session_id=new_session_id(), **common
    )
    assert rec.session_epoch == 10


# ------------------------------------------------------- DEFECT-2 uint64 epoch


async def test_uint64_epoch_round_trips_through_node_and_session_rows(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents(epoch=MAX_SESSION_EPOCH)
    ack = await agent.connect()
    node = await registry.get_node(agent.node_id)
    session = await registry.get_session(ack.session_id)
    assert node is not None and node.session_epoch == 2**64 - 1
    assert isinstance(node.session_epoch, int)
    assert session is not None and session.session_epoch == 2**64 - 1
    # An agent at the top of the domain cannot reconnect (nothing is higher); that
    # is the protocol's rule, not a crash: the Hello is a clean STALE_EPOCH.
    again: FakeAgent = agents(node_id=agent.node_id, epoch=MAX_SESSION_EPOCH)
    reply = await again.hello()
    assert reply is not None and reply.hello_ack.accepted is False
    assert reply.hello_ack.reject_code == RejectCode.REJECT_CODE_STALE_EPOCH


async def test_database_refuses_epochs_outside_the_uint64_domain(
    runtime: ControllerRuntime, pg_settings: DatabaseSettings, registry: Registry
) -> None:
    node_id = new_node_id()
    await registry.upsert_node_on_hello(
        node_id=node_id,
        hostname="h",
        os="o",
        platform="p",
        hardware={},
        session_epoch=1,
        session_id=new_session_id(),
        state=NodeState.ONLINE,
    )
    engine = create_engine(pg_settings)
    try:
        for bad in (2**64, -1):
            async with engine.begin() as conn:
                with pytest.raises(IntegrityError, match="ck_node_session_epoch"):
                    await conn.execute(
                        text("UPDATE node SET session_epoch = :e WHERE node_id = :n"),
                        {"e": bad, "n": node_id},
                    )
    finally:
        await engine.dispose()
    rec = await registry.get_node(node_id)
    assert rec is not None and rec.session_epoch == 1


# ------------------------------------------- DEFECT-2 (b) admit failure is typed


async def test_registry_failure_during_admit_ends_the_stream_internal(
    runtime: ControllerRuntime,
    agents: Any,
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(self: Registry, **kwargs: Any) -> None:
        raise RegistryError(
            "registry.upsert_node_on_hello failed: ConnectionRefusedError",
            details={"operation": "upsert_node_on_hello", "cause": "OSError"},
        )

    monkeypatch.setattr(Registry, "upsert_node_on_hello", boom)
    victim: FakeAgent = agents()
    with structlog.testing.capture_logs() as logs:
        assert await victim.hello() is None
    assert victim.end_code is grpc.StatusCode.INTERNAL
    assert "RegistryError" in victim.end_details
    failed = [e for e in logs if e["event"] == "session.admit_failed"]
    assert len(failed) == 1
    assert failed[0]["node_id"] == victim.node_id
    assert failed[0]["cause"] == "RegistryError"
    assert failed[0]["operation"] == "upsert_node_on_hello"
    assert await registry.get_node(victim.node_id) is None
    assert runtime.manager is not None and len(runtime.manager) == 0

    # The server is intact: once the database is back the next Hello is admitted.
    monkeypatch.undo()
    survivor: FakeAgent = agents()
    await survivor.connect()
    assert len(runtime.manager) == 1


# ---------------------------------------- FINDING: coalesced last_seen_at writes


async def test_heartbeat_burst_keeps_the_lease_and_persists_last_seen_once_per_window(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    """2000 back-to-back heartbeats on the 0.2 s / 0.6 s liveness must neither
    expire the lease (one DB write per window, not per message) nor leave
    ``last_seen_at`` behind: the timer flushes the coalesced write."""
    agent: FakeAgent = agents()
    ack = await agent.connect()
    before = await registry.get_node(agent.node_id)
    assert before is not None

    for _ in range(2000):
        await agent.heartbeat()

    live = runtime.manager.get(agent.node_id) if runtime.manager else None
    assert live is not None and not live.ended, "burst must not expire the lease"
    assert live.heartbeats >= 1

    async def all_processed() -> bool:
        return live.heartbeats == 2000

    await poll_until(all_processed, timeout_s=3.0)
    assert live.state is NodeState.ONLINE

    async def flushed() -> bool:
        rec = await registry.get_node(agent.node_id)
        return rec is not None and rec.last_seen_at > before.last_seen_at

    await poll_until(flushed, timeout_s=2.0)
    rec = await registry.get_session(ack.session_id)
    assert rec is not None and rec.is_open
