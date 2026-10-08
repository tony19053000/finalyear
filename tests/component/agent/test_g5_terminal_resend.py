"""V0.4 G5 D75 end to end: lost final reports, released attempts.

Real PostgreSQL + real gRPC + real REST + a real agent (subprocess runtime).

* The controller drops the first ``DONE`` it receives (as a full status queue
  would: not applied, not acked). The agent re-sends it after
  ``terminal_resend_s`` and the job ends ``succeeded`` - never
  ``deadline_exceeded`` via the backstop, never stuck in ``collecting``.
* A report for an attempt the controller already ended (``LOST``) is acked and
  answered ``CancelAttempt{release}``: the agent frees it and its workdir.
* A Reconcile listing an attempt the controller does not know releases it too
  (the running workload is stopped, the workdir freed).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of
from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

SLEEP_CODE = """\
import sys, time
with open(sys.argv[1], "a") as fh:
    fh.write("run\\n")
time.sleep(float(sys.argv[2]))
print("done", flush=True)
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings, jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0}
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(
        grpc_target, data_dir, execution={"terminal_resend_s": 0.5}
    )
    yield running
    await running.stop()


def _job(tmp_path: Path, sleep_s: float) -> tuple[Path, dict[str, Any], Path]:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(SLEEP_CODE)
    counter = tmp_path / "count.txt"
    manifest = {
        "name": "d75",
        "entrypoint": ["python", "main.py", str(counter), str(sleep_s)],
        "limits": {"timeout": "4s"},
    }
    return project, manifest, counter


async def _until(predicate: Any, timeout_s: float = 10.0) -> None:
    for _ in range(int(timeout_s / 0.05)):
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_a_dropped_final_report_is_resent_and_the_job_succeeds(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manager, dispatcher = runtime.manager, runtime.dispatcher
    assert manager is not None and dispatcher is not None
    real = manager.dispatch.handler_for("attempt_status")
    dropped: list[str] = []

    async def drop_first_done(live: Any, msg: agent_pb2.AgentMessage) -> None:
        if msg.attempt_status.state == common_pb2.ATTEMPT_STATE_DONE and not dropped:
            dropped.append(msg.message_id)  # not applied, not acked
            return
        await real(live, msg)

    monkeypatch.setitem(manager.dispatch._handlers, "attempt_status", drop_first_done)
    project, manifest, counter = _job(tmp_path, 0.2)
    job_id = await submit(http, project, agent.node_id, manifest)
    job = await wait_job(http, job_id, timeout_s=15)
    assert dropped, "the first DONE never reached the controller"
    assert job["state"] == "succeeded", job
    (attempt,) = job["attempts"]
    assert attempt["state"] == "done" and attempt["failure_type"] is None
    assert agent.executor.terminal_resends >= 1
    assert counter.read_text().count("run") == 1
    await _until(lambda: agent.executor.held() == [])


async def test_a_report_for_a_lost_attempt_is_acked_and_the_attempt_released(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    project, manifest, counter = _job(tmp_path, 1.5)
    # V0.6 G3: a loss is retried by default; this test is about the release
    # of the lost attempt, so the job opts out (D117 on_lost: false).
    manifest["retry"] = {"on_lost": False}
    job_id = await submit(http, project, agent.node_id, manifest)
    await wait_job(http, job_id, states=frozenset({"running"}))
    (live,) = await runtime.jobs.live_attempts(agent.node_id)
    workdir = agent.executor.work_root / live.attempt.id
    assert workdir.is_dir()
    # The controller ends it on its own (as after an OFFLINE it wrongly saw).
    await runtime.jobs.end_attempt(
        live.attempt.id,
        live.attempt.fencing_token,
        event=AttemptEvent.LOSE,
        failure_type=FailureType.NODE_LOST,
        detail="test",
    )
    await _until(lambda: agent.executor.held() == [])
    await _until(lambda: not workdir.exists())
    job = await wait_job(http, job_id)
    assert job["state"] == "failed" and job["failure_type"] == "node_lost"
    assert runtime.dispatcher is not None
    assert runtime.dispatcher.stats.releases_sent >= 1
    assert counter.read_text().count("run") == 1


async def test_a_reconcile_listing_an_unknown_attempt_releases_it(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    manager = runtime.manager
    assert manager is not None
    project, manifest, _ = _job(tmp_path, 60)
    job_id = await submit(http, project, agent.node_id, manifest)
    await wait_job(http, job_id, states=frozenset({"running"}))
    (live,) = await runtime.jobs.live_attempts(agent.node_id)
    workdir = agent.executor.work_root / live.attempt.id
    # The controller forgets the attempt entirely (a restored backup, say).
    async with runtime.jobs._sessions() as db, db.begin():
        await db.execute(
            text("DELETE FROM attempt_phase WHERE attempt_id = :a"),
            {"a": live.attempt.id},
        )
        await db.execute(
            text("DELETE FROM attempt WHERE id = :a"), {"a": live.attempt.id}
        )
    session = manager.get(agent.node_id)
    assert session is not None
    await manager.end_session(session, SessionEndReason.STREAM_LOST)
    await _until(lambda: agent.executor.held() == [], timeout_s=15)
    await _until(lambda: not workdir.exists())
