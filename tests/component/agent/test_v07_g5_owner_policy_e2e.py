"""V0.7 G5 (roadmap 0.7.8, D142): owner policy end to end - real PostgreSQL, a
real controller (mTLS/HTTPS), a real agent running real jobs.

* the agent advertises ``[agent.policy]`` in Hello; the controller keeps it on
  the live session;
* ``policy set`` (the live file) reaches the controller as a ``PolicyUpdate``
  within the reload interval - no restart - and the planner uses it at once;
* a job no node's policy allows is refused at submit with the ``policy_*``
  codes (409) and a queued one fails ``no_feasible_plan`` naming the code -
  never hangs; ``compute explain``'s data (plan decisions) names the code;
* defence in depth: with the controller's copy forced stale/open, the agent
  still refuses a violating StartAttempt ``FAILED(unauthorized)``,
  ``policy_denied:<code>`` - and the controller does not retry it.
"""

from __future__ import annotations

import asyncio
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
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.agent.policy import write_overlay
from meshforge.common import owner_policy as op
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"
GIB = 1024**3
PROGRAM = 'print("hello from a policy-checked job", flush=True)\n'
MANIFEST: dict[str, Any] = {"name": "p", "entrypoint": ["python", "main.py"]}


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
async def clients(
    engine: AsyncEngine, runtime: ControllerRuntime
) -> AsyncIterator[dict[str, httpx.AsyncClient]]:
    await create_user(engine, "admin", ADMIN_PW, admin=True)
    await create_user(engine, "alice", ALICE_PW)
    await create_user(engine, "bob", BOB_PW)
    base = f"https://127.0.0.1:{runtime.http_port}"
    out: dict[str, httpx.AsyncClient] = {}
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        for name, pw in (("admin", ADMIN_PW), ("alice", ALICE_PW), ("bob", BOB_PW)):
            token = await login(anon, name, pw)
            out[name] = httpx.AsyncClient(
                base_url=base, headers=bearer(token), timeout=30
            )
    try:
        yield out
    finally:
        for client in out.values():
            await client.aclose()


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "main.py").write_text(PROGRAM, "utf-8")
    return folder


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


async def _until(check: Callable[[], bool], within_s: float = 10.0) -> float:
    start = time.monotonic()
    while not check():
        assert time.monotonic() - start < within_s, "condition not reached"
        await asyncio.sleep(0.02)
    return time.monotonic() - start


def _live_policy(runtime: ControllerRuntime, node_id: str) -> op.OwnerPolicy | None:
    live = runtime.manager.get(node_id) if runtime.manager is not None else None
    return None if live is None else live.policy


async def _post(
    client: httpx.AsyncClient, project: Path, manifest: dict[str, Any]
) -> httpx.Response:
    from meshforge.common.bundle import pack_to_file

    bundle = project.parent / "p.bundle"
    packed = await asyncio.to_thread(pack_to_file, project, bundle)
    put = await client.put(
        f"/v1/blobs/{packed.digest.hex}", content=bundle.read_bytes()
    )
    assert put.status_code in (200, 201), put.text
    return await client.post(
        "/v1/jobs",
        json={
            "manifest": manifest,
            "bundle_digest": str(packed.digest),
            "placement": {},
        },
    )


