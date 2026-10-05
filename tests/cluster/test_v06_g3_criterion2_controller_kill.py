"""V0.6 criterion 2 / D115: a real controller ``SIGKILL`` with 20 running jobs.

A controller process and four agent processes (subprocess runtime, five CPU
slots each); 20 automatically placed ledger jobs (``mf_ledger``) are all
``RUNNING`` and held at the release gate. Then the controller is killed with
``SIGKILL``; while it is down half of the jobs are released (they finish, and
their final reports wait in the agents' buffers); the controller is started
again (new process, same database, new ports); the other half is released.

Every job must reach the right terminal state (two of them exit 3 on purpose ->
``FAILED(user_error)``, never retried; the rest ``SUCCEEDED``) with **one**
attempt, and each workload's side-effect file must hold exactly one line - the
attempt the controller committed. The ledger must show no overlapping run and
one ``start``/``end`` per job. Timings are loopback numbers.
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
from mf_ledger import (
    Ledger,
    job_view,
    ledger_manifest,
    make_ledger,
    submit,
    upload_workload,
)

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(300)]

TIMING = Timing(
    name="criterion2",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.5,
    lease_multiplier=6,
    controller_hello_timeout_s=3.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=1.0,
    agent_hello_timeout_s=3.0,
)
EXEC = {
    "enabled": True,
    "allow_unsandboxed": True,
    "grace_s": 1.0,
    "max_cpu_jobs": 5,
    "max_concurrent_attempts": 5,
}
JOBS = {"dispatch_interval_s": 0.1, "reconnect_grace_s": 30.0}
TERMINAL = {"succeeded", "failed", "cancelled"}
JOB_COUNT = 20
FAILING = {3, 14}
"""Indices whose workload exits 3: one finishes during the outage, one after."""


def _wait_all(
    fabric: Fabric, job_ids: list[str], states: set[str], within: float
) -> dict[str, dict[str, Any]]:
    deadline = time.monotonic() + within
    seen: dict[str, dict[str, Any]] = {}
    while time.monotonic() < deadline:
        for job_id in job_ids:
            if seen.get(job_id, {}).get("state") in states:
                continue
            view = job_view(fabric, job_id)
            if view is not None:
                seen[job_id] = view
        if all(seen.get(j, {}).get("state") in states for j in job_ids):
            return seen
        time.sleep(0.1)
    stuck = {j: seen.get(j, {}).get("state") for j in job_ids}
    fabric.fail(f"jobs not all in {sorted(states)} within {within} s: {stuck}")


def test_controller_sigkill_with_20_running_jobs_every_side_effect_once(
    make_fabric: Callable[..., Fabric], tmp_path: Path, record_timing: Any
) -> None:
    fabric = make_fabric(
        agents=4,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs=JOBS,
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    ledger = Ledger(make_ledger(tmp_path / "ledger"))
    digest = upload_workload(fabric, tmp_path)
    jobs = [
        submit(
            fabric,
            digest,
            ledger_manifest(
                ledger.root,
                sleep_s=0.2,
                exit_code=3 if i in FAILING else 0,
                wait_release=True,
                name=f"c2-{i}",
            ),
        )
        for i in range(JOB_COUNT)
    ]
    _wait_all(fabric, jobs, {"running"}, 90)
    # every workload has started (ledger) - not only reported RUNNING
    deadline = time.monotonic() + 30
    while len({ln.job_id for ln in ledger.lines()}) < JOB_COUNT:
        if time.monotonic() > deadline:
            fabric.fail("not every workload started")
        time.sleep(0.05)

    killed_at = fabric.kill_controller()
    early, late = jobs[: JOB_COUNT // 2], jobs[JOB_COUNT // 2 :]
    ledger.release(early)  # these finish while no controller is running
    time.sleep(2.0)
    fabric.restart_controller()
    restarted_at = time.monotonic()
    fabric.wait_all_connected(within=START_TIMEOUT_S)
    ledger.release(late)
    final = _wait_all(fabric, jobs, TERMINAL, 120)
    record_timing(
        "criterion2-restart-to-all-terminal",
        time.monotonic() - restarted_at,
        None,
        TIMING.name,
    )
    record_timing("criterion2-outage", restarted_at - killed_at, None, TIMING.name)

    by_job = ledger.by_job()
    for i, job_id in enumerate(jobs):
        job = final[job_id]
        expected = ("failed", "user_error") if i in FAILING else ("succeeded", None)
        assert (job["state"], job["failure_type"]) == expected, (i, job)
        (attempt,) = job["attempts"]  # one attempt: nothing was lost or retried
        assert attempt["state"] == ("failed" if i in FAILING else "done"), attempt
        assert ledger.side_effects(job_id) == [attempt["attempt_id"]], (i, job_id)
        kinds = sorted(line.kind for line in by_job[job_id])
        assert kinds == ["end", "start"], (i, kinds)
        assert {line.attempt_id for line in by_job[job_id]} == {attempt["attempt_id"]}
    assert not [ln for ln in ledger.lines() if ln.kind == "overlap"]
