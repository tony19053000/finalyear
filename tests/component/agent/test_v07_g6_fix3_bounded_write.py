"""V0.7 G6 fix round 3, T-19 (D164): the wait for the secret write thread is
bounded, and the wipe still happens after the write.

A filesystem call in the write thread hangs (forced). Real mTLS controller,
PostgreSQL and a real agent in this loop, ``secret_write_wait_s = 1``:

* a cancel is terminal within the bound (+ a small margin), with exactly one
  warning ``agent.execution.secret_write_pending``; when the write returns
  later, the write thread itself wipes the folder (order: written, wipe);
* ``agent.stop()`` returns within its bounds without waiting for the hung
  write - and the folder is still wiped when the write returns, although the
  agent is gone by then;
* a write that ends within the bound is joined: when ``agent.stop()``
  returns, the folder has been written AND wiped already.
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import structlog
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

VALUE = b"t19-bounded-write-secret-0123456789"
BOUND_S = 1.0
MARGIN_S = 3.0
PENDING = "agent.execution.secret_write_pending"


class Held:
    """``SecretVault.materialise`` held before it writes; wipes recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, hold_s: float) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.written = threading.Event()
        self.order: list[str] = []
        real, real_wipe = SecretVault.materialise, SecretVault.wipe
        held = self

        def late(vault: SecretVault, *args: Any, **kwargs: Any) -> Path:
            held.entered.set()
            held.release.wait(hold_s)
            folder = real(vault, *args, **kwargs)
            assert (folder / "ONE").read_bytes() == VALUE
            held.order.append("written")
            held.written.set()
            return folder

        def wipe(vault: SecretVault, folder: Path | None) -> None:
            real_wipe(vault, folder)
            held.order.append("wipe")

        monkeypatch.setattr(SecretVault, "materialise", late)
        monkeypatch.setattr(SecretVault, "wipe", wipe)


async def _started(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path, bound_s: float
) -> tuple[ExecAgent, str]:
    assert (await alice.put("/v1/secrets/ONE", content=VALUE)).is_success
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        execution={"secret_write_wait_s": bound_s},
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text("import time\ntime.sleep(60)\n")
    job_id = await submit(
        alice,
        project,
        agent.node_id,
        {
            "name": "t19-bounded",
            "entrypoint": ["python", "main.py"],
            "secrets": ["ONE"],
            "retry": {"max_attempts": 1},
        },
    )
    return agent, job_id


def _left(base: Path) -> list[str]:
    return sorted(p.name for p in base.iterdir()) if base.exists() else []


async def _wiped_by_the_thread(held: Held, base: Path) -> None:
    """The write returns now; its own thread must wipe what it wrote."""
    held.release.set()
    assert await asyncio.to_thread(held.written.wait, 30)
    for _ in range(100):
        if "wipe" in held.order and not _left(base):
            break
        await asyncio.sleep(0.05)
    assert held.order == ["written", "wipe"]
    assert _left(base) == []


@pytest.mark.security
async def test_a_cancel_waits_for_a_hung_write_only_for_the_bound(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = Held(monkeypatch, hold_s=120)
    agent, job_id = await _started(alice, grpc_target, tmp_path, BOUND_S)
    base = agent.executor.vault.base
    try:
        assert await asyncio.to_thread(held.entered.wait, 30), "materialise never ran"
        with structlog.testing.capture_logs() as logs:
            started = time.monotonic()
            await alice.post(f"/v1/jobs/{job_id}/cancel")
            job = await wait_job(alice, job_id, timeout_s=60)
            took = time.monotonic() - started
        assert job["state"] == "cancelled", job
        assert BOUND_S * 0.9 <= took <= BOUND_S + MARGIN_S, took
        pending = [entry for entry in logs if entry["event"] == PENDING]
        assert len(pending) == 1, pending
        assert pending[0]["log_level"] == "warning"
        assert pending[0]["bound_s"] == BOUND_S
        assert pending[0]["job_id"] == job_id and pending[0]["attempt_id"]
        assert VALUE.decode() not in repr(logs)
        assert held.order == [] and _left(base) == []  # nothing written yet
        await _wiped_by_the_thread(held, base)
    finally:
        held.release.set()
        await agent.stop()
    assert _left(base) == []


@pytest.mark.security
async def test_agent_stop_does_not_wait_for_a_hung_write_and_it_is_still_wiped(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = Held(monkeypatch, hold_s=120)
    agent, _job_id = await _started(alice, grpc_target, tmp_path, BOUND_S)
    base = agent.executor.vault.base
    try:
        assert await asyncio.to_thread(held.entered.wait, 30), "materialise never ran"
        with structlog.testing.capture_logs() as logs:
            started = time.monotonic()
            await asyncio.wait_for(agent.stop(), timeout=60)
            took = time.monotonic() - started
        # the attempt's own stop bound is grace_s (1) + 3; the wipe's is 1
        assert took <= 4.0 + MARGIN_S, took
        assert len([entry for entry in logs if entry["event"] == PENDING]) == 1
        assert agent.executor._wipes == set()  # joined, not orphaned
        assert held.order == [] and _left(base) == []
        # the agent is gone; the write returns; nobody but its thread is left
        await _wiped_by_the_thread(held, base)
    finally:
        held.release.set()


@pytest.mark.security
async def test_agent_stop_joins_a_write_that_ends_within_the_bound(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attempt's task is given up after ``grace_s + 3`` = 4 s (this
    harness stops the executor twice: 8 s) and cancelled; the write returns at
    10 s, inside the wipe's bound (20 s): ``stop()`` waits for that wipe."""
    held = Held(monkeypatch, hold_s=10.0)
    agent, _job_id = await _started(alice, grpc_target, tmp_path, 20.0)
    base = agent.executor.vault.base
    try:
        assert await asyncio.to_thread(held.entered.wait, 30), "materialise never ran"
        with structlog.testing.capture_logs() as logs:
            started = time.monotonic()
            await asyncio.wait_for(agent.stop(), timeout=60)
            took = time.monotonic() - started
        # no polling: at the moment stop() returns it is written AND wiped
        assert held.order == ["written", "wipe"], (took, held.order)
        assert _left(base) == []
        assert 9.5 <= took <= 10.0 + MARGIN_S, took
        assert [entry for entry in logs if entry["event"] == PENDING] == []
    finally:
        held.release.set()
