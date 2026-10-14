# ruff: noqa: F811, S311, T201, ASYNC110
"""V0.7 G6 tester r3 (T-14): cancel a secret-carrying SUBPROCESS job at many
random moments across the whole attempt life; afterwards nothing is left under
the agent's secret base or /dev/shm (listed directly, not through the product).

Also: the worst cancel latency, and a write that is BLOCKED (a hung filesystem
call): what happens to the cancel and to agent shutdown.
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_exec_harness import ExecAgent, submit, wait_job
from test_v07_g6_secret_delivery import (  # noqa: F401
    KEY,
    PASS,
    alice,
    settings,
)

from meshforge.agent.execution.secret_files import SecretVault

pytestmark = [
    pytest.mark.component,
    pytest.mark.asyncio,
    pytest.mark.timeout(1200),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

WORK = r"""
import os, sys, time
d = os.environ["MESHFORGE_SECRETS_DIR"]
print("k=" + open(os.path.join(d, "API_KEY")).read(), flush=True)
print("p=" + os.environ["DB_PASS"], flush=True)
time.sleep(60)
"""

MANIFEST = {
    "name": "r3-cancel",
    "entrypoint": ["python", "main.py"],
    "secrets": ["API_KEY", {"name": "DB_PASS", "as_env": True}],
    "retry": {"max_attempts": 1},
}


def _shm() -> set[str]:
    return {n for n in os.listdir("/dev/shm") if n.startswith("meshforge-")}


def _listing(base: Path) -> list[str]:
    return sorted(p.name for p in base.rglob("*")) if base.exists() else []


async def _agent(grpc_target: str, data: Path) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target,
        data,
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )


async def test_100_percent_clean_after_cancels_at_random_moments(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    rnd = random.Random(20261014)
    shm_before = await asyncio.to_thread(_shm)
    agent = await _agent(grpc_target, tmp_path / "agent")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WORK)
    base = agent.executor.vault.base
    cancels = 0
    latencies: list[float] = []
    states: dict[str, int] = {}
    try:
        for i in range(60):
            job_id = await submit(
                alice, project, agent.node_id, dict(MANIFEST, name=f"c{i}")
            )
            delay = rnd.choice(
                [
                    0.0,
                    0.0,
                    0.005,
                    0.01,
                    0.02,
                    0.04,
                    0.07,
                    0.1,
                    0.15,
                    0.2,
                    0.3,
                    0.5,
                    0.8,
                    1.2,
                    2.0,
                ]
            ) * rnd.uniform(0.8, 1.25)
            await asyncio.sleep(delay)
            t0 = time.monotonic()
            await alice.post(f"/v1/jobs/{job_id}/cancel")
            job = await wait_job(alice, job_id, timeout_s=60)
            latencies.append(time.monotonic() - t0)
            states[job["state"]] = states.get(job["state"], 0) + 1
            assert job["state"] in ("cancelled", "failed", "succeeded"), job
            cancels += 1
            # the wipe is part of the attempt: on disk it must be gone at once
            # or within a moment; poll the directory, not the API
            deadline = time.monotonic() + 10
            while _listing(base) and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert _listing(base) == [], (i, delay, _listing(base))
            extra = (await asyncio.to_thread(_shm)) - shm_before - {base.name}
            assert extra == set(), (i, extra)
        print(f"cancels={cancels} states={states} worst_latency={max(latencies):.2f}s")
        assert max(latencies) < 20
    finally:
        await agent.stop()
    # the values are nowhere under the data dir
    for path in (tmp_path / "agent").rglob("*"):
        if path.is_file() and path.stat().st_size < 5_000_000:
            data = path.read_bytes()
            assert KEY not in data and PASS not in data, path
    leftovers = await asyncio.to_thread(_shm)
    assert leftovers - shm_before - {base.name} == set()


async def test_a_blocked_write_delays_the_cancel_until_the_write_ends_and_does_not_hang_forever(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A filesystem call that blocks for 8 s inside ``materialise``. Measures
    when the job turns `cancelled` and when the folder is gone."""
    real = SecretVault.materialise
    entered = threading.Event()
    release = threading.Event()

    def blocked(self: SecretVault, *args: Any, **kwargs: Any) -> Path:
        entered.set()
        release.wait(8)  # "the filesystem call hangs", then completes
        return real(self, *args, **kwargs)

    monkeypatch.setattr(SecretVault, "materialise", blocked)
    agent = await _agent(grpc_target, tmp_path / "agent")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WORK)
    base = agent.executor.vault.base
    try:
        job_id = await submit(alice, project, agent.node_id, dict(MANIFEST))
        assert await asyncio.to_thread(entered.wait, 30)
        t0 = time.monotonic()
        await alice.post(f"/v1/jobs/{job_id}/cancel")
        job = await wait_job(alice, job_id, timeout_s=60)
        took = time.monotonic() - t0
        print(f"blocked write 8 s: job {job['state']} after {took:.1f}s")
        assert job["state"] == "cancelled", job
        for _ in range(100):
            if not _listing(base):
                break
            await asyncio.sleep(0.1)
        assert _listing(base) == []
        # the cancel waited for the thread (no folder appeared after the wipe)
        assert took >= 7, (
            "terminal before the blocked write ended: wipe could not have been after it"
        )
    finally:
        release.set()
        await agent.stop()


@pytest.mark.security
async def test_agent_shutdown_with_a_write_blocked_for_a_long_time_returns(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`agent.stop()` while the write thread is blocked 12 s: how long?"""
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    real = SecretVault.materialise

    def blocked(self: SecretVault, *args: Any, **kwargs: Any) -> Path:
        entered.set()
        release.wait(12)
        try:
            return real(self, *args, **kwargs)
        finally:
            returned.set()

    monkeypatch.setattr(SecretVault, "materialise", blocked)
    agent = await _agent(grpc_target, tmp_path / "agent")
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WORK)
    base = agent.executor.vault.base
    try:
        await submit(alice, project, agent.node_id, dict(MANIFEST))
        assert await asyncio.to_thread(entered.wait, 30)
        t0 = time.monotonic()
        await asyncio.wait_for(agent.stop(), timeout=60)
        took = time.monotonic() - t0
        print(f"agent.stop() with a write blocked 12 s: {took:.1f}s")
    finally:
        release.set()
    # The write runs on after the stop (that is the point); an empty base
    # before it has even made the folder proves nothing - poll once it is done.
    assert await asyncio.to_thread(returned.wait, 30)
    for _ in range(100):
        if not _listing(base):
            break
        await asyncio.sleep(0.1)
    assert _listing(base) == [], "plaintext left after agent stop"
