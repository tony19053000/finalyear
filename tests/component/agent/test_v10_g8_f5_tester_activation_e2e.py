"""V1.0 G8 F5 round 2 tester: ExecutionActivator under attack (real controller
+ agent). Jobs-off start is look-only; the first live enable activates once."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import submit, wait_job
from mf_jobs_harness import token_of
from test_v10_g8_f5_live_accept_jobs_e2e import (  # type: ignore[import-not-found]
    MANIFEST,
    NOT_ACCEPTING,
    _post,
    _refusal,
    _until,
)

from meshforge.agent.execution.cpu_cgroup import CpuCgroupProbe
from meshforge.agent.policy import write_overlay
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings, jobs={"dispatch_interval_s": 0.05}, **tolerant_liveness()
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "main.py").write_text("print('act ok')\n", "utf-8")
    return folder


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    )


class _Probe:
    """cgroup probe spy: counts calls, reports 'unavailable' (never writes)."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> CpuCgroupProbe:
        self.calls += 1
        return CpuCgroupProbe(parent=None, reason="tester_spy")


async def _start(grpc_target: str, data_dir: Path, probe: _Probe, **kw: Any) -> Any:
    from mf_exec_harness import ExecAgent

    return await ExecAgent.start(
        grpc_target,
        data_dir,
        agent_overrides={
            "policy": {
                "accept_jobs": False,
                "allow_unsandboxed": True,
                "reload_interval_s": 0.1,
            }
        },
        cgroup_probe=probe,
        **kw,
    )


def _accepting(runtime: ControllerRuntime, node_id: str, want: bool) -> Any:
    def check() -> bool:
        assert runtime.manager is not None
        now = runtime.manager.get(node_id)
        return now is not None and bool(now.policy) and now.policy.accept_jobs is want

    return check


def _session(runtime: ControllerRuntime, node_id: str) -> str | None:
    assert runtime.manager is not None
    now = runtime.manager.get(node_id)
    return None if now is None else now.session_id


