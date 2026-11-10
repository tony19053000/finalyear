"""V1.0 G9-B item 3 (D433, lab bug #7) over real sessions.

A real controller (PostgreSQL, mTLS gRPC, HTTPS REST, the real dispatcher and
planner) and one hand-driven agent advertising ``env-policy:<A>``:

1. job 1's attempt ends ``env_error`` -> ``(node, env)`` is known-bad, recorded
   under fingerprint A;
2. job 2 (same environment) is not placed there - also after the agent
   reconnects with the **same** fingerprint (a reconnect is not a policy change);
3. the owner changes ``[agent.envs]`` (here: the agent reconnects advertising
   fingerprint B) -> the entry is dropped and job 2 is placed on the node.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels, client_ssl_context
from mf_jobs_harness import HARNESS_NODE_ID, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.capabilities import env_policy_tag
from meshforge.common.errors import FailureType
from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import failure_type_to_proto
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(120),
]

BUNDLE = os.urandom(2000)
GIB = 1024**3
A, B = "a" * 12, "b" * 12
QUIET_S = 1.5
"""How long job 2 must stay unplaced (30 dispatch ticks of 50 ms)."""


async def _until(check: Callable[[], Awaitable[Any]], timeout_s: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def _hardware(policy: str) -> agent_pb2.HardwareSummary:
    return agent_pb2.HardwareSummary(
        cpu_model="t",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * GIB,
        capabilities=["exec", "exec-subprocess", env_policy_tag(policy)],
    )


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, jobs={"dispatch_interval_s": 0.05})
    async with running_controller(settings) as rt:
        yield rt


async def test_a_policy_change_across_hello_clears_the_known_bad_env(
    runtime: ControllerRuntime,
) -> None:
    node_id = str(new_node_id())
    agents: list[FakeAgent] = []
    async with (
        NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel,
        httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{runtime.http_port}",
            headers={"authorization": f"Bearer {token_of(runtime)}"},
            timeout=20,
            verify=client_ssl_context(node_id=HARNESS_NODE_ID),
        ) as http,
    ):

        async def connect(policy: str, epoch: int) -> FakeAgent:
            agent = FakeAgent(channel, node_id=node_id, epoch=epoch)
            agents.append(agent)
            await agent.connect(hardware=_hardware(policy))
            agent.start_heartbeats(0.3)
            return agent

        async def submit(name: str) -> str:
            created = await http.post(
                "/v1/jobs",
                json={
                    "manifest": {
                        "name": name,
                        "entrypoint": "python main.py",
                        "retry": {"max_attempts": 1},
                    },
                    "bundle_digest": str(digest),
                },
            )
            assert created.status_code == 201, created.text
            return str(created.json()["job_id"])

        def starts(agent: FakeAgent, job_id: str) -> list[Any]:
            return [
                m
                for m in agent.messages("start_attempt")
                if m.start_attempt.job_id == job_id
            ]

        try:
            digest = sha256_bytes(BUNDLE)
            put = await http.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
            assert put.status_code == 201, put.text
            first = await connect(A, 1)
            job1 = await submit("one")

            async def started1() -> Any:
                found = starts(first, job1)
                return found[0] if found else None

            msg = await _until(started1)
            start = msg.start_attempt
            await first.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    session_id=first.session_id,
                    ack=agent_pb2.Ack(acked_message_id=msg.message_id),
                )
            )
            await first.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    session_id=first.session_id,
                    attempt_status=agent_pb2.AttemptStatus(
                        job_id=start.job_id,
                        attempt_id=start.attempt_id,
                        fencing_token=start.fencing_token,
                        state=common_pb2.ATTEMPT_STATE_FAILED,
                        failure=common_pb2.Failure(
                            failure_type=failure_type_to_proto(FailureType.ENV_ERROR),
                            message="setup_failed: no wheel for sdist-only-pkg",
                        ),
                    ),
                )
            )

            async def failed1() -> bool:
                job = (await http.get(f"/v1/jobs/{job1}")).json()
                return job["state"] == "failed"

            await _until(failed1)
            bad = runtime.known_bad_envs
            assert bad is not None
            (entry,) = bad.for_node(node_id)
            assert entry.env_policy == A

            job2 = await submit("two")
            await asyncio.sleep(QUIET_S)
            assert starts(first, job2) == []

            # a reconnect with the same policy changes nothing
            await first.close(graceful=True)
            same = await connect(A, 2)
            await asyncio.sleep(QUIET_S)
            assert starts(same, job2) == []
            assert len(bad.for_node(node_id)) == 1
            decisions = (await http.get(f"/v1/jobs/{job2}/decisions")).json()
            codes = {
                c.get("reject_code")
                for d in decisions["decisions"]
                for c in d["candidates"]
            }
            assert "env_known_bad" in codes, decisions

            # the owner changed [agent.envs]: another fingerprint at the next Hello
            await same.close(graceful=True)
            changed = await connect(B, 3)

            async def started2() -> bool:
                return bool(starts(changed, job2))

            await _until(started2)
            assert bad.for_node(node_id) == []
            assert bad.cleared == 1
        finally:
            for agent in agents:
                agent.stop_heartbeats()
                await agent.close()
