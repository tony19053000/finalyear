"""V0.7 G6 fix round 3, T-22 (D164), end to end: a log redactor that failed
closed is visible to the operator and to the user.

Real mTLS controller, PostgreSQL and a real agent (subprocess runtime) in this
loop. A matcher loop guard is forced on ONE side at a time (the agent's
redactors are the growing style, the controller's the length-preserving one):

* agent side: one warning ``agent.execution.log_redaction_failed_closed`` per
  stream with ids only; ``GET /v1/jobs/<id>/logs`` shows the note as a line
  of its own between the withheld ``*``;
* controller side: one warning ``controller.logs.redaction_failed_closed`` per
  stream; the stored log starts with the note written over the first withheld
  bytes and has exactly the length the agent sent;
* either way the job itself succeeds, and no value or form is anywhere.
"""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import structlog

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.redaction_notice import FAIL_CLOSED_NOTE
from meshforge.common.secret_redaction import (
    RedactionStallError,
    StreamRedactor,
    encoded_forms,
)
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX"),
]

VALUE = b"fail-closed-e2e-secret-0123456789"
WORKLOAD = r"""
import os, sys
value = open(os.path.join(os.environ["MESHFORGE_SECRETS_DIR"], "API_KEY")).read()
for i in range(20):
    print("out line", i, value, flush=True)
    print("err line", i, value.encode().hex(), file=sys.stderr, flush=True)
"""
AGENT_EVENT = "agent.execution.log_redaction_failed_closed"
CONTROLLER_EVENT = "controller.logs.redaction_failed_closed"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def alice(
    pg_settings: DatabaseSettings, runtime: ControllerRuntime
) -> AsyncIterator[httpx.AsyncClient]:
    engine: AsyncEngine = create_engine(pg_settings)
    try:
        await create_user(engine, "alice", "alice-password-1")
    finally:
        await engine.dispose()
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        token = await login(anon, "alice", "alice-password-1")
    async with httpx.AsyncClient(
        base_url=base, headers=bearer(token), timeout=30
    ) as client:
        assert (await client.put("/v1/secrets/API_KEY", content=VALUE)).is_success
        yield client


def _stall(monkeypatch: pytest.MonkeyPatch, *, length_preserving: bool) -> None:
    """Force the loop guard in every redactor of one style."""
    real = StreamRedactor._read

    def read(self: StreamRedactor, end: int) -> None:
        if self._preserve is length_preserving:
            raise RedactionStallError("injected")
        real(self, end)

    monkeypatch.setattr(StreamRedactor, "_read", read)


async def _run(
    alice: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> tuple[dict[str, bytes], list[Any], dict[str, Any]]:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(WORKLOAD)
    agent = await ExecAgent.start(
        grpc_target,
        tmp_path / "agent",
        agent_overrides={"policy": {"accept_jobs": True, "allow_unsandboxed": True}},
    )
    try:
        with structlog.testing.capture_logs() as logs:
            job_id = await submit(
                alice,
                project,
                None,
                {
                    "name": "g6-fix3-t22",
                    "entrypoint": ["python", "main.py"],
                    "secrets": ["API_KEY"],
                    "retry": {"max_attempts": 1},
                },
            )
            job = await wait_job(alice, job_id, timeout_s=60)
        stored = {}
        for stream in ("stdout", "stderr"):
            reply = await alice.get(
                f"/v1/jobs/{job_id}/logs", params={"stream": stream}
            )
            assert reply.is_success
            stored[stream] = reply.content
    finally:
        await agent.stop()
    return stored, logs, job


def _no_value(stored: dict[str, bytes], logs: list[Any]) -> None:
    for blob in (*stored.values(), repr(logs).encode()):
        for form in encoded_forms(VALUE):
            assert form not in blob


def _warned(logs: list[Any], event: str) -> list[Any]:
    return [entry for entry in logs if entry["event"] == event]


@pytest.mark.security
async def test_an_agent_side_stall_is_logged_once_per_stream_and_noted_in_the_log(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stall(monkeypatch, length_preserving=False)
    stored, logs, job = await _run(alice, grpc_target, tmp_path)
    assert job["state"] == "succeeded", job
    _no_value(stored, logs)
    for blob in stored.values():
        assert blob.count(b"\n" + FAIL_CLOSED_NOTE + b"\n") == 1, blob[:200]
        rest = blob.replace(b"\n" + FAIL_CLOSED_NOTE + b"\n", b"")
        assert rest and set(rest) == {ord("*")}
        assert blob.startswith(b"*")  # the note follows the first withheld bytes
    warned = _warned(logs, AGENT_EVENT)
    assert sorted(entry["stream"] for entry in warned) == ["stderr", "stdout"]
    for entry in warned:
        assert entry["log_level"] == "warning"
        assert entry["job_id"] == job["job_id"]
        assert entry["attempt_id"] and entry["node_id"]
        assert set(entry) == {
            "event",
            "log_level",
            "job_id",
            "attempt_id",
            "node_id",
            "stream",
        }
    assert _warned(logs, CONTROLLER_EVENT) == []  # its own matcher is healthy


@pytest.mark.security
async def test_a_controller_side_stall_is_logged_once_per_stream_and_noted_in_place(
    alice: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stall(monkeypatch, length_preserving=True)
    stored, logs, job = await _run(alice, grpc_target, tmp_path)
    assert job["state"] == "succeeded", job
    _no_value(stored, logs)
    # what the (healthy) agent sent: the values already as [secret:API_KEY]
    sent = {
        "stdout": sum(len(f"out line {i} [secret:API_KEY]\n") for i in range(20)),
        "stderr": sum(len(f"err line {i} [secret:API_KEY]\n") for i in range(20)),
    }
    for stream, blob in stored.items():
        assert len(blob) == sent[stream]  # offsets untouched
        assert blob.startswith(FAIL_CLOSED_NOTE)
        assert set(blob[len(FAIL_CLOSED_NOTE) :]) == {ord("*")}
    warned = _warned(logs, CONTROLLER_EVENT)
    assert sorted(entry["stream"] for entry in warned) == ["stderr", "stdout"]
    for entry in warned:
        assert entry["job_id"] == job["job_id"] and entry["log_level"] == "warning"
    assert _warned(logs, AGENT_EVENT) == []
