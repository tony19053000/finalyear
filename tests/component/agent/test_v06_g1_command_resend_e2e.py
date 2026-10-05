"""V0.6 G1 (D113) end to end: a lost Ack makes the controller re-send the
command, and the agent applies it exactly once.

Real controller (PostgreSQL, gRPC, REST) + a real agent (session client, GC
service). The agent's first Ack for a ``GcRequest`` is dropped on the way out
(its session writer skips it - what a lost Ack looks like to the controller);
after ``command_resend_s`` the controller sends the same envelope again; the
agent recognises the ``message_id``, does not run the request again, and acks.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_jobs_harness import token_of

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings, command_resend_s=1.0, **tolerant_liveness()
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


async def test_a_lost_ack_is_resent_and_applied_once(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
) -> None:
    agent = await ExecAgent.start(grpc_target, data_dir)
    try:
        assert agent.gc is not None
        handled: Counter[str] = Counter()
        gc_ids: set[str] = set()
        route = agent.client.command_handler
        assert route is not None

        def counting(msg: agent_pb2.ControllerMessage) -> None:
            if msg.WhichOneof("payload") == "gc" and msg.gc.run_now:
                handled[msg.message_id] += 1
                gc_ids.add(msg.message_id)
            route(msg)

        agent.client.command_handler = counting
        real_write = agent.client._write
        dropped: list[str] = []

        async def lossy(msg: agent_pb2.AgentMessage, *, last: bool = False) -> None:
            if (
                msg.HasField("ack")
                and msg.ack.acked_message_id in gc_ids
                and not dropped
            ):
                dropped.append(msg.ack.acked_message_id)  # lost on the way
                return
            await real_write(msg, last=last)

        agent.client._write = lossy  # type: ignore[method-assign]
        passes_before = agent.gc.passes
        got = await http.post(f"/v1/nodes/{agent.node_id}/gc")
        assert got.status_code == 202, got.text
        assert runtime.manager is not None
        live = runtime.manager.get(agent.node_id)
        assert live is not None
        deadline = asyncio.get_running_loop().time() + 20
        while not (dropped and dropped[0] not in live.awaiting_ack):
            assert asyncio.get_running_loop().time() < deadline, (
                dropped,
                dict(handled),
                live.commands_resent,
            )
            await asyncio.sleep(0.05)
        (lost,) = dropped
        assert live.commands_resent >= 1
        assert handled == Counter({lost: 1})  # applied once, despite the re-send
        assert agent.client.stats.duplicates >= 1
        # exactly one pass came from it (plus none from the duplicate)
        deadline = asyncio.get_running_loop().time() + 10
        while agent.gc.passes == passes_before:
            assert asyncio.get_running_loop().time() < deadline
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.3)
        assert agent.gc.passes - passes_before == 1
    finally:
        await agent.stop()
