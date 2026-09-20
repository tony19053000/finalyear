"""V0.3 G2 review round 2 component tests (B1, B3, NB1, NB6) against a real
PostgreSQL and a real ``ControllerRuntime``.

The only monkeypatches are on ``Registry`` methods (to make the database *slow*,
which a container cannot be told to be on demand) - the point is the session
layer's behaviour with a stalled store, not the store itself.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import grpc
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from mf_session_client import FakeAgent, poll_until

from meshforge.common.ids import new_command_id, new_node_id, new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.session.limits import (
    HOSTNAME_MAX_LENGTH,
    OS_MAX_LENGTH,
    PLATFORM_MAX_LENGTH,
)
from meshforge.controller.store.models.node import SessionEndReason

if TYPE_CHECKING:
    from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

STALL_S = 2.0  # > the 0.6 s lease of the fixture's FAST_LIVENESS


# ------------------------------------------------ B1: a stalled database write


async def test_stalled_touch_node_never_costs_a_healthy_node_its_lease(
    runtime: ControllerRuntime,
    agents: Any,
    registry: Registry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reviewer's reproduction: ``touch_node`` takes 2 s (lease 0.6 s) while
    the agent heartbeats every 0.1 s. Before: 1 of 15 heartbeats handled and the
    session closed ``lease_expired``. Now: every heartbeat is handled at once,
    the node stays ONLINE, and a second node's lease is still evaluated."""
    real_touch = Registry.touch_node
    stalled: list[str] = []

    async def slow_touch(self: Registry, node_id: str) -> None:
        stalled.append(node_id)
        await asyncio.sleep(STALL_S)
        await real_touch(self, node_id)

    monkeypatch.setattr(Registry, "touch_node", slow_touch)

    healthy: FakeAgent = agents()
    await healthy.connect()
    silent: FakeAgent = agents()  # never heartbeats: its lease must still expire
    silent_ack = await silent.connect()
    assert runtime.manager is not None
    live = runtime.manager.get(healthy.node_id)
    assert live is not None

    for _ in range(15):
        await healthy.heartbeat()
        await asyncio.sleep(0.1)  # 1.5 s total: 2.5 leases, inside one stall

    async def all_handled() -> bool:
        return live.heartbeats == 15

    await poll_until(all_handled, timeout_s=1.0)
    assert not live.ended and live.state is NodeState.ONLINE
    assert not healthy.ended.is_set()
    assert stalled == [healthy.node_id]  # one coalesced write, still in flight
    rec = await registry.get_node(healthy.node_id)
    assert rec is not None and rec.state is NodeState.ONLINE

    # The other node walked SUSPECT -> OFFLINE on time, in memory and on disk.
    await silent.wait_ended(timeout_s=2.0)
    assert silent.end_code is grpc.StatusCode.ABORTED
    assert silent.end_details == SessionEndReason.LEASE_EXPIRED.value

    async def silent_offline() -> bool:
        rec = await registry.get_node(silent.node_id)
        return rec is not None and rec.state is NodeState.OFFLINE

    await poll_until(silent_offline, timeout_s=2.0)
    closed = await registry.get_session(silent_ack.session_id)
    assert closed is not None and closed.end_reason is SessionEndReason.LEASE_EXPIRED

    # Once the stall passes the coalesced write lands and later ones follow.
    async def touched() -> bool:
        rec = await registry.get_node(healthy.node_id)
        return rec is not None and len(stalled) >= 2

    healthy.start_heartbeats(0.1)
    await poll_until(touched, timeout_s=STALL_S + 2.0)
    assert not live.ended and live.state is NodeState.ONLINE


# ---------------------------------------------------- B3: over-long Hello strings


