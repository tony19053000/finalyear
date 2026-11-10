"""V1.0 G9-B item 4: ``placement.exclude_nodes`` (``compute run --exclude-node``)
over a real controller (PostgreSQL, mTLS gRPC sessions, HTTPS REST, the real
dispatcher + planner) and hand-driven ``FakeAgent`` sessions.

* the job never runs on an excluded node: first placement, the retry after its
  node is lost, and after a **controller restart** (the list is stored in the
  job's ``placement``) - the decision names it ``excluded_by_user``;
* naming the pinned node is ``422``; more than 16 or a non-canonical id is
  ``400``; excluding every node that could run it is ``409 no_feasible_plan``
  with ``excluded_by_user`` rejections and no job row;
* the stored placement reads back sorted and unique; a job without it keeps
  the old placement view (no key).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from mf_fabric import NodeChannels, client_ssl_context
from mf_jobs_harness import HARNESS_NODE_ID, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.hashing import sha256_bytes
from meshforge.common.ids import new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(180),
]

BUNDLE = os.urandom(2500)
GIB = 1024**3
QUIET_S = 1.5


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


def _http(rt: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{rt.http_port}",
        headers={"authorization": f"Bearer {token_of(rt)}"},
        timeout=20,
        verify=client_ssl_context(node_id=HARNESS_NODE_ID),
    )


def _starts(agent: FakeAgent, job_id: str) -> list[Any]:
    return [
        m for m in agent.messages("start_attempt") if m.start_attempt.job_id == job_id
    ]


def _body(digest: str, **placement: Any) -> dict[str, Any]:
    return {
        "manifest": {
            "name": "x",
            "entrypoint": "python main.py",
            "retry": {"max_attempts": 5, "backoff_s": 0, "on_lost": True},
        },
        "bundle_digest": digest,
        "placement": placement,
    }


async def _codes(http: httpx.AsyncClient, job_id: str, node_id: str) -> set[str]:
    decisions = (await http.get(f"/v1/jobs/{job_id}/decisions")).json()
    return {
        c.get("reject_code")
        for d in decisions["decisions"]
        for c in d["candidates"]
        if c.get("node_id") == node_id
    }


async def test_excluded_node_is_never_used_across_retry_and_restart(
    pg_settings: DatabaseSettings,
) -> None:
    settings = g2_settings(pg_settings, jobs={"dispatch_interval_s": 0.05})
    a_id, b_id = str(new_node_id()), str(new_node_id())
    digest = str(sha256_bytes(BUNDLE))
    agents: list[FakeAgent] = []

    async def connect(channel: Any, node_id: str, epoch: int) -> FakeAgent:
        agent = FakeAgent(channel, node_id=node_id, epoch=epoch)
        agents.append(agent)
        await agent.connect(hardware=_hardware())
        agent.start_heartbeats(0.3)
        return agent

    async def close_all() -> None:
        for agent in agents:
            agent.stop_heartbeats()
            await agent.close()
        agents.clear()

    async with (
        running_controller(settings) as rt,
        NodeChannels(f"127.0.0.1:{rt.grpc_port}") as channel,
        _http(rt) as http,
    ):
        try:
            put = await http.put(f"/v1/blobs/{digest[7:]}", content=BUNDLE)
            assert put.status_code == 201, put.text
            a = await connect(channel, a_id, 1)
            b = await connect(channel, b_id, 1)
            created = await http.post(
                "/v1/jobs", json=_body(digest, exclude_nodes=[a_id, a_id])
            )
            assert created.status_code == 201, created.text
            job_id = created.json()["job_id"]
            job = (await http.get(f"/v1/jobs/{job_id}")).json()
            assert job["placement"]["exclude_nodes"] == [a_id]

            async def on_b() -> bool:
                return bool(_starts(b, job_id))

            await _until(on_b)
            assert _starts(a, job_id) == []
            assert "excluded_by_user" in await _codes(http, job_id, a_id)

            # B is lost -> the retry must wait rather than use A
            await b.close()
            agents.remove(b)

            async def lost() -> bool:
                got = (await http.get(f"/v1/jobs/{job_id}")).json()
                return got["attempts"][0]["state"] == "lost"

            await _until(lost, timeout_s=40)
            await asyncio.sleep(QUIET_S)
            assert _starts(a, job_id) == []
            state = (await http.get(f"/v1/jobs/{job_id}")).json()["state"]
            assert state in ("queued", "retrying", "planning"), state
        finally:
            await close_all()

    # a restarted controller reads the list from the job's placement
    async with (
        running_controller(settings) as rt,
        NodeChannels(f"127.0.0.1:{rt.grpc_port}") as channel,
        _http(rt) as http,
    ):
        try:
            a = await connect(channel, a_id, 2)
            await asyncio.sleep(QUIET_S)
            assert _starts(a, job_id) == []
            b = await connect(channel, b_id, 2)

            async def back_on_b() -> bool:
                return bool(_starts(b, job_id))

            await _until(back_on_b, timeout_s=40)
            assert _starts(a, job_id) == []
        finally:
            await close_all()


async def test_submit_bounds(pg_settings: DatabaseSettings) -> None:
    settings = g2_settings(pg_settings, jobs={"dispatch_interval_s": 60.0})
    digest = str(sha256_bytes(BUNDLE))
    async with (
        running_controller(settings) as rt,
        NodeChannels(f"127.0.0.1:{rt.grpc_port}") as channel,
        _http(rt) as http,
    ):
        only = FakeAgent(channel)
        try:
            await only.connect(hardware=_hardware())
            put = await http.put(f"/v1/blobs/{digest[7:]}", content=BUNDLE)
            assert put.status_code == 201
            pinned = await http.post(
                "/v1/jobs",
                json=_body(digest, node_id=only.node_id, exclude_nodes=[only.node_id]),
            )
            assert pinned.status_code == 422, pinned.text
            error = pinned.json()["error"]
            assert error["code"] == "invalid_input"
            assert error["reason"] == "pinned_node_excluded"
            too_many = await http.post(
                "/v1/jobs",
                json=_body(
                    digest, exclude_nodes=[str(new_node_id()) for _ in range(17)]
                ),
            )
            assert too_many.status_code == 400, too_many.text
            bad = await http.post("/v1/jobs", json=_body(digest, exclude_nodes=["L1"]))
            assert bad.status_code == 400, bad.text
            everything = await http.post(
                "/v1/jobs", json=_body(digest, exclude_nodes=[only.node_id])
            )
            assert everything.status_code == 409, everything.text
            (rejection,) = everything.json()["error"]["rejections"]
            assert rejection["reject_code"] == "excluded_by_user"
            assert rt.jobs is not None
            assert await rt.jobs.list_jobs(limit=10) == []
            plain = await http.post("/v1/jobs", json=_body(digest))
            assert plain.status_code == 201
            view = (await http.get(f"/v1/jobs/{plain.json()['job_id']}")).json()
            assert "exclude_nodes" not in view["placement"]
        finally:
            await only.close()
