# ruff: noqa: F811, S311  # tester test file: seeded RNG, report prints, re-imported fixtures
"""V0.7 G6 tester r2 (4a79155): cancelling a secret-carrying Docker job at
RANDOM moments leaves nothing - proven with `docker ps -a` (every state), the
agent's secrets folder, and no unclosed Docker socket.

Moments: before the controller even dispatched, while the host folder exists
(PREPARING/STARTING), while the container runs, and a short timeout.
"""

from __future__ import annotations

import asyncio
import gc
import random
import subprocess
import sys
import warnings
from pathlib import Path

import httpx
import pytest
from mf_exec_harness import ExecAgent, submit, wait_job
from test_v07_g6_tester_r1_docker import (  # noqa: F401
    V1,
    _docker_ready,
    _entries,
    alice,
    settings,
)

pytestmark = [
    pytest.mark.component,
    # T-13 (pre-G6, minor): a cancel before staging leaves fetch_bundle() un-awaited
    # (see tests/unit/agent/test_v07_g6_tester_r2_step_unawaited.py); only that
    # warning is let through here, so the rest of the test can run to its end.
    pytest.mark.filterwarnings(
        "ignore:coroutine 'fetch_bundle' was never awaited:RuntimeWarning",
        "ignore::pytest.PytestUnraisableExceptionWarning",
    ),
    pytest.mark.asyncio,
    pytest.mark.timeout(900),
    pytest.mark.docker,
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers"),
]

ITERATIONS = 14


def _ps_a() -> set[str]:
    out = subprocess.run(
        ["docker", "ps", "-a", "--no-trunc", "--format", "{{.ID}}"],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return set(out.split())


def _labelled() -> list[str]:
    out = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=meshforge.managed=1",
            "--format",
            "{{.Names}} {{.Status}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return out.splitlines()


async def test_cancel_at_random_moments_leaves_no_container_socket_or_folder(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    if not await asyncio.to_thread(_docker_ready):
        pytest.skip("Docker or the pinned python-cpu image is not available here")
    rnd = random.Random(1013)
    before = await asyncio.to_thread(_ps_a)
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", ResourceWarning)
        agent = await ExecAgent.start(
            grpc_target,
            tmp_path / "agent",
            agent_overrides={
                "policy": {"accept_jobs": True, "allow_unsandboxed": False}
            },
            docker=True,
        )
        try:
            base = agent.executor.vault.base
            project = tmp_path / "proj"
            project.mkdir()
            (project / "main.py").write_text(
                "import sys, time\n"
                "print(open('/run/secrets/ONE','rb').read()[:3], flush=True)\n"
                "print('.' * 100, flush=True)\n"
                "time.sleep(float(sys.argv[1]))\n"
            )
            seen_states: set[str] = set()
            # T-14 (major, own deterministic test in
            # test_v07_g6_tester_r2_secret_folder_cancel.py): a job cancelled before
            # the agent ever started it can leave its secrets folder behind. Those
            # attempts are named here and excluded from the folder check below, so
            # this test keeps measuring what it is about (containers, sockets, and
            # the folders of attempts that ran).
            known_t14: set[str] = set()
            for i in range(ITERATIONS):
                manifest = {
                    "name": f"r2-cancel-{i}",
                    "entrypoint": ["python", "main.py", "60"],
                    "secrets": ["ONE"],
                    "retry": {"max_attempts": 1},
                }
                timeout_case = i % 7 == 6
                if timeout_case:
                    manifest["limits"] = {"timeout": 4}
                job_id = await submit(alice, project, agent.node_id, manifest)
                await asyncio.sleep(
                    rnd.choice([0.0, 0.05, 0.2, 0.5, 1.0, 1.5, 2.5, 3.5])
                )
                if not timeout_case:
                    await alice.post(f"/v1/jobs/{job_id}/cancel")
                job = await wait_job(alice, job_id, timeout_s=120)
                seen_states.add(job["state"])
                assert job["state"] in ("cancelled", "failed"), job
                if timeout_case:
                    assert job["state"] == "failed", job
                for att in job["attempts"]:
                    if att.get("started_at") is None:
                        known_t14.add(att["attempt_id"])

                def others() -> list[str]:
                    return [e for e in _entries(base) if e not in known_t14]

                for _ in range(150):
                    if not await asyncio.to_thread(others):
                        break
                    await asyncio.sleep(0.1)
                assert others() == [], (i, others())
            # every container is gone, in every state
            left: set[str] = set()
            for _ in range(150):
                left = (await asyncio.to_thread(_ps_a)) - before
                if not left:
                    break
                await asyncio.sleep(0.2)
            assert left == set(), (left, await asyncio.to_thread(_labelled))
            assert await asyncio.to_thread(_labelled) == []
        finally:
            await agent.stop()
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0.05)
    leaked = [str(w.message) for w in caught if "docker.sock" in str(w.message)]
    assert leaked == [], leaked
    # the value is not on the agent's disk either
    for path in (tmp_path / "agent").rglob("*"):
        if path.is_file() and path.stat().st_size < 5_000_000:
            assert V1 not in path.read_bytes(), path
