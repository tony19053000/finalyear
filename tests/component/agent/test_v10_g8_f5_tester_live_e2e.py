"""V1.0 G8 F5 tester: real controller + agent, adversarial live-policy flips."""

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
    _agent,
    _post,
    _refusal,
    _until,
)

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
    (folder / "main.py").write_text(
        "import time\ntime.sleep(4)\nprint('slow done', flush=True)\n", "utf-8"
    )
    return folder


def _http(runtime: ControllerRuntime) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    )


def _accepting(runtime: ControllerRuntime, node_id: str, want: bool) -> Any:
    def check() -> bool:
        assert runtime.manager is not None
        now = runtime.manager.get(node_id)
        return now is not None and bool(now.policy) and now.policy.accept_jobs is want

    return check


async def test_running_job_survives_run_jobs_off_and_new_ones_are_refused(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    """(7) documented: a tighter policy does not stop a running job."""
    agent = await _agent(
        grpc_target, data_dir, accept_jobs=True, allow_unsandboxed=True
    )
    try:
        node_id = agent.node_id
        async with _http(runtime) as http:
            job_id = await submit(http, project, node_id, MANIFEST)
            for _ in range(200):
                state = (await http.get(f"/v1/jobs/{job_id}")).json()["state"]
                if state == "running":
                    break
                await asyncio.sleep(0.05)
            assert state == "running", state
            assert agent.live_policy is not None
            write_overlay(agent.live_policy.path, {"accept_jobs": False})
            await _until(_accepting(runtime, node_id, False))
            refused = await _post(http, project, node_id, MANIFEST)
            assert NOT_ACCEPTING in _refusal(refused)
            done = await wait_job(http, job_id, timeout_s=60)
            assert done["state"] == "succeeded", done
    finally:
        await agent.stop()


async def test_rapid_flips_never_run_a_job_while_the_final_state_is_off(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    """Flip on/off quickly, submit between flips (pinned and auto); end OFF.
    Every submit is either a clean 409 or a created job; nothing may start
    after the final OFF has been seen, and the node stays usable (on again ->
    job runs)."""
    (project / "main.py").write_text("print('quick')\n", "utf-8")
    agent = await _agent(grpc_target, data_dir)
    try:
        node_id = agent.node_id
        assert agent.live_policy is not None
        path = agent.live_policy.path
        async with _http(runtime) as http:
            created: list[str] = []
            for i in range(8):
                on = i % 2 == 0
                write_overlay(path, {"accept_jobs": on, "allow_unsandboxed": True})
                resp = await _post(http, project, node_id if i % 4 else None, MANIFEST)  # type: ignore[arg-type]
                assert resp.status_code in (201, 200, 409), resp.text
                if resp.status_code in (200, 201):
                    created.append(
                        resp.json()["job_id"]
                        if "job_id" in resp.json()
                        else resp.json()["id"]
                    )
                await asyncio.sleep(0.05)
            write_overlay(path, {"accept_jobs": False, "allow_unsandboxed": True})
            await _until(_accepting(runtime, node_id, False))
            for job_id in created:  # let anything admitted settle
                await wait_job(
                    http,
                    job_id,
                    timeout_s=60,
                    states=frozenset({"succeeded", "failed", "cancelled", "queued"}),
                )
            refused_pinned = await _post(http, project, node_id, MANIFEST)
            assert refused_pinned.status_code == 409
            refused_auto = await _post(http, project, None, MANIFEST)  # type: ignore[arg-type]
            assert refused_auto.status_code == 409, refused_auto.text
            write_overlay(path, {"accept_jobs": True, "allow_unsandboxed": True})
            await _until(_accepting(runtime, node_id, True))
            (project / "main.py").write_text("print('again')\n", "utf-8")
            job_id = await submit(http, project, node_id, MANIFEST)
            done = await wait_job(http, job_id, timeout_s=60)
            assert done["state"] == "succeeded", done
    finally:
        await agent.stop()


async def test_allow_unsandboxed_off_live_blocks_the_only_runtime(
    runtime: ControllerRuntime, grpc_target: str, data_dir: Path, project: Path
) -> None:
    """(2) subprocess is advertised; allow_unsandboxed off -> 409 at submit
    (policy_unsandboxed), no job ever created, then on -> runs."""
    (project / "main.py").write_text("print('x')\n", "utf-8")
    agent = await _agent(
        grpc_target, data_dir, accept_jobs=True, allow_unsandboxed=False
    )
    try:
        node_id = agent.node_id
        assert "subprocess" in agent.setup.runtimes
        async with _http(runtime) as http:
            refused = await _post(http, project, node_id, MANIFEST)
            assert refused.status_code == 409, refused.text
            assert (
                "unsandboxed" in refused.text.lower()
                or "sandbox" in refused.text.lower()
            )
            assert agent.live_policy is not None
            write_overlay(agent.live_policy.path, {"allow_unsandboxed": True})

            async def _ok() -> None:
                for _ in range(100):
                    r = await _post(http, project, node_id, MANIFEST)
                    if r.status_code in (200, 201):
                        return
                    await asyncio.sleep(0.1)
                raise AssertionError("never admitted")

            await _ok()
    finally:
        await agent.stop()
