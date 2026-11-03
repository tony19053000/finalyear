"""V1.0 G8 F5 (D428): "Run jobs" turned on in a running agent takes effect live.

Lab sequence (installed v0.17.0): the agent connected with ``accept_jobs`` off,
the owner turned Run jobs (and "Allow jobs without a container") on in the
console, the controller logged ``session.policy_updated accept_jobs=true`` -
and every ``compute run --node <id>`` was still refused 409 "the node does not
accept jobs", because the agent had probed no runtime and advertised no
``exec`` at its Hello.

Real PostgreSQL, a real controller (mTLS/HTTPS, planner, dispatcher) and a real
agent (subprocess runtime, no Docker - like the lab's Windows laptops):

* connect with ``accept_jobs = false`` and ``allow_unsandboxed = false``: the
  agent holds a *look-only* setup (no side-effecting probe: the cgroup probe
  never ran) and still advertises ``exec`` (what it CAN run); a pinned job is
  refused 409 ``policy_not_accepting`` (what its owner ALLOWS);
* the owner turns both on (the live file the console writes) -> the agent
  completes its setup **once** (the cgroup probe runs once), hands the session
  over **once** so a fresh Hello carries the capabilities, and the same pinned
  job is admitted and **succeeds** - no restart;
* turned off again -> refused at once at submit; on again -> admitted, with no
  second activation and no second hand-over;
* GPU slots live: ``max_gpu_jobs = 0`` -> a ``gpu: required`` job is refused
  ``no_gpu_slots``; ``max_gpu_jobs = 1`` live -> it runs.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of

from meshforge.agent.execution.cpu_cgroup import CpuCgroupProbe
from meshforge.agent.policy import write_overlay
from meshforge.common.capabilities import EXEC_CAPABILITY, EXEC_GPU_CAPABILITY
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

PROGRAM = 'print("hello from a live-enabled node", flush=True)\n'
MANIFEST: dict[str, Any] = {"name": "f5", "entrypoint": ["python", "main.py"]}
GPU_MANIFEST: dict[str, Any] = {**MANIFEST, "resources": {"gpu": "required"}}


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings, jobs={"dispatch_interval_s": 0.05}, **tolerant_liveness()
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "main.py").write_text(PROGRAM, "utf-8")
    return folder


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    )


async def _until(check: Callable[[], bool], within_s: float = 10.0) -> None:
    start = time.monotonic()
    while not check():
        assert time.monotonic() - start < within_s, "condition not reached"
        await asyncio.sleep(0.02)


async def _post(
    http: httpx.AsyncClient, project: Path, node_id: str, manifest: dict[str, Any]
) -> httpx.Response:
    from meshforge.common.bundle import pack_to_file

    bundle = project.parent / "p.bundle"
    packed = await asyncio.to_thread(pack_to_file, project, bundle)
    put = await http.put(f"/v1/blobs/{packed.digest.hex}", content=bundle.read_bytes())
    assert put.status_code in (200, 201), put.text
    return await http.post(
        "/v1/jobs",
        json={
            "manifest": manifest,
            "bundle_digest": str(packed.digest),
            "placement": {"node_id": node_id},
        },
    )


NOT_ACCEPTING = "does not accept jobs now"  # owner_policy NOT_ACCEPTING text
NO_GPU_SLOTS = "allows no GPU jobs"  # feasibility NO_GPU_SLOTS text


def _refusal(response: httpx.Response) -> str:
    assert response.status_code == 409, response.text
    return str(response.json()["error"]["message"])


class _CgroupSpy:
    """Stands in for ``probe_cpu_cgroups`` (the probe that may write
    ``cgroup.subtree_control``): counts calls, never touches the host."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> CpuCgroupProbe:
        self.calls += 1
        return CpuCgroupProbe(None, "not_delegated")


