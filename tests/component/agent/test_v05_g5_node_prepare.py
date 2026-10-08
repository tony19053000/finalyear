"""V0.5 G5 (0.5.8 controller part): ``compute node prepare`` over real PostgreSQL
+ gRPC + REST + agent. The env is built by the real G2 builder (subprocess
runtime: the agent's interpreter + a cached ``--target`` dir) - a second request
finds it (``hit``); bad requests and unknown/offline nodes are refused typed.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_exec_harness import ExecAgent
from mf_jobs_harness import token_of

from meshforge.agent.envs.builder import default_uv_argv
from meshforge.common.ids import new_node_id
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(pg_settings, jobs={"dispatch_interval_s": 0.1})


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


async def _done(
    http: httpx.AsyncClient, node_id: str, request_id: str, timeout_s: float = 180
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while True:
        got = await http.get(f"/v1/nodes/{node_id}/prepare/{request_id}")
        assert got.status_code == 200, got.text
        body = dict(got.json())
        if body["done"]:
            return body
        assert time.monotonic() < deadline, body
        await asyncio.sleep(0.1)


@pytest.mark.skipif(
    not default_uv_argv(), reason="the uv wheel (agent extra) is absent"
)
async def test_prepare_builds_once_then_reports_hit(
    runtime: ControllerRuntime, http: httpx.AsyncClient, agent: ExecAgent
) -> None:
    body = {
        "images": [],
        "envs": [{"base": "python-cpu", "requirements": "", "runtime": "subprocess"}],
    }
    first = await http.post(f"/v1/nodes/{agent.node_id}/prepare", json=body)
    assert first.status_code == 202, first.text
    done = await _done(http, agent.node_id, first.json()["request_id"])
    (item,) = done["items"]
    assert done["ok"] is True and item["state"] == "built", done
    assert len(item["key"]) == 64  # the env hash
    again = await http.post(f"/v1/nodes/{agent.node_id}/prepare", json=body)
    done2 = await _done(http, agent.node_id, again.json()["request_id"])
    (item2,) = done2["items"]
    assert item2["state"] == "hit" and item2["key"] == item["key"], done2


async def test_typed_refusals(
    runtime: ControllerRuntime, http: httpx.AsyncClient, agent: ExecAgent
) -> None:
    offline = await http.post(f"/v1/nodes/{new_node_id()}/prepare", json={})
    assert offline.status_code == 409, offline.text
    bad_base = await http.post(
        f"/v1/nodes/{agent.node_id}/prepare", json={"images": ["windows-gpu"]}
    )
    assert bad_base.status_code == 400
    too_many = await http.post(
        f"/v1/nodes/{agent.node_id}/prepare",
        json={"envs": [{"base": "python-cpu", "requirements": ""}] * 33},
    )
    assert too_many.status_code == 400
    max_ok = await http.post(
        f"/v1/nodes/{agent.node_id}/prepare",
        json={"images": [], "envs": [{"base": "python-cpu", "runtime": "docker"}]},
    )
    assert max_ok.status_code == 202, max_ok.text
    done = await _done(http, agent.node_id, max_ok.json()["request_id"])
    (item,) = done["items"]
    # This agent offers no Docker runtime (harness): a typed refusal, not a hang.
    assert item["state"] == "failed" and item["reason"] == "runtime_unavailable"
    assert item["failure_type"] == "no_feasible_plan" and done["ok"] is False
    rejected = await http.post(
        f"/v1/nodes/{agent.node_id}/prepare",
        json={
            "images": [],
            "envs": [
                {
                    "base": "python-cpu",
                    "requirements": "-e ./local\n",
                    "runtime": "subprocess",
                }
            ],
        },
    )
    done = await _done(http, agent.node_id, rejected.json()["request_id"])
    assert done["items"][0]["reason"] == "requirements_rejected", done
    unknown = await http.get(f"/v1/nodes/{agent.node_id}/prepare/nope")
    assert unknown.status_code == 404
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}"
    ) as plain:
        anonymous = await plain.post(f"/v1/nodes/{agent.node_id}/prepare", json={})
    assert anonymous.status_code == 401


async def test_a_gc_request_from_the_controller_runs_a_pass_now(
    runtime: ControllerRuntime, http: httpx.AsyncClient, agent: ExecAgent
) -> None:
    assert agent.gc is not None
    await agent.gc.wait_idle()
    before = agent.gc.passes
    asked = await http.post(f"/v1/nodes/{agent.node_id}/gc")
    assert asked.status_code == 202, asked.text
    deadline = time.monotonic() + 10
    while agent.gc.passes <= before:
        assert time.monotonic() < deadline, "no GC pass after GcRequest{run_now}"
        await asyncio.sleep(0.05)
    assert agent.gc.last is not None and "controller" in agent.gc.last.reason
    offline = await http.post(f"/v1/nodes/{new_node_id()}/gc")
    assert offline.status_code == 409


async def test_a_node_lost_mid_prepare_ends_the_request_node_lost(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    grpc_target: str,
    data_dir: Path,
) -> None:
    """Fix round 1 (N2): the agent accepts the request, then goes away (a clean
    stop: Goodbye -> OFFLINE) before finishing - the request ends failed
    ``node_lost``, never pending forever."""
    running = await ExecAgent.start(grpc_target, data_dir / "lost")
    stopped = False
    try:
        assert running.preparer is not None
        received = asyncio.Event()
        never = asyncio.Event()

        async def stuck(request: Any) -> None:
            received.set()
            await never.wait()  # the work never finishes on this agent

        running.preparer._run = stuck  # type: ignore[method-assign]
        posted = await http.post(
            f"/v1/nodes/{running.node_id}/prepare", json={"images": ["python-cpu"]}
        )
        assert posted.status_code == 202, posted.text
        await asyncio.wait_for(received.wait(), 10)
        request_id = posted.json()["request_id"]
        status = await http.get(f"/v1/nodes/{running.node_id}/prepare/{request_id}")
        assert status.json()["done"] is False
        await running.stop()
        stopped = True
        done = await _done(http, running.node_id, request_id, timeout_s=30)
        assert done["ok"] is False, done
        lost = [i for i in done["items"] if i["failure_type"] == "node_lost"]
        assert lost and lost[-1]["kind"] == "request", done
        assert lost[-1]["reason"] == "node_offline"
    finally:
        if not stopped:
            await running.stop()
