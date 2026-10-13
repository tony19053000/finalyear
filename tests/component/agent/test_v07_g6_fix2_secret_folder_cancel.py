"""V0.7 G6 fix round 2 (D163, T-14): the wipe wins against a write that ends
after the cancel.

The tester's test holds the worker thread AFTER the files exist. Here the
thread is held BEFORE it writes anything: when the stop arrives there is no
folder yet, so a wipe that does not wait for the thread finds nothing, and
the files appear afterwards. The executor must wait for the write, then wipe.
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import sys
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
from mf_exec_harness import ExecAgent, submit, wait_job
from test_v07_g6_tester_r1_jobs import (  # noqa: F401
    alice,
    alice_token,
    settings,
)

from meshforge.agent.execution.secret_files import SecretVault

pytestmark = [
    pytest.mark.component,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]


async def test_a_write_that_starts_after_the_cancel_is_still_wiped(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = b"t14-late-write-secret-0123456789"
    assert (await alice.put("/v1/secrets/ONE", content=value)).is_success
    real = SecretVault.materialise
    real_wipe = SecretVault.wipe
    entered = threading.Event()
    release = threading.Event()
    written = threading.Event()
    order: list[str] = []

    def late(self: SecretVault, *args: Any, **kwargs: Any) -> Path:
        entered.set()
        release.wait(30)  # the cancel arrives before a single byte is written
        folder = real(self, *args, **kwargs)
        assert (folder / "ONE").read_bytes() == value
        order.append("written")
        written.set()
        return folder

    def wipe(self: SecretVault, folder: Path | None) -> None:
        order.append("wipe")
        real_wipe(self, folder)

    monkeypatch.setattr(SecretVault, "materialise", late)
    monkeypatch.setattr(SecretVault, "wipe", wipe)
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )
    try:
        base = agent.executor.vault.base
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text("import time\ntime.sleep(60)\n")
        job_id = await submit(
            alice,
            project,
            agent.node_id,
            {
                "name": "t14-late",
                "entrypoint": ["python", "main.py"],
                "secrets": ["ONE"],
                "retry": {"max_attempts": 1},
            },
        )
        assert await asyncio.to_thread(entered.wait, 30), "materialise never ran"
        await alice.post(f"/v1/jobs/{job_id}/cancel")
        await asyncio.sleep(1.0)  # the executor has seen the stop
        assert order == [], "the wipe must wait for the write it cannot stop"
        release.set()
        assert await asyncio.to_thread(written.wait, 30)
        job = await wait_job(alice, job_id, timeout_s=60)
        assert job["state"] == "cancelled", job
        left: list[str] = ["?"]
        for _ in range(100):  # 10 s
            left = sorted(p.name for p in base.iterdir()) if base.exists() else []
            if not left and "wipe" in order:
                break
            await asyncio.sleep(0.1)
        assert left == [], f"secret folder(s) still on the agent: {left}"
        assert order == ["written", "wipe"]
    finally:
        release.set()
        await agent.stop()