async def test_hello_policy_live_change_and_submit_codes(
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
    clients: dict[str, httpx.AsyncClient],
    project: Path,
) -> None:
    agent = await _agent(grpc_target, data_dir, allowed_users=["alice"])
    try:
        advertised = _live_policy(runtime, agent.node_id)
        assert advertised is not None and advertised.allowed_users == ("alice",)
        assert advertised.accept_jobs and not advertised.allow_network

        # bob is not allowed anywhere: 409 at submit naming the code.
        refused = await _post(clients["bob"], project, MANIFEST)
        assert refused.status_code == 409, refused.text
        rejections = refused.json()["error"]["rejections"]
        assert [r["reject_code"] for r in rejections] == ["policy_user_not_allowed"]
        # network: on is not allowed by this owner.
        refused = await _post(
            clients["alice"], project, {**MANIFEST, "limits": {"network": "on"}}
        )
        assert refused.status_code == 409, refused.text
        assert "policy_network" in refused.text

        # alice's job runs.
        job_id = await submit(clients["alice"], project, None, MANIFEST)
        done = await wait_job(clients["alice"], job_id, timeout_s=60)
        assert done["state"] == "succeeded", done

        # Live change (what `policy set` writes): bob allowed, RAM capped.
        assert agent.live_policy is not None
        write_overlay(
            agent.live_policy.path,
            {"allowed_users": ["alice", "bob"], "max_ram_bytes": GIB},
        )
        took = await _until(
            lambda: (
                (_live_policy(runtime, agent.node_id) or op.DENY_ALL).max_ram_bytes
                == GIB
            )
        )
        assert took < 5.0  # reload 0.1 s + one report round trip
        assert _live_policy(runtime, agent.node_id).allowed_users == ("alice", "bob")  # type: ignore[union-attr]
        job_id = await submit(clients["bob"], project, None, MANIFEST)
        assert (await wait_job(clients["bob"], job_id, timeout_s=60))["state"] == (
            "succeeded"
        )
        refused = await _post(
            clients["bob"], project, {**MANIFEST, "resources": {"ram_gb": 2}}
        )
        assert refused.status_code == 409 and "policy_ram" in refused.text

        # The owner stops accepting while job A runs and job B waits for the
        # node's only CPU slot: B fails naming the code (never hangs); A is not
        # killed (a tightened policy blocks the next start only).
        slow = project.parent / "slow"
        slow.mkdir()
        (slow / "main.py").write_text(
            "import time\ntime.sleep(4)\nprint('done', flush=True)\n", "utf-8"
        )
        job_a = await submit(clients["alice"], slow, None, MANIFEST)
        await wait_job(clients["alice"], job_a, states=frozenset({"running"}))
        job_b = await submit(clients["alice"], project, None, MANIFEST)
        write_overlay(
            agent.live_policy.path,
            {"allowed_users": ["alice", "bob"], "accept_jobs": False},
        )
        await _until(
            lambda: not (_live_policy(runtime, agent.node_id) or OPEN).accept_jobs
        )
        failed = await wait_job(clients["alice"], job_b, timeout_s=30)
        assert failed["state"] == "failed", failed
        assert failed["failure_type"] == "no_feasible_plan", failed
        assert "compute explain" in (failed["failure_detail"] or ""), failed
        decisions = (await clients["alice"].get(f"/v1/jobs/{job_b}/decisions")).json()
        assert "policy_not_accepting" in str(decisions)  # compute explain's data
        assert (await wait_job(clients["alice"], job_a, timeout_s=30))["state"] == (
            "succeeded"
        )
        refused = await _post(clients["alice"], project, MANIFEST)
        assert refused.status_code == 409 and "policy_not_accepting" in refused.text
    finally:
        await agent.stop()


OPEN = op.OPEN_POLICY


async def test_the_agent_refuses_what_a_stale_controller_sends(
    runtime: ControllerRuntime,
    grpc_target: str,
    data_dir: Path,
    clients: dict[str, httpx.AsyncClient],
    project: Path,
) -> None:
    agent = await _agent(grpc_target, data_dir, max_ram_bytes=GIB)
    try:
        assert runtime.manager is not None
        live = runtime.manager.get(agent.node_id)
        assert live is not None and live.policy is not None
        live.policy = OPEN  # the controller's copy is wrong (stale/compromised)
        job_id = await submit(
            clients["alice"], project, None, {**MANIFEST, "resources": {"ram_gb": 2}}
        )
        done = await wait_job(clients["alice"], job_id, timeout_s=60)
        assert done["state"] == "failed", done
        assert done["failure_type"] == "unauthorized", done
        assert "policy_denied:policy_ram" in str(done), done
        assert len(done["attempts"]) == 1  # unauthorized: never retried
        assert done["attempts"][0]["failure_type"] == "unauthorized"
    finally:
        await agent.stop()
