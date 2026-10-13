# ruff: noqa: F811, T201
"""V0.7 G6 tester r3 (T-17): the matcher-spin inputs through a REAL DOCKER job
(controller + agent in this process, the container is real). Job finishes, the
stored log has no form, the loop keeps answering (lag sampled while it runs)."""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_redaction_oracle import CASES, MAIN, forms_of
from test_v07_g6_tester_r1_docker import _docker_ready, alice, settings  # noqa: F401

pytestmark = [
    pytest.mark.component,
    pytest.mark.asyncio,
    pytest.mark.docker,
    pytest.mark.timeout(600),
    pytest.mark.skipif(sys.platform == "win32", reason="Linux containers"),
]


async def test_t17_inputs_in_a_docker_job(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    if not await asyncio.to_thread(_docker_ready):
        pytest.skip("Docker or the pinned python-cpu image is not available here")
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": False}},
        docker=True,
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(MAIN)
    lags: list[float] = []
    stop = asyncio.Event()

    async def sampler() -> None:
        while not stop.is_set():
            t0 = time.monotonic()
            await asyncio.sleep(0.05)
            lags.append(time.monotonic() - t0 - 0.05)

    task = asyncio.create_task(sampler())
    try:
        for case, secrets in CASES.items():
            named = {f"{case.upper()}_{k}": v for k, v in secrets.items()}
            for name, value in named.items():
                assert (
                    await alice.put(f"/v1/secrets/{name}", content=value)
                ).is_success
            job_id = await submit(
                alice,
                project,
                agent.node_id,
                {
                    "name": f"d-{case}",
                    "entrypoint": ["python", "main.py", case],
                    "secrets": sorted(named),
                    "retry": {"max_attempts": 1},
                },
            )
            job = await wait_job(alice, job_id, timeout_s=180)
            assert job["state"] == "succeeded", (case, job)
            got = await alice.get(
                f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"}
            )
            body = got.content
            for name, value in named.items():
                cleaned = body.replace(f"[secret:{name}]".encode(), b"")
                for form in forms_of(value):
                    assert form not in cleaned, (case, name, form)
            assert b"[secret:" in body and b"*" not in body, (case, body[:200])
            print("docker", case, len(body))
    finally:
        stop.set()
        await task
        await agent.stop()
    print(f"worst loop lag {max(lags):.3f}s over {len(lags)} samples")
    assert max(lags) < 2.0
