"""V0.4 G4 end to end on real PostgreSQL + real gRPC + real REST + a real agent.

The job runs through the **subprocess runtime** (every OS, no Docker needed) and
is observed only through ``/v1/jobs`` and the job tables. Side effects that must
happen exactly once (criterion 4) are counted in a file the workload appends to.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
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

from meshforge.agent.execution.fetch import FetchError, fetch_bundle
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.states import AttemptPhase
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

COUNTER_CODE = """\
import os, sys, time
path = sys.argv[1]
with open(path, "a") as fh:
    fh.write("run\\n")
time.sleep(float(sys.argv[2]) if len(sys.argv) > 2 else 0)
print("hello from the job", flush=True)
sys.exit(int(sys.argv[3]) if len(sys.argv) > 3 else 0)
"""


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings, jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0}
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=10,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


@pytest_asyncio.fixture
async def store(pg_settings: Any) -> AsyncIterator[ExecutionStore]:
    engine = create_engine(pg_settings)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


def _project(tmp_path: Path, name: str = "proj") -> Path:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(COUNTER_CODE)
    return project


def _manifest(
    counter: Path, sleep_s: float = 0, exit_code: int = 0, **extra: Any
) -> dict[str, Any]:
    manifest: dict[str, Any] = {
        "name": "g4",
        "entrypoint": ["python", "main.py", str(counter), str(sleep_s), str(exit_code)],
    }
    manifest.update(extra)
    return manifest


def _runs(counter: Path) -> int:
    return counter.read_text().count("run") if counter.exists() else 0


async def test_submit_runs_to_succeeded_with_five_phase_rows(
    http: httpx.AsyncClient, agent: ExecAgent, store: ExecutionStore, tmp_path: Path
) -> None:
    counter = tmp_path / "count.txt"
    started = time.monotonic()
    job_id = await submit(http, _project(tmp_path), agent.node_id, _manifest(counter))
    running = await wait_job(
        http, job_id, states=frozenset({"running", "collecting", "succeeded"})
    )
    dispatch_latency = time.monotonic() - started
    job = await wait_job(http, job_id)
    assert job["state"] == "succeeded", job
    (attempt,) = job["attempts"]
    assert attempt["state"] == "done" and attempt["exit_code"] == 0
    assert _runs(counter) == 1
    phases = {p.phase: p for p in await store.phases(attempt["attempt_id"])}
    assert set(phases) == {p.value for p in AttemptPhase}
    assert not phases["queue"].skipped and not phases["run"].skipped
    assert not phases["stage"].skipped and not phases["collect"].skipped
    # no requirements: the subprocess runtime has no env phase
    assert phases["env"].skipped
    record = await store.get_attempt(attempt["attempt_id"])
    assert record is not None and record.runtime == "subprocess"
    assert record.start_snapshot is not None and record.start_snapshot["version"] == 1
    assert [t["to_state"] for t in job["transitions"]][-4:] == [
        "preparing",
        "running",
        "collecting",
        "succeeded",
    ]
    print(  # noqa: T201 - a measured number for the handoff (run with -s)
        "Laptop 1 (not a benchmark claim): submit->running "
        f"{dispatch_latency:.3f} s ({running['state']})"
    )


async def test_a_non_zero_exit_is_user_error(
    http: httpx.AsyncClient, agent: ExecAgent, tmp_path: Path
) -> None:
    counter = tmp_path / "count.txt"
    job_id = await submit(
        http, _project(tmp_path), agent.node_id, _manifest(counter, exit_code=3)
    )
    job = await wait_job(http, job_id)
    assert job["state"] == "failed" and job["failure_type"] == "user_error"
    assert job["attempts"][0]["exit_code"] == 3


async def test_fetch_refuses_a_wrong_token_and_a_wrong_digest(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    blob = b"bundle bytes"
    import hashlib

    digest = Digest(hashlib.sha256(blob).hexdigest())
    assert (await http.put(f"/v1/blobs/{digest.hex}", content=blob)).status_code == 201
    base = f"http://127.0.0.1:{runtime.http_port}"
    with pytest.raises(FetchError) as wrong_token:
        await fetch_bundle(base, digest, "not-a-token", tmp_path / "a.bundle")
    assert wrong_token.value.failure_type is FailureType.UNAUTHORIZED
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint("att_x", [digest], ttl_s=60).get_secret_value()
    size = await fetch_bundle(base, digest, token, tmp_path / "b.bundle")
    assert size == len(blob) and (tmp_path / "b.bundle").read_bytes() == blob
    runtime.fetch_tokens.revoke("att_x")
    with pytest.raises(FetchError) as expired:
        await fetch_bundle(base, digest, token, tmp_path / "c.bundle")
    assert expired.value.failure_type is FailureType.UNAUTHORIZED
    # a token for another digest cannot read this one
    other = runtime.fetch_tokens.mint(
        "att_y", [Digest("0" * 64)], ttl_s=60
    ).get_secret_value()
    with pytest.raises(FetchError):
        await fetch_bundle(base, digest, other, tmp_path / "d.bundle")
    assert not await asyncio.to_thread(lambda: list(tmp_path.glob("*.part")))


async def test_start_attempt_delivered_twice_and_after_a_reconnect_runs_once(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    store: ExecutionStore,
    tmp_path: Path,
) -> None:
    counter = tmp_path / "count.txt"
    job_id = await submit(
        http, _project(tmp_path), agent.node_id, _manifest(counter, sleep_s=1.5)
    )
    await wait_job(http, job_id, states=frozenset({"running"}))
    (live,) = await store.live_attempts(agent.node_id)
    dispatcher = runtime.dispatcher
    assert dispatcher is not None and runtime.manager is not None
    dispatcher._send_start(live.job, live.attempt, resend=True)  # a second copy
    # and a reconnect while it runs: Reconcile lists it -> it continues
    session = runtime.manager.get(agent.node_id)
    assert session is not None
    await runtime.manager.end_session(session, SessionEndReason.STREAM_LOST)
    job = await wait_job(http, job_id)
    assert job["state"] == "succeeded", job
    assert _runs(counter) == 1
    assert len(job["attempts"]) == 1


async def test_commit_without_send_is_resent_on_reconnect_and_runs_once(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    store: ExecutionStore,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The crash window of D65: the attempt row committed, StartAttempt never left."""
    dispatcher = runtime.dispatcher
    assert dispatcher is not None and runtime.manager is not None
    real = dispatcher._send_start
    dropped: list[str] = []

    def lose_first(job: Any, attempt: Any, *, resend: bool) -> None:
        if not resend and not dropped:
            dropped.append(attempt.id)
            return  # "crashed" between commit and send
        real(job, attempt, resend=resend)

    monkeypatch.setattr(dispatcher, "_send_start", lose_first)
    counter = tmp_path / "count.txt"
    job_id = await submit(http, _project(tmp_path), agent.node_id, _manifest(counter))
    await wait_job(http, job_id, states=frozenset({"preparing"}))
    for _ in range(40):
        if dropped:
            break
        await asyncio.sleep(0.05)
    assert dropped
    session = runtime.manager.get(agent.node_id)
    assert session is not None
    await runtime.manager.end_session(session, SessionEndReason.STREAM_LOST)
    job = await wait_job(http, job_id)
    assert job["state"] == "succeeded", job
    assert _runs(counter) == 1
    assert dispatcher.stats.start_resent >= 1


