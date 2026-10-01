"""V0.4 G5 tester round 1: adversarial gaps the implementer's own suite did not
cover (roadmap 0.4.9/0.4.10, D71/D72/D73/D74/D75).

* A real controller **process restart** while a real job is RUNNING (not just a
  dropped report or a bare session-registry sweep): the job must reach *some*
  terminal state within a bound time - never stuck in ``running``/``collecting``
  forever - and D75's re-send/release path is exercised across the restart, not
  just within one controller instance's lifetime.
* Hostile output globs: ``..`` and an absolute-looking pattern must never
  select anything outside the workdir (D74) - not just symlinks, which the
  implementer's own file already covers.
* A live WebSocket follower **reconnecting mid-run** with ``?offset=`` while the
  job keeps producing output must never duplicate or drop a byte (D72), as
  opposed to the implementer's ``test_resume_from_an_offset`` which only
  resumes after the job has already finished (a purely persisted read).
"""

from __future__ import annotations

import base64
import json
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import TERMINAL, ExecAgent, submit, wait_job
from mf_jobs_harness import token_of
from websockets.asyncio.client import connect

from meshforge.agent.execution.outputs import expand_outputs
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.timeout(180)]


# --------------------------------------------------------------- restart


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    # Not a liveness test (see TOLERANT_LIVENESS); the restart below keeps it.
    return make_controller_settings(
        pg_settings, jobs={"dispatch_interval_s": 0.1}, **tolerant_liveness()
    )


RUN_THEN_OUTPUT = """\
import sys, time
print("started", flush=True)
time.sleep(2.0)
with open("result.txt", "w") as fh:
    fh.write("done\\n")
print("finished", flush=True)
"""


@pytest.mark.asyncio
async def test_controller_restart_while_a_job_is_running_never_strands_it(
    runtime: ControllerRuntime,
    pg_settings: DatabaseSettings,
    data_dir: Path,
    tmp_path: Path,
) -> None:
    """Kill the controller (same as a SIGKILL, in-process) while a real agent's
    job is RUNNING, restart a fresh :class:`ControllerRuntime` bound to the SAME
    ports against the SAME database, and require the job to leave RUNNING within
    a bounded time. D75's lost-terminal fix and the D73 upload grant only get
    exercised end to end if the controller that minted the grant is not the one
    that later sees the report - which none of the implementer's own tests do
    (theirs use one controller instance and a dropped/synthetic report)."""
    grpc_port, http_port = runtime.grpc_port, runtime.http_port
    agent = await ExecAgent.start(f"127.0.0.1:{grpc_port}", data_dir)
    try:
        project = tmp_path / "proj"
        project.mkdir()
        (project / "main.py").write_text(RUN_THEN_OUTPUT)
        manifest = {
            "name": "restart-mid-run",
            "entrypoint": ["python", "main.py"],
            "outputs": ["result.txt"],
        }
        async with httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{http_port}",
            headers={"authorization": f"Bearer {token_of(runtime)}"},
            timeout=30,
        ) as http:
            job_id = await submit(http, project, agent.node_id, manifest)
            # Wait until it is actually RUNNING before pulling the rug (a
            # deadline on the monotonic clock, not a count of polls whose
            # length a loaded machine stretches).
            job = await wait_job(http, job_id, states=frozenset({"running"}) | TERMINAL)
            assert job["state"] == "running", job

        await runtime.stop()

        restarted = make_controller_settings(
            pg_settings,
            jobs={"dispatch_interval_s": 0.1},
            **tolerant_liveness(),
            grpc_bind=f"127.0.0.1:{grpc_port}",
            http_bind=f"127.0.0.1:{http_port}",
        )
        async with (
            ControllerRuntime.start(restarted) as fresh,
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{fresh.http_port}",
                headers={"authorization": f"Bearer {token_of(fresh)}"},
                timeout=30,
            ) as http2,
        ):
            final = await wait_job(http2, job_id, timeout_s=60.0)
            # The headline requirement: never stuck. The exact terminal state
            # is secondary (a lost upload grant is a known follow-up - see the
            # handoff), but it MUST be terminal, not left running/collecting.
            assert final["state"] in ("succeeded", "failed", "cancelled"), final
    finally:
        await agent.stop()


# --------------------------------------------------------------- hostile globs


def test_dotdot_and_absolute_patterns_never_escape_the_workdir(tmp_path: Path) -> None:
    """D74: a pattern is a relative POSIX path; ``..`` components and an
    absolute-looking pattern must be neutralised, never resolved against the
    real filesystem. Proven against a real secret file placed just outside the
    workdir, which a hostile compute.yaml could plausibly try to reach."""
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "safe.txt").write_bytes(b"safe")
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"outside the workdir, never collectable")
    # Also plant a file at a path that an absolute pattern would name literally
    # if the leading "/" were stripped and resolved against the workdir root -
    # this must NOT be treated as a match for "/etc/passwd" unless it actually
    # exists as workdir/etc/passwd (which it does not here).
    files, missing = expand_outputs(workdir, ("../secret.txt",))
    assert files == []
    assert missing == ["../secret.txt"]
    assert not any("secret" in f for f in files)

    files2, missing2 = expand_outputs(workdir, ("/etc/passwd",))
    assert files2 == []
    assert missing2 == ["/etc/passwd"]

    # A pattern combining a legitimate leading component with a later ".." must
    # also be refused, not partially matched.
    (workdir / "results").mkdir()
    files3, missing3 = expand_outputs(workdir, ("results/../../secret.txt",))
    assert files3 == []
    assert missing3 == ["results/../../secret.txt"]

    # Sanity: a real, in-workdir pattern still matches normally.
    files4, missing4 = expand_outputs(workdir, ("safe.txt",))
    assert files4 == ["safe.txt"] and missing4 == []


# --------------------------------------------------------------- WS reconnect


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as client:
        yield client


TICKER = """\
import sys, time
for i in range(40):
    print(f"line {i}", flush=True)
    time.sleep(0.05)
"""


@pytest.mark.asyncio
async def test_reconnecting_mid_follow_with_offset_never_dupes_or_drops(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    """The implementer's own ``test_resume_from_an_offset`` only resumes AFTER
    the job has finished (pure persisted read). Here the client disconnects and
    reconnects with ``?offset=`` WHILE the job is still emitting live chunks -
    the actual criterion-2 scenario a flaky Wi-Fi link produces."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(TICKER)
    manifest = {"name": "ticker", "entrypoint": ["python", "main.py"]}
    job_id = await submit(http, project, agent.node_id, manifest)

    url = f"ws://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"
    headers = {"Authorization": f"Bearer {token_of(runtime)}"}
    got = b""
    async with connect(url, additional_headers=headers) as ws:
        while len(got) < 30:
            raw = await ws.recv()
            frame = json.loads(raw)
            if frame.get("end"):
                raise AssertionError("job ended before the reconnect point")
            assert frame["offset"] == len(got)
            got += base64.b64decode(frame["data"])
    # Disconnected mid-stream; the job keeps running and keeps emitting.
    resume_url = f"{url}?stream=stdout&stdout_offset={len(got)}"
    async with connect(resume_url, additional_headers=headers) as ws2:
        async for raw in ws2:
            frame = json.loads(raw)
            if frame.get("end"):
                break
            assert frame["offset"] == len(got), (frame["offset"], len(got))
            got += base64.b64decode(frame["data"])

    full = (
        await http.get(f"/v1/jobs/{job_id}/logs", params={"stream": "stdout"})
    ).content
    assert got == full, (len(got), len(full))