async def _agent(
    grpc_target: str, data_dir: Path, *, spy: _CgroupSpy | None = None, **policy: Any
) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data_dir,
        cgroup_probe=spy,
        agent_overrides={
            "policy": {
                "accept_jobs": False,
                "allow_unsandboxed": False,
                "reload_interval_s": 0.1,
                **policy,
            }
        },
        **({"fake_gpus": 1} if "max_gpu_jobs" in policy else {}),
    )


async def test_run_jobs_turned_on_after_connect_admits_and_off_refuses(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    spy = _CgroupSpy()
    agent = await _agent(grpc_target, data_dir, spy=spy)
    try:
        node_id = agent.node_id
        manager = runtime.manager
        assert manager is not None
        live = manager.get(node_id)
        assert live is not None and live.policy is not None
        assert not live.policy.accept_jobs
        session_id = live.session_id
        # look only: no side-effecting probe ran; CAN is still advertised
        assert not agent.setup.complete and spy.calls == 0
        assert EXEC_CAPABILITY in agent.setup.offer.capabilities
        activator = agent.activator
        assert activator is not None and activator.pending

        async with _http(runtime) as http:
            refused = await _post(http, project, node_id, MANIFEST)
            assert NOT_ACCEPTING in _refusal(refused)

            # the console's Settings page: Run jobs + no container, live
            assert agent.live_policy is not None
            write_overlay(
                agent.live_policy.path,
                {"accept_jobs": True, "allow_unsandboxed": True},
            )

            def accepting() -> bool:
                now = manager.get(node_id)
                return now is not None and bool(now.policy and now.policy.accept_jobs)

            await _until(accepting)
            job_id = await submit(http, project, node_id, MANIFEST)
            done = await wait_job(http, job_id, timeout_s=60)
            assert done["state"] == "succeeded", done
            # the first enable completed the setup once and re-advertised once
            assert activator.activations == 1 and not activator.pending
            assert spy.calls == 1
            assert agent.client.handovers == 1
            now = manager.get(node_id)
            assert now is not None and now.session_id != session_id  # one re-Hello

            # off again: the very next submit is refused
            write_overlay(
                agent.live_policy.path,
                {"accept_jobs": False, "allow_unsandboxed": True},
            )
            await _until(lambda: not accepting())
            refused = await _post(http, project, node_id, MANIFEST)
            assert NOT_ACCEPTING in _refusal(refused)

            # on again: live, no second activation, no second hand-over
            write_overlay(
                agent.live_policy.path,
                {"accept_jobs": True, "allow_unsandboxed": True},
            )
            await _until(accepting)
            job_id = await submit(http, project, node_id, MANIFEST)
            assert (await wait_job(http, job_id, timeout_s=60))["state"] == "succeeded"
            assert activator.activations == 1 and spy.calls == 1
            assert agent.client.handovers == 1
    finally:
        await agent.stop()


async def test_gpu_slots_change_live(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    agent = await _agent(
        grpc_target,
        data_dir,
        accept_jobs=True,
        allow_unsandboxed=True,
        max_gpu_jobs=0,
    )
    try:
        node_id = agent.node_id
        manager = runtime.manager
        assert manager is not None
        assert EXEC_GPU_CAPABILITY in agent.setup.offer.capabilities
        async with _http(runtime) as http:
            refused = await _post(http, project, node_id, GPU_MANIFEST)
            assert NO_GPU_SLOTS in _refusal(refused)

            assert agent.live_policy is not None
            write_overlay(agent.live_policy.path, {"max_gpu_jobs": 1})

            def gpu_slots() -> int | None:
                live = manager.get(node_id)
                slots = None if live is None else live.policy_slots
                return None if slots is None else slots[1]

            await _until(lambda: gpu_slots() == 1)
            job_id = await submit(http, project, node_id, GPU_MANIFEST)
            done = await wait_job(http, job_id, timeout_s=60)
            assert done["state"] == "succeeded", done

            write_overlay(agent.live_policy.path, {"max_gpu_jobs": 0})
            await _until(lambda: gpu_slots() == 0)
            refused = await _post(http, project, node_id, GPU_MANIFEST)
            assert NO_GPU_SLOTS in _refusal(refused)
    finally:
        await agent.stop()
