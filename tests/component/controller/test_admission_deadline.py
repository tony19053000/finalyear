"""V0.4 G1 (D41), real controller + real PostgreSQL + real gRPC: a registry that
hangs during admission gets the Hello refused UNAVAILABLE within
``admission_timeout_s`` (+ margin), and a node whose previous session ended
meanwhile is not left ONLINE.

The hang is injected on the running controller's own ``Registry`` instance:
``get_node`` for one node is held on an Event (never released while held - the
database "does not answer"); every other call goes to PostgreSQL. The real
agent's backoff-and-succeed half is ``tests/component/agent``'s
``test_agent_admission_deadline.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from typing import Any

import grpc
import pytest
import structlog
from mf_session_client import FakeAgent, poll_until

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.component

MARGIN_S = 1.0
"""Loopback round trip + scheduling on a loaded machine; the deadline is 0.5 s."""


class HungGetNode:
    """Hold ``get_node`` for ``node_id`` while ``holding``; pass everything else."""

    def __init__(self, registry: Registry, node_id: str) -> None:
        self._inner = registry.get_node
        self._registry = registry
        self._node_id = node_id
        self.holding = True
        self.held = asyncio.Event()
        self.calls = 0
        registry.get_node = self  # type: ignore[method-assign]

    async def __call__(self, node_id: str) -> Any:
        if node_id == self._node_id and self.holding:
            self.calls += 1
            self.held.set()
            await asyncio.Event().wait()  # a database that never answers
        return await self._inner(node_id)

    def restore(self) -> None:
        del self._registry.get_node


@contextlib.asynccontextmanager
async def own_channel(target: str) -> AsyncIterator[grpc.aio.Channel]:
    async with grpc.aio.insecure_channel(target) as ch:
        yield ch


def deadline_of(runtime: ControllerRuntime) -> float:
    return runtime.settings.controller.admission_timeout_s


@pytest.mark.asyncio
async def test_a_hung_registry_refuses_the_hello_unavailable_and_the_goodbye_is_offline(
    runtime: ControllerRuntime, grpc_target: str, registry: Registry
) -> None:
    assert runtime.manager is not None
    async with own_channel(grpc_target) as ch:
        old = FakeAgent(ch, epoch=1)
        old_ack = await old.connect()
        hang = HungGetNode(runtime.manager._registry, old.node_id)
        try:
            new = FakeAgent(ch, node_id=old.node_id, epoch=2)
            with structlog.testing.capture_logs() as logs:
                started = time.monotonic()
                reply_task = asyncio.create_task(new.hello())
                await asyncio.wait_for(hang.held.wait(), 5.0)
                # The agent restarts while its new Hello is stuck: Goodbye on the
                # old stream. D28 defers its end to the pending Hello...
                await old.goodbye()
                reply = await asyncio.wait_for(reply_task, deadline_of(runtime) + 5.0)
                took = time.monotonic() - started
            assert reply is not None and reply.WhichOneof("payload") == "hello_ack"
            assert not reply.hello_ack.accepted
            assert (
                reply.hello_ack.reject_code
                == agent_pb2.HelloAck.REJECT_CODE_UNAVAILABLE
            )
            assert took <= deadline_of(runtime) + MARGIN_S, took
            assert [e for e in logs if e["event"] == "session.admission_timeout"]

            # ...and when that Hello fails, the Goodbye's OFFLINE is written after
            # all: the node is NOT left ONLINE.
            async def offline() -> bool:
                rec = await registry.get_node(old.node_id)
                return rec is not None and rec.state is NodeState.OFFLINE

            await poll_until(offline, timeout_s=5.0, interval_s=0.01)
            rec = await registry.get_session(old_ack.session_id)
            assert rec is not None and rec.end_reason is SessionEndReason.AGENT_CLOSED
            assert runtime.manager._pending_hellos == {}  # the claim was released
            await new.close()
        finally:
            hang.restore()

        # The database answers again: the next Hello of the same node is admitted.
        retry = FakeAgent(ch, node_id=old.node_id, epoch=3)
        await retry.connect()

        async def online() -> bool:
            rec = await registry.get_node(old.node_id)
            return rec is not None and rec.state is NodeState.ONLINE

        await poll_until(online, timeout_s=5.0, interval_s=0.01)
        await retry.close(graceful=True)
        await old.close()
