"""V0.7 G6 tester r2 (T-14, MAJOR): a job cancelled while its secret files are
being written leaves the secrets folder (the plaintext values) on the agent.

`Executor._run` assigns `folder = await self._materialise(...)`; the write runs
in a worker thread under `_step`. A stop that arrives during the write cancels
the await, the thread still finishes and creates `<base>/<attempt>/NAME`, but
`folder` was never assigned, so the `finally` does not wipe it. Found by
cancelling Docker jobs at random moments: the folder (file `ONE`) was still
there 60 s after the job was `cancelled`, with no attempt left in the executor.
Same bug class as the Docker `to_thread` result dropped on cancel (4a79155).
"""

# ruff: noqa: F811, ASYNC251
from __future__ import annotations

import asyncio
import sys
import threading
import time
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


@pytest.mark.xfail(
    strict=True,
    reason="T-14 (major): a stop during the secret-file write leaves the folder "
    "(plaintext values) on the agent: `folder` is assigned only after the await",
)
async def test_a_cancel_during_the_secret_file_write_still_wipes_the_folder(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    value = b"t14-cancel-secret-0123456789"
    assert (await alice.put("/v1/secrets/ONE", content=value)).is_success
    real = SecretVault.materialise
    started = threading.Event()
    release = threading.Event()

    def slow(self: SecretVault, *args: Any, **kwargs: Any) -> Path:
        folder = real(self, *args, **kwargs)  # the files exist now
        started.set()
        release.wait(30)  # the cancel arrives while we are "still writing"
        return folder

    monkeypatch.setattr(SecretVault, "materialise", slow)
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
                "name": "t14",
                "entrypoint": ["python", "main.py"],
                "secrets": ["ONE"],
                "retry": {"max_attempts": 1},
            },
        )
        assert await asyncio.to_thread(started.wait, 30), "materialise never ran"
        await alice.post(f"/v1/jobs/{job_id}/cancel")
        await asyncio.sleep(1.0)  # the executor has seen the stop
        release.set()
        job = await wait_job(alice, job_id, timeout_s=60)
        assert job["state"] == "cancelled", job
        left: list[str] = []
        for _ in range(100):  # 10 s
            left = sorted(p.name for p in base.iterdir()) if base.exists() else []
            if not left:
                break
            await asyncio.sleep(0.1)
        assert left == [], f"secret folder(s) still on the agent: {left}"
        for path in base.rglob("*") if base.exists() else []:
            assert not path.is_file() or value not in path.read_bytes(), path
    finally:
        release.set()
        await agent.stop()
        time.sleep(0)
