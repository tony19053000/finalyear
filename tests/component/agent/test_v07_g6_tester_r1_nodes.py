"""V0.7 G6 tester r1, item 0: ``compute nodes`` shows owner caps that are NOT
enforced, and the CPU cgroup probe says so (typed) when it cannot delegate.

A real controller, a real agent whose owner caps CPU at 50 % and allows
unsandboxed jobs; ``compute nodes`` as a separate process.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_fabric import ca_file
from mf_users import bearer, create_user, login

from meshforge.agent.execution.cpu_cgroup import probe_cpu_cgroups
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def token(pg_settings: DatabaseSettings, runtime: ControllerRuntime) -> str:
    engine = create_engine(pg_settings)
    try:
        await create_user(engine, "alice", "alice-password-1")
    finally:
        await engine.dispose()
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as anon:
        return await login(anon, "alice", "alice-password-1")


async def test_the_probe_never_raises_and_gives_a_typed_reason_here() -> None:
    probe = probe_cpu_cgroups()
    if probe.parent is None:
        assert probe.reason in {
            "not_linux",
            "no_cgroup_v2",
            "not_delegated",
            "no_cpu_controller",
            "not_writable",
            "cannot_enable_cpu",
        }
    for platform in ("win32", "darwin"):
        assert probe_cpu_cgroups(platform=platform).reason == "not_linux"
    assert probe_cpu_cgroups(root=Path("/nonexistent")).reason == "no_cgroup_v2"


async def test_compute_nodes_lists_the_unenforced_cap(
    grpc_target: str,
    runtime: ControllerRuntime,
    token: str,
    tmp_path: Path,
) -> None:
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={
            "policy": {
                "accept_jobs": True,
                "allow_unsandboxed": True,
                "max_cpu_percent": 50,
            }
        },
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{runtime.http_port}",
            headers=bearer(token),
            timeout=30,
        ) as http:
            rows = []
            for _ in range(100):
                rows = (await http.get("/v1/nodes")).json().get("nodes", [])
                if rows and rows[0].get("not_enforced") is not None:
                    break
                await asyncio.sleep(0.1)
        row = rows[0]
        delegated = probe_cpu_cgroups().parent is not None
        if delegated:
            assert not any("cpu" in str(x) for x in row["not_enforced"]), row
        else:
            assert any("cpu" in str(x) for x in row["not_enforced"]), row
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.upper().startswith("MESHFORGE_")
        }
        env.update(
            {
                "MESHFORGE_CLI__CONTROLLER_URL": f"https://127.0.0.1:{runtime.http_port}",
                "MESHFORGE_CLI__CA_FILE": str(ca_file()),
                "MESHFORGE_CLI__TOKEN": token,
                "XDG_CONFIG_HOME": str(tmp_path / "cli"),
                "COLUMNS": "200",
            }
        )
        out = []
        for args in (("nodes",), ("nodes", agent.node_id), ("nodes", "--json")):
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "meshforge.cli",
                *args,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 60)
            assert proc.returncode == 0, (args, stderr.decode()[-400:])
            out.append(stdout.decode())
        assert "NOT ENFORCED" in out[0]
        assert ("cpu" in out[0]) is (not delegated)
        assert "not_enforced" in out[1] or "not enforced" in out[1].lower()
    finally:
        await agent.stop()