async def test_cancel_mid_run_ends_cancelled_within_5_s_and_kills_the_process(
    http: httpx.AsyncClient, agent: ExecAgent, tmp_path: Path
) -> None:
    project = _project(tmp_path)
    (project / "main.py").write_text(
        "import os, sys, time\n"
        "open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "print('ready', flush=True)\n"
        "time.sleep(120)\n"
    )
    pid_file = tmp_path / "pid.txt"
    job_id = await submit(
        http,
        project,
        agent.node_id,
        {"name": "c", "entrypoint": ["python", "main.py", str(pid_file)]},
    )
    await wait_job(http, job_id, states=frozenset({"running"}))
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    started = time.monotonic()
    resp = await http.post(f"/v1/jobs/{job_id}/cancel")
    assert resp.status_code == 202, resp.text
    job = await wait_job(http, job_id, timeout_s=10)
    took = time.monotonic() - started
    assert job["state"] == "cancelled", job
    assert took <= 5.0, took
    assert not _alive(pid)
    again = await http.post(f"/v1/jobs/{job_id}/cancel")
    assert again.status_code == 200 and again.json()["state"] == "cancelled"
    print(f"Laptop 1 (not a benchmark claim): cancel -> cancelled {took:.3f} s")  # noqa: T201


def _alive(pid: int) -> bool:
    if sys.platform == "win32":  # pragma: no cover
        import psutil

        return bool(psutil.pid_exists(pid))
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_timeout_is_deadline_exceeded_within_2_s_of_the_limit(
    http: httpx.AsyncClient, agent: ExecAgent, tmp_path: Path
) -> None:
    counter = tmp_path / "count.txt"
    job_id = await submit(
        http,
        _project(tmp_path),
        agent.node_id,
        _manifest(counter, sleep_s=60, limits={"timeout": 2}),
    )
    await wait_job(http, job_id, states=frozenset({"running"}))
    started = time.monotonic()
    job = await wait_job(http, job_id, timeout_s=15)
    overshoot = time.monotonic() - started - 2.0
    assert job["state"] == "failed" and job["failure_type"] == "deadline_exceeded", job
    assert overshoot <= 2.0, overshoot
    print(f"Laptop 1 (not a benchmark claim): timeout overshoot {overshoot:.3f} s")  # noqa: T201


