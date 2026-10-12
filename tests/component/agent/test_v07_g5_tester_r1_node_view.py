# ruff: noqa: S103
# (adversarial literals / world-writable modes / seeded fuzz are the point of these tests)
"""V0.7 G5 tester round 1: what the REST node view says about the owner policy
is the truth - and follows the live policy, a broken file and a stale copy.

Real PostgreSQL, a real mTLS controller, a real agent (see the G5 e2e file).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.agent.policy import write_overlay
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

ALICE_PW = "alice-password-1"
ADMIN_PW = "admin-password-1"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def alice(
    engine: AsyncEngine, runtime: ControllerRuntime
) -> AsyncIterator[httpx.AsyncClient]:
    await create_user(engine, "admin", ADMIN_PW, admin=True)
    await create_user(engine, "alice", ALICE_PW)
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        token = await login(anon, "alice", ALICE_PW)
    async with httpx.AsyncClient(
        base_url=base, headers=bearer(token), timeout=30
    ) as client:
        yield client


async def _agent(grpc_target: str, data_dir: Path, **policy: Any) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={
            "policy": {
                "accept_jobs": True,
                "allow_unsandboxed": True,
                "reload_interval_s": 0.1,
                **policy,
            }
        },
    )


async def _view(client: httpx.AsyncClient, node_id: str) -> dict[str, Any]:
    response = await client.get(f"/v1/nodes/{node_id}")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


async def _until(check: Callable[[], Any], within_s: float = 10.0) -> None:
    start = time.monotonic()
    while True:
        value = await check() if asyncio.iscoroutinefunction(check) else check()
        if asyncio.iscoroutine(value):
            value = await value
        if value:
            return
        assert time.monotonic() - start < within_s, "condition not reached"
        await asyncio.sleep(0.05)


async def test_not_enforced_follows_the_live_policy_and_hides_the_user_list(
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
    alice: httpx.AsyncClient,
) -> None:
    agent = await _agent(grpc_target, data_dir, allowed_users=["secret-bob", "carol"])
    try:
        assert agent.live_policy is not None
        view = (await _view(alice, agent.node_id))["owner_policy"]
        assert view["allowed_users_count"] == 2
        assert "secret-bob" not in str(view) and "allowed_users" not in view
        assert "max_vram_bytes:no_runtime_limit" in view["not_enforced"]
        if os.name == "posix":
            assert "max_cpu_percent:unsandboxed_linux" in view["not_enforced"]

        # live: the sandbox only -> only VRAM is a cap nothing enforces
        write_overlay(agent.live_policy.path, {"allow_unsandboxed": False})

        async def only_vram() -> bool:
            seen = (await _view(alice, agent.node_id))["owner_policy"]
            return list(seen["not_enforced"]) == ["max_vram_bytes:no_runtime_limit"]

        await _until(only_vram)
        # live: back on
        write_overlay(agent.live_policy.path, {"allow_unsandboxed": True})

        async def back() -> bool:
            seen = (await _view(alice, agent.node_id))["owner_policy"]
            return len(seen["not_enforced"]) > 1

        await _until(back)
    finally:
        await agent.stop()


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
async def test_a_world_writable_policy_file_closes_the_node_for_the_planner(
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
    alice: httpx.AsyncClient,
) -> None:
    agent = await _agent(grpc_target, data_dir)
    try:
        assert agent.live_policy is not None
        path = agent.live_policy.path
        write_overlay(path, {"allow_network": True})
        os.chmod(path, 0o666)  # content changes below -> the stamp changes

        path.write_bytes(path.read_bytes() + b"# touched\n")

        async def closed() -> bool:
            seen = (await _view(alice, agent.node_id))["owner_policy"]
            return seen["accept_jobs"] is False

        await _until(closed)
        seen = (await _view(alice, agent.node_id))["owner_policy"]
        assert seen["accept_jobs"] is False and seen["allow_network"] is False
        assert seen["allow_unsandboxed"] is False
    finally:
        await agent.stop()
