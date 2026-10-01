"""V0.4 G4 tester round 1: a REAL controller process crash/restart racing dispatch.

D65's promise is "commit-before-send means a crash after commit is repaired by
D66's resend". The implementer's own test for this
(``test_commit_without_send_is_resent_on_reconnect_and_runs_once``) is a
monkeypatch that drops one send in-process - it proves the *code path* exists
but never proves a real OS-level process death at that instant is survived
(a real crash can also lose whatever was buffered in the write queue, an
in-flight TCP segment, etc., none of which a monkeypatch can touch). This test
kills and restarts the **actual controller process** (``mf_cluster``'s
``restart_controller``, SIGKILL by default) right as each job's attempt is
being committed (``QUEUED -> PREPARING`` happens in the same transaction as the
attempt row and only then is ``StartAttempt`` sent, D65), so on some iterations
the kill lands before the send left the process and on others just after -
across several iterations at least one of each should occur. Either way the
brief's invariant must hold: exactly one execution, ever.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import START_TIMEOUT_S, Fabric, Timing

from meshforge.common.bundle import pack_to_file

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(180)]

TIMING = Timing(
    name="controller-restart-race",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.2,
    lease_multiplier=4,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=2.0,
    agent_hello_timeout_s=2.0,
)
EXEC = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
JOBS = {"dispatch_interval_s": 0.05}
TERMINAL = {"succeeded", "failed", "cancelled"}

WORKLOAD = """\
import sys, time
with open(sys.argv[1], "a") as fh:
    fh.write("run\\n")
time.sleep(float(sys.argv[2]))
"""


def _submit(
    fabric: Fabric, tmp_path: Path, node_id: str, counter: Path, name: str
) -> tuple[int, dict[str, Any]]:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(WORKLOAD)
    bundle = tmp_path / f"{name}.bundle"
    packed = pack_to_file(project, bundle)
    put = fabric.call(
        "PUT", f"/v1/blobs/{packed.digest.hex}", content=bundle.read_bytes()
    )
    assert put.status_code in (200, 201), put.text
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": "restart-race",
                "entrypoint": ["python", "main.py", str(counter), "0.2"],
                "limits": {"timeout": 60},
            },
            "bundle_digest": str(packed.digest),
            "placement": {"node_id": node_id},
        },
    )
    return resp.status_code, resp.json()


def _job(fabric: Fabric, job_id: str) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _wait(
    fabric: Fabric, job_id: str, states: set[str], within: float
) -> dict[str, Any]:
    deadline = time.monotonic() + within
    job: dict[str, Any] = {}
    while time.monotonic() < deadline:
        job = _job(fabric, job_id)
        if job["state"] in states:
            return job
        time.sleep(0.01)
    fabric.fail(f"job {job_id} not in {sorted(states)} within {within} s: {job}")


def _runs(counter: Path) -> int:
    return counter.read_text().count("run") if counter.exists() else 0


def test_real_controller_kill_and_restart_near_dispatch_commit_runs_exactly_once(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs=JOBS,
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    node = fabric.nodes[0].node_id

    for i in range(4):
        counter = tmp_path / f"count{i}.txt"
        code, body = _submit(fabric, tmp_path, node, counter, f"proj{i}")
        assert code == 201, body
        job_id = body["job_id"]
        # Race the kill against the commit: poll as fast as possible for the
        # QUEUED -> PREPARING transition (the attempt row's commit, D65) and
        # kill immediately after observing it - some iterations will still
        # catch the agent mid-StartAttempt-send, others just after.
        _wait(fabric, job_id, {"preparing", "running", "succeeded"}, 10)
        fabric.restart_controller(graceful=False)  # real SIGKILL, real restart
        fabric.wait_all_connected(within=START_TIMEOUT_S)

        job = _wait(fabric, job_id, TERMINAL, 30)
        assert job["state"] == "succeeded", (job["failure_type"], job["attempts"])
        assert len(job["attempts"]) == 1, job["attempts"]
        assert _runs(counter) == 1, f"job {i}: expected exactly one execution"