@pytest.mark.parametrize(
    ("field", "limit"),
    [
        ("hostname", HOSTNAME_MAX_LENGTH),
        ("os", OS_MAX_LENGTH),
        ("platform", PLATFORM_MAX_LENGTH),
    ],
)
async def test_over_long_hello_string_is_invalid_argument_and_nothing_persisted(
    runtime: ControllerRuntime,
    agents: Any,
    registry: Registry,
    field: str,
    limit: int,
) -> None:
    agent: FakeAgent = agents()
    assert await agent.hello(**{field: "Z" * (limit + 1)}) is None
    assert agent.end_code is grpc.StatusCode.INVALID_ARGUMENT
    assert agent.end_details.startswith(f"Hello.{field} is {limit + 1} characters")
    assert "ZZZ" not in agent.end_details
    assert await registry.get_node(agent.node_id) is None
    assert await registry.list_sessions(agent.node_id) == []
    assert runtime.manager is not None and len(runtime.manager) == 0

    # Exactly at the cap is stored intact (no truncation, no INTERNAL).
    ok: FakeAgent = agents()
    await ok.connect(**{field: "Y" * limit})
    rec = await registry.get_node(ok.node_id)
    assert rec is not None and getattr(rec, field) == "Y" * limit


async def test_malformed_message_id_over_the_wire_is_protocol_error_aborted(
    runtime: ControllerRuntime, agents: Any, registry: Registry
) -> None:
    agent: FakeAgent = agents()
    ack = await agent.connect()
    await agent.write(
        agent_pb2.AgentMessage(
            message_id="M" * 4096,
            session_id=ack.session_id,
            heartbeat=agent_pb2.Heartbeat(sequence=1),
        )
    )
    await agent.wait_ended()
    assert agent.end_code is grpc.StatusCode.ABORTED
    assert agent.end_details == SessionEndReason.PROTOCOL_ERROR.value

    async def closed() -> bool:
        rec = await registry.get_session(ack.session_id)
        return rec is not None and not rec.is_open

    await poll_until(closed, timeout_s=3.0)
    rec = await registry.get_session(ack.session_id)
    assert rec is not None and rec.end_reason is SessionEndReason.PROTOCOL_ERROR
    live = runtime.manager.get(agent.node_id) if runtime.manager else None
    assert live is None

    # A well-formed id from the same node (next epoch) is admitted normally.
    again: FakeAgent = agents(node_id=agent.node_id, epoch=2)
    await again.connect()
    await again.heartbeat()
    good = agent_pb2.AgentMessage(
        message_id=new_command_id(),
        session_id=again.session_id,
        heartbeat=agent_pb2.Heartbeat(sequence=2),
    )
    await again.write(good)
    await asyncio.sleep(0.1)
    assert not again.ended.is_set()


# ---------------------------------------- NB6: GREATEST clamp inside the UPDATE


async def test_concurrent_upserts_keep_the_highest_epoch_without_any_lock(
    runtime: ControllerRuntime, registry: Registry
) -> None:
    """Twenty concurrent ``upsert_node_on_hello`` calls with shuffled epochs and no
    session-manager lock in front of them: the stored epoch is the maximum, so
    the clamp is the statement's, not a Python read-modify-write."""
    node_id = new_node_id()
    common: dict[str, Any] = {
        "hostname": "h",
        "os": "o",
        "platform": "p",
        "hardware": {},
        "state": NodeState.ONLINE,
    }
    await registry.upsert_node_on_hello(
        node_id=node_id, session_epoch=1, session_id=new_session_id(), **common
    )
    epochs = [7, 3, 20, 11, 2, 19, 5, 13, 8, 17, 4, 15, 6, 12, 9, 18, 10, 16, 14, 1]
    results = await asyncio.gather(
        *(
            registry.upsert_node_on_hello(
                node_id=node_id,
                session_epoch=e,
                session_id=new_session_id(),
                **common,
            )
            for e in epochs
        )
    )
    assert all(r.session_epoch >= 1 for r in results)
    stored = await registry.get_node(node_id)
    assert stored is not None and stored.session_epoch == 20
    # and every returned record reflects a value that was really stored at the
    # time (monotone non-decreasing is all concurrency guarantees)
    assert max(r.session_epoch for r in results) == 20