async def test_queued_job_is_cancelled_at_once(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    assert runtime.dispatcher is not None
    counter = tmp_path / "count.txt"
    first = await submit(
        http, _project(tmp_path, "a"), agent.node_id, _manifest(counter, sleep_s=3)
    )
    await wait_job(http, first, states=frozenset({"running"}))
    second = await submit(
        http, _project(tmp_path, "b"), agent.node_id, _manifest(counter)
    )
    body = (await http.get(f"/v1/jobs/{second}")).json()
    assert body["state"] == "queued"  # one attempt per node at a time
    resp = await http.post(f"/v1/jobs/{second}/cancel")
    assert resp.status_code == 202 and resp.json()["state"] == "cancelled"
    assert (await http.post("/v1/jobs/job_notanid/cancel")).status_code == 400


async def test_agent_stop_mid_run_reports_agent_shutdown_then_goodbye(
    http: httpx.AsyncClient,
    agent: ExecAgent,
    runtime: ControllerRuntime,
    tmp_path: Path,
) -> None:
    counter = tmp_path / "count.txt"
    job_id = await submit(
        http, _project(tmp_path), agent.node_id, _manifest(counter, sleep_s=60)
    )
    await wait_job(http, job_id, states=frozenset({"running"}))
    await agent.stop()
    job = await wait_job(http, job_id, timeout_s=10)
    assert job["state"] == "failed" and job["failure_type"] == "node_lost", job
    assert job["attempts"][0]["detail"] == "agent_shutdown"
    node = (await http.get(f"/v1/nodes/{agent.node_id}")).json()
    assert node["state"] == "offline"


async def test_backstop_fails_an_attempt_whose_agent_hangs(
    grpc_target: str, http: httpx.AsyncClient, tmp_path: Path, data_dir: Path
) -> None:
    """Injected: a runtime that ignores its own time limit (a wedged agent)."""
    from meshforge.agent.execution.offer import ExecutionSetup
    from meshforge.agent.execution.runtime import RunObserver, RuntimeAvailability
    from meshforge.agent.execution.selection import execution_offer

    class Hanging:
        name = "subprocess"

        async def probe(self) -> RuntimeAvailability:
            return RuntimeAvailability("subprocess", True)

        async def run(
            self, spec: Any, on_output: Any, *, observer: RunObserver | None = None
        ) -> Any:
            assert observer is not None
            observer.enter_run()
            await asyncio.Event().wait()  # never honours spec.limits.timeout_s

    offer = execution_offer(
        enabled=True,
        allow_unsandboxed=True,
        docker=None,
        subprocess=RuntimeAvailability("subprocess", True),
    )
    setup = ExecutionSetup(offer=offer, runtimes={"subprocess": Hanging()})  # type: ignore[dict-item]
    hung = await ExecAgent.start(grpc_target, data_dir, setup=setup)
    try:
        counter = tmp_path / "count.txt"
        job_id = await submit(
            http,
            _project(tmp_path),
            hung.node_id,
            _manifest(counter, limits={"timeout": 1}),
        )
        job = await wait_job(http, job_id, timeout_s=15)
        assert job["state"] == "failed" and job["failure_type"] == "deadline_exceeded"
        assert job["attempts"][0]["detail"] == "controller_backstop"
    finally:
        await hung.stop()


def _docker_ready() -> bool:
    try:
        from meshforge.agent.execution.docker_support import open_client
        from meshforge.agent.settings import PYTHON_CPU_IMAGE

        client = open_client(10)
    except Exception:  # noqa: BLE001
        return False
    try:
        client.images.get(PYTHON_CPU_IMAGE)
    except Exception:  # noqa: BLE001
        return False
    finally:
        client.close()
    return True


@pytest.mark.docker
async def test_docker_runtime_end_to_end(
    grpc_target: str, http: httpx.AsyncClient, tmp_path: Path, data_dir: Path
) -> None:
    """The same path through the sandbox: the job cannot see the counter file on
    the host, so it prints instead and the test reads the job's own result."""
    if not await asyncio.to_thread(_docker_ready):
        pytest.skip("Docker or the pinned python-cpu image is not available here")
    docker_agent = await ExecAgent.start(
        grpc_target, data_dir, execution={"allow_unsandboxed": False}, docker=True
    )
    try:
        assert docker_agent.setup.runtimes.keys() == {"docker"}
        project = tmp_path / "dproj"
        project.mkdir()
        (project / "main.py").write_text(
            "import sys; print('in docker'); sys.exit(0)\n"
        )
        job_id = await submit(
            http,
            project,
            docker_agent.node_id,
            {"name": "d", "entrypoint": ["python", "main.py"]},
        )
        job = await wait_job(http, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        assert job["attempts"][0]["exit_code"] == 0
    finally:
        await docker_agent.stop()
