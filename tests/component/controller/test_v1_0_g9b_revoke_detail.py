"""V1.0 G9-B item 1 (lab bug #6): revoking a node with a running attempt.

A real controller (PostgreSQL, mTLS gRPC sessions, HTTPS REST, the real
dispatcher) and hand-driven ``FakeAgent`` sessions. The victim reports its
attempt RUNNING; ``POST /v1/nodes/{id}/revoke`` then cuts its session and the
attempt ends ``LOST``. Through the REST API (what ``compute jobs JOB_ID``
prints) the attempt must say why: ``failure_type = node_lost``, ``detail =
node_revoked``; the job is retried on the other node, and the log follower's
retry frame carries the same detail (what ``compute run -f`` prints).
"""

from __future__ import annotations

import asyncio
import json
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

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_command_id
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(120),
]

BUNDLE = os.urandom(3000)
GIB = 1024**3


async def _until(check: Callable[[], Awaitable[Any]], timeout_s: float = 20.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


def _hardware() -> agent_pb2.HardwareSummary:
    return agent_pb2.HardwareSummary(
        cpu_model="t",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * GIB,
        capabilities=["exec", "exec-subprocess"],
    )


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, jobs={"dispatch_interval_s": 0.05})
    async with running_controller(settings) as rt:
        yield rt


async def _retry_frame(url: str, token: str) -> dict[str, Any]:
    from websockets.asyncio.client import connect

    async with connect(
        url,
        additional_headers={"Authorization": f"Bearer {token}"},
        ssl=client_ssl_context(node_id=HARNESS_NODE_ID),
    ) as ws:
        async for raw in ws:
            frame = json.loads(raw)
            if frame.get("retry"):
                return dict(frame)
            assert not frame.get("end"), frame
    raise AssertionError("closed without a retry frame")


async def test_revoking_a_node_with_a_running_attempt_names_node_revoked(
    runtime: ControllerRuntime,
) -> None:
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
        try:
            digest = sha256_bytes(BUNDLE)
            put = await http.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
            assert put.status_code == 201, put.text
            victim = FakeAgent(channel)
            agents.append(victim)
            await victim.connect(hardware=_hardware())
            victim.start_heartbeats(0.3)
            created = await http.post(
                "/v1/jobs",
                json={
                    "manifest": {
                        "name": "revoke-me",
                        "entrypoint": "python main.py",
                        "retry": {"max_attempts": 3, "backoff_s": 0, "on_lost": True},
                    },
                    "bundle_digest": str(digest),
                },
            )
            assert created.status_code == 201, created.text
            job_id = created.json()["job_id"]

            async def started() -> agent_pb2.ControllerMessage | None:
                found = victim.messages("start_attempt")
                return found[0] if found else None

            start_msg = await _until(started)
            start = start_msg.start_attempt
            await victim.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    session_id=victim.session_id,
                    ack=agent_pb2.Ack(acked_message_id=start_msg.message_id),
                )
            )
            await victim.write(
                agent_pb2.AgentMessage(
                    message_id=new_command_id(),
                    session_id=victim.session_id,
                    attempt_status=agent_pb2.AttemptStatus(
                        job_id=start.job_id,
                        attempt_id=start.attempt_id,
                        fencing_token=start.fencing_token,
                        state=common_pb2.ATTEMPT_STATE_RUNNING,
                    ),
                )
            )

            async def running() -> bool:
                job = (await http.get(f"/v1/jobs/{job_id}")).json()
                return job["state"] == "running"

            await _until(running)
            follower = asyncio.create_task(
                _retry_frame(
                    f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}"
                    "/logs/ws?stream=stdout",
                    token_of(runtime),
                )
            )
            try:
                spare = FakeAgent(channel)
                agents.append(spare)
                await spare.connect(hardware=_hardware())
                spare.start_heartbeats(0.3)

                revoked = await http.post(f"/v1/nodes/{victim.node_id}/revoke")
                assert revoked.status_code == 200, revoked.text

                async def lost() -> dict[str, Any] | None:
                    job = (await http.get(f"/v1/jobs/{job_id}")).json()
                    first = next(
                        a
                        for a in job["attempts"]
                        if a["attempt_id"] == start.attempt_id
                    )
                    return job if first["state"] == "lost" else None

                job = await _until(lost)
                first = next(
                    a for a in job["attempts"] if a["attempt_id"] == start.attempt_id
                )
                assert first["failure_type"] == "node_lost", first
                assert first["detail"] == "node_revoked", first
                assert first["node_id"] == victim.node_id

                frame = await asyncio.wait_for(follower, 30)
            finally:
                if not follower.done():
                    follower.cancel()
                    await asyncio.gather(follower, return_exceptions=True)
            assert frame["previous_attempt_id"] == start.attempt_id
            assert frame["previous_failure_type"] == "node_lost"
            assert frame["previous_detail"] == "node_revoked"
            # V1.0 G9-D: the roles ride along (a whole-job retry: run -> run)
            assert (frame["previous_role"], frame["role"]) == ("run", "run")

            async def retried_on_spare() -> bool:
                return bool(spare.messages("start_attempt"))

            await _until(retried_on_spare)
        finally:
            for agent in agents:
                agent.stop_heartbeats()
                await agent.close()
