# ruff: noqa: F811, S311, T201, ASYNC110
"""V0.7 G6 tester r3 (T-14, 4a79155): cancel secret-carrying DOCKER jobs at many
random moments; afterwards nothing under the agent's secret base, /dev/shm, and
`docker ps -a` (every state) shows no meshforge container.
"""

from __future__ import annotations

import asyncio
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from mf_exec_harness import ExecAgent, submit, wait_job
from test_v07_g6_tester_r1_docker import (  # noqa: F401
    V1,
    _docker_ready,
    alice,
    settings,
)

pytestmark = [
    pytest.mark.component,
    pytest.mark.asyncio,
    pytest.mark.timeout(1800),
    pytest.mark.docker,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers"),
]

WORK = (
    "import sys, time\n"
    "print('k=' + open('/run/secrets/ONE').read(), flush=True)\n"
    "print('.' * 300, flush=True)\n"
    "time.sleep(float(sys.argv[1]))\n"
)


def _ps_a() -> set[str]:
    out = subprocess.run(
        ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return set(out.split())


def _listing(base: Path) -> list[str]:
    return sorted(p.name for p in base.rglob("*")) if base.exists() else []


async def test_cancel_docker_jobs_at_random_moments_leaves_nothing(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    if not await asyncio.to_thread(_docker_ready):
        pytest.skip("Docker or the pinned python-cpu image is not available here")
    rnd = random.Random(20261014)
    ps_before = await asyncio.to_thread(_ps_a)
    shm_before = {n for n in os.listdir("/dev/shm") if n.startswith("meshforge-")}
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": False}},
        docker=True,
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WORK)
    base = agent.executor.vault.base
    latencies: list[float] = []
    states: dict[str, int] = {}
    try:
        for i in range(45):
            manifest = {
                "name": f"d{i}",
                "entrypoint": ["python", "main.py", "60"],
                "secrets": ["ONE"],
                "retry": {"max_attempts": 1},
            }
            job_id = await submit(alice, project, agent.node_id, manifest)
            await asyncio.sleep(
                rnd.choice(
                    [0.0, 0.0, 0.02, 0.05, 0.1, 0.2, 0.35, 0.5, 0.8, 1.2, 1.8, 2.6, 3.5]
                )
                * rnd.uniform(0.8, 1.25)
            )
            t0 = time.monotonic()
            await alice.post(f"/v1/jobs/{job_id}/cancel")
            job = await wait_job(alice, job_id, timeout_s=120)
            latencies.append(time.monotonic() - t0)
            states[job["state"]] = states.get(job["state"], 0) + 1
            assert job["state"] in ("cancelled", "failed", "succeeded"), job
            deadline = time.monotonic() + 15
            while _listing(base) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert _listing(base) == [], (i, _listing(base))
            extra = {n for n in os.listdir("/dev/shm") if n.startswith("meshforge-")} - shm_before - {base.name}
            assert extra == set(), (i, extra)
        print(f"docker cancels=45 states={states} worst_latency={max(latencies):.2f}s")
        left: set[str] = set()
        for _ in range(100):
            left = (await asyncio.to_thread(_ps_a)) - ps_before
            if not left:
                break
            await asyncio.sleep(0.2)
        assert left == set(), left
    finally:
        await agent.stop()
    for path in (tmp_path / "agent").rglob("*"):
        if path.is_file() and path.stat().st_size < 5_000_000:
            assert V1 not in path.read_bytes(), path


@pytest.mark.parametrize("where", ["before_start_call", "after_start_call"])
async def test_cancel_while_container_start_runs_in_its_thread_leaves_nothing(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    where: str,
) -> None:
    """The site the implementer left as benign by reading: `container.start` in a
    thread that a cancel cannot stop. Hold it 1.5 s before / after the real call."""
    import threading

    if not await asyncio.to_thread(_docker_ready):
        pytest.skip("Docker or the pinned python-cpu image is not available here")
    from docker.models.containers import Container

    real = Container.start
    entered = threading.Event()

    def slow(self: Container, *a: object, **k: object) -> object:
        entered.set()
        if where == "before_start_call":
            time.sleep(1.5)
            return real(self, *a, **k)
        out = real(self, *a, **k)
        time.sleep(1.5)
        return out

    monkeypatch.setattr(Container, "start", slow)
    ps_before = await asyncio.to_thread(_ps_a)
    shm_before = {n for n in os.listdir("/dev/shm") if n.startswith("meshforge-")}
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": False}},
        docker=True,
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WORK)
    base = agent.executor.vault.base
    try:
        for i in range(3):
            entered.clear()
            job_id = await submit(
                alice,
                project,
                agent.node_id,
                {
                    "name": f"s{i}",
                    "entrypoint": ["python", "main.py", "60"],
                    "secrets": ["ONE"],
                    "retry": {"max_attempts": 1},
                },
            )
            assert await asyncio.to_thread(entered.wait, 60)
            await asyncio.sleep(0.4)
            await alice.post(f"/v1/jobs/{job_id}/cancel")
            job = await wait_job(alice, job_id, timeout_s=120)
            assert job["state"] == "cancelled", job
            deadline = time.monotonic() + 15
            while _listing(base) and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert _listing(base) == []
        await asyncio.sleep(3)  # the held thread has finished its real start by now
        left: set[str] = set()
        for _ in range(100):
            left = (await asyncio.to_thread(_ps_a)) - ps_before
            if not left:
                break
            await asyncio.sleep(0.2)
        assert left == set(), (where, left)
        assert {n for n in os.listdir("/dev/shm") if n.startswith("meshforge-")} - shm_before - {base.name} == set()
    finally:
        await agent.stop()