async def test_jobs_off_start_has_zero_side_effects_then_one_activation(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    probe = _Probe()
    agent = await _start(grpc_target, data_dir, probe)
    try:
        node_id = agent.node_id
        # look-only: no cgroup probe/sweep, no executor start-up work, no Docker handle
        assert probe.calls == 0
        assert agent.setup.complete is False
        assert agent.setup.docker is None
        assert not agent.executor.work_root.exists()
        assert agent.activator is not None and agent.activator.activations == 0
        # still admits nothing
        async with _http(runtime) as http:
            assert NOT_ACCEPTING in _refusal(
                await _post(http, project, node_id, MANIFEST)
            )
            await asyncio.sleep(0.5)  # idle: nothing wakes the side effects
            assert probe.calls == 0 and not agent.executor.work_root.exists()

            first = _session(runtime, node_id)
            write_overlay(agent.live_policy.path, {"accept_jobs": True})
            await _until(lambda: agent.activator.activations == 1, 20)
            await _until(lambda: _session(runtime, node_id) not in (None, first), 20)
            await _until(_accepting(runtime, node_id, True), 20)
            assert probe.calls == 1
            assert agent.executor.work_root.exists()
            assert agent.setup is not None and agent.activator.setup.complete
            second = _session(runtime, node_id)

            job_id = await submit(http, project, node_id, MANIFEST)
            done = await wait_job(http, job_id, timeout_s=60)
            assert done["state"] == "succeeded", done

            # off/on x5: never a second activation, probe, or hand-over
            for i in range(5):
                write_overlay(agent.live_policy.path, {"accept_jobs": i % 2 == 1})
                await asyncio.sleep(0.15)
            write_overlay(agent.live_policy.path, {"accept_jobs": True})
            await _until(_accepting(runtime, node_id, True), 20)
            await asyncio.sleep(0.3)
            assert agent.activator.activations == 1
            assert probe.calls == 1
            assert _session(runtime, node_id) == second
            job_id = await submit(http, project, node_id, MANIFEST)
            assert (await wait_job(http, job_id, timeout_s=60))["state"] == "succeeded"
    finally:
        await agent.stop()


async def test_on_off_on_during_a_slow_activation_activates_once_and_ends_on(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    probe = _Probe()
    agent = await _start(grpc_target, data_dir, probe)
    try:
        node_id = agent.node_id
        activator = agent.activator
        real_build = activator._build
        entered = asyncio.Event()
        release = asyncio.Event()
        builds = 0

        async def slow(*a: Any, **kw: Any) -> Any:
            nonlocal builds
            builds += 1
            entered.set()
            await release.wait()
            return await real_build(*a, **kw)

        activator._build = slow
        path = agent.live_policy.path
        async with _http(runtime) as http:
            write_overlay(path, {"accept_jobs": True})
            await asyncio.wait_for(entered.wait(), 10)
            write_overlay(path, {"accept_jobs": False})
            await asyncio.sleep(0.2)
            write_overlay(path, {"accept_jobs": True})
            await asyncio.sleep(0.3)
            # mid-activation: the controller has not been told "on" yet
            assert not _accepting(runtime, node_id, True)()
            refused = await _post(http, project, node_id, MANIFEST)
            assert refused.status_code == 409
            release.set()
            await _until(_accepting(runtime, node_id, True), 30)
            await asyncio.sleep(0.5)
            assert builds == 1 and activator.activations == 1 and probe.calls == 1
            job_id = await submit(http, project, node_id, MANIFEST)
            done = await wait_job(http, job_id, timeout_s=60)
            assert done["state"] == "succeeded", done
    finally:
        await agent.stop()


async def test_off_again_during_activation_ends_off_and_refuses(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    """on, then off while the build runs: the end state is OFF; the controller
    must end up OFF too (a stale 'on' update would admit jobs the owner refused)."""
    probe = _Probe()
    agent = await _start(grpc_target, data_dir, probe)
    try:
        node_id = agent.node_id
        activator = agent.activator
        real_build = activator._build
        entered, release = asyncio.Event(), asyncio.Event()

        async def slow(*a: Any, **kw: Any) -> Any:
            entered.set()
            await release.wait()
            return await real_build(*a, **kw)

        activator._build = slow
        path = agent.live_policy.path
        async with _http(runtime) as http:
            write_overlay(path, {"accept_jobs": True})
            await asyncio.wait_for(entered.wait(), 10)
            write_overlay(path, {"accept_jobs": False})
            await asyncio.sleep(0.3)
            release.set()
            await _until(lambda: activator.activations == 1, 30)
            await asyncio.sleep(1.5)  # hand-over + reconnect + queued updates settle
            await _until(_accepting(runtime, node_id, False), 20)
            assert NOT_ACCEPTING in _refusal(
                await _post(http, project, node_id, MANIFEST)
            )
    finally:
        await agent.stop()


async def test_activation_failure_is_retried_and_does_not_strand_the_node(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    """Build blows up once (Docker down / unexpected error). The node must not be
    left refusing forever: a retry has to succeed (here: the owner re-toggles)."""
    probe = _Probe()
    agent = await _start(grpc_target, data_dir, probe)
    try:
        node_id = agent.node_id
        activator = agent.activator
        real_build = activator._build
        attempts = 0

        async def flaky(*a: Any, **kw: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("docker daemon went away")
            return await real_build(*a, **kw)

        activator._build = flaky
        path = agent.live_policy.path
        async with _http(runtime) as http:
            write_overlay(path, {"accept_jobs": True})
            await _until(lambda: attempts >= 1, 10)
            await asyncio.sleep(0.5)
            assert activator.activations == 0 and activator.pending
            assert agent.setup.complete is False  # still look-only, still safe
            # owner toggles off then on: retry must work
            write_overlay(path, {"accept_jobs": False})
            await asyncio.sleep(0.3)
            write_overlay(path, {"accept_jobs": True})
            await _until(lambda: activator.activations == 1, 30)
            await _until(_accepting(runtime, node_id, True), 30)
            job_id = await submit(http, project, node_id, MANIFEST)
            assert (await wait_job(http, job_id, timeout_s=60))["state"] == "succeeded"
    finally:
        await agent.stop()


async def test_activation_failure_without_retoggle_self_heals(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    """After a failed activation the owner's file still says Run jobs ON. The
    agent must not sit with local policy ON, controller told OFF, and no retry
    until the owner happens to flip the switch twice."""
    probe = _Probe()
    agent = await _start(grpc_target, data_dir, probe)
    try:
        node_id = agent.node_id
        activator = agent.activator
        real_build = activator._build
        attempts = 0

        async def flaky(*a: Any, **kw: Any) -> Any:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("docker daemon went away")
            return await real_build(*a, **kw)

        activator._build = flaky
        write_overlay(agent.live_policy.path, {"accept_jobs": True})
        await _until(lambda: attempts >= 1, 10)
        # no further owner action: within a generous window it must recover
        await _until(lambda: activator.activations == 1, 30)
        await _until(_accepting(runtime, node_id, True), 30)
    finally:
        await agent.stop()
