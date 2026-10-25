"""V0.4 G4 cluster scenarios: jobs through real controller + agent PROCESSES.

Agents run with ``[agent.execution] enabled = true, allow_unsandboxed = true`` (the
subprocess runtime - no Docker needed). Everything is observed through REST (and
criterion 6 through one SQL read of ``attempt_phase``). Timings are **loopback**
numbers: they bound the software on one machine, never a real network.

* criterion 3 - cancel <= 5 s, timeout within 2 s of the limit (measured);
* criterion 4 - one Idempotency-Key POSTed 5x (3 of them concurrently) -> one job,
  one execution (counted by a file the workload appends to);
* criterion 6 - every attempt of the run has all five phase rows;
* agent SIGKILLed mid-run -> the attempt is ``lost`` only after the node is
  OFFLINE, never before; the pinned job is retried on its node only (V0.6 G3)
  and fails ``node_lost/placed_node_offline`` once the node is gone (D65); a
  blackhole shorter than the lease leaves the job running and it succeeds;
* agent stopped gracefully mid-run -> ``node_lost/agent_shutdown`` then OFFLINE.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing

from meshforge.common.bundle import pack_to_file
from meshforge.common.states import AttemptPhase

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(240)]

JOB_TIMING = Timing(
    name="jobs",
    # SUSPECT at 0.7 s, OFFLINE at 2.0 s: room for a blackhole clearly shorter
    # than the lease on a loaded machine.
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
JOBS = {"dispatch_interval_s": 0.1}
TERMINAL = {"succeeded", "failed", "cancelled"}

WORKLOAD = """\
import sys, time
with open(sys.argv[1], "a") as fh:
    fh.write("run\\n")
print("started", flush=True)
time.sleep(float(sys.argv[2]))
"""


def _fabric(make_fabric: Callable[..., Fabric], agents: int = 1) -> Fabric:
    return make_fabric(
        agents=agents,
        timing=JOB_TIMING,
        agent_execution=EXEC,
        controller_jobs=JOBS,
        # Pin the subprocess runtime (the counter file lives on the host; the
        # Docker sandbox would - correctly - not see it). Same on every machine.
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )


def _submit(
    fabric: Fabric,
    tmp_path: Path,
    node_id: str,
    counter: Path,
    sleep_s: float,
    *,
    timeout: int = 3600,
    key: str | None = None,
    name: str = "proj",
    retry: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    project = tmp_path / name
    if not project.exists():
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
                "name": "cluster",
                "entrypoint": ["python", "main.py", str(counter), str(sleep_s)],
                "limits": {"timeout": timeout},
                **({"retry": retry} if retry else {}),
            },
            "bundle_digest": str(packed.digest),
            "placement": {"node_id": node_id},
        },
        headers={"idempotency-key": key} if key else {},
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
        time.sleep(0.05)
    fabric.fail(f"job {job_id} not in {sorted(states)} within {within} s: {job}")


def _explain(job: dict[str, Any]) -> str:
    return (
        f"{job['state']} {job.get('failure_type')} {job.get('failure_detail')!r} "
        f"attempts={[(a['state'], a.get('detail')) for a in job.get('attempts', [])]}"
    )


def _runs(counter: Path) -> int:
    return counter.read_text().count("run") if counter.exists() else 0


def test_criterion_4_one_key_five_posts_one_job_one_execution(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    node = fabric.nodes[0].node_id
    counter = tmp_path / "count.txt"
    key = "criterion-4-key"
    first = _submit(fabric, tmp_path, node, counter, 1.0, key=key)
    with concurrent.futures.ThreadPoolExecutor(3) as pool:
        racers = list(
            pool.map(
                lambda _i: _submit(fabric, tmp_path, node, counter, 1.0, key=key),
                range(3),
            )
        )
    last = _submit(fabric, tmp_path, node, counter, 1.0, key=key)
    answers = [first, *racers, last]
    job_ids = {body["job_id"] for _code, body in answers}
    assert len(job_ids) == 1, answers
    assert sorted(code for code, _ in answers) == [200, 200, 200, 200, 201]
    job = _wait(fabric, job_ids.pop(), TERMINAL, 30)
    assert job["state"] == "succeeded" and len(job["attempts"]) == 1
    time.sleep(0.5)  # a duplicate execution would have appended by now
    assert _runs(counter) == 1


def test_criterion_3_cancel_and_timeout_measured(
    make_fabric: Callable[..., Fabric], tmp_path: Path, record_timing: Any
) -> None:
    fabric = _fabric(make_fabric)
    node = fabric.nodes[0].node_id
    code, body = _submit(fabric, tmp_path, node, tmp_path / "c1.txt", 120, name="a")
    assert code == 201, body
    _wait(fabric, body["job_id"], {"running"}, 30)
    time.sleep(0.3)
    started = time.monotonic()
    assert fabric.call("POST", f"/v1/jobs/{body['job_id']}/cancel").status_code == 202
    job = _wait(fabric, body["job_id"], TERMINAL, 10)
    cancel_s = time.monotonic() - started
    assert job["state"] == "cancelled", job
    record_timing("jobs-cancel", cancel_s, 5.0, JOB_TIMING.name)
    assert cancel_s <= 5.0

    code, body = _submit(
        fabric, tmp_path, node, tmp_path / "c2.txt", 120, timeout=2, name="b"
    )
    assert code == 201, body
    _wait(fabric, body["job_id"], {"running"}, 30)
    started = time.monotonic()
    job = _wait(fabric, body["job_id"], TERMINAL, 15)
    overshoot = time.monotonic() - started - 2.0
    assert job["state"] == "failed" and job["failure_type"] == "deadline_exceeded", job
    record_timing("jobs-timeout-overshoot", overshoot, 2.0, JOB_TIMING.name)
    assert overshoot <= 2.0


def test_criterion_6_every_attempt_has_a_row_per_phase(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = _fabric(make_fabric)
    node = fabric.nodes[0].node_id
    ids = []
    for i, sleep_s in enumerate((0.0, 0.2, 60.0)):
        code, body = _submit(
            fabric, tmp_path, node, tmp_path / f"p{i}.txt", sleep_s, name=f"p{i}"
        )
        assert code == 201, body
        ids.append(body["job_id"])
        if sleep_s > 10:
            _wait(fabric, body["job_id"], {"running"}, 30)
            fabric.call("POST", f"/v1/jobs/{body['job_id']}/cancel")
        _wait(fabric, body["job_id"], TERMINAL, 30)
    rows = asyncio.run(_phase_counts(pg_settings))
    attempts = {a["attempt_id"] for j in ids for a in _job(fabric, j)["attempts"]}
    assert attempts and set(rows) == attempts
    # One row per AttemptPhase: the V0.4 five + V0.9 G1's ``compute`` (D213;
    # written skipped when the workload reports no kernel time).
    assert all(count == len(AttemptPhase) for count in rows.values()), rows


async def _phase_counts(pg_settings: Any) -> dict[str, int]:
    from sqlalchemy import text

    from meshforge.controller.store.engine import create_engine

    engine = create_engine(pg_settings)
    try:
        async with engine.connect() as conn:
            result = await conn.execute(
                text(
                    "SELECT a.id, count(p.phase) FROM attempt a "
                    "LEFT JOIN attempt_phase p ON p.attempt_id = a.id GROUP BY a.id"
                )
            )
            return {str(r[0]): int(r[1]) for r in result}
    finally:
        await engine.dispose()


def test_sigkill_mid_run_fails_node_lost_only_after_offline(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    victim = fabric.nodes[0]
    code, body = _submit(fabric, tmp_path, victim.node_id, tmp_path / "k.txt", 120)
    assert code == 201, body
    _wait(fabric, body["job_id"], {"running"}, 30)
    victim.kill()
    deadline = time.monotonic() + 20
    saw_suspect_while_running = False
    while time.monotonic() < deadline:
        node = fabric.node_view(victim)
        job = _job(fabric, body["job_id"])
        node_state = None if node is None else node["state"]
        if job["state"] != "running":
            # the job may only end once the node is OFFLINE (D66: never on SUSPECT)
            final = fabric.node_view(victim)
            assert final is not None and final["state"] == "offline", (job, final)
            break
        if node_state == "suspect":
            saw_suspect_while_running = True
        time.sleep(0.02)
    job = _job(fabric, body["job_id"])
    # V0.6 G3 (D110): the loss is retried - a pinned job on its own node only,
    # so it waits in the queue and D65 fails it once the node is gone for good
    assert job["state"] in ("queued", "failed"), job
    assert job["attempts"][0]["state"] == "lost"
    assert saw_suspect_while_running  # SUSPECT happened and did not kill the job
    job = _wait(fabric, body["job_id"], TERMINAL, 30)
    assert job["state"] == "failed" and job["failure_type"] == "node_lost", job
    assert job["failure_detail"] == "placed_node_offline", job
    assert [a["state"] for a in job["attempts"]] == ["lost"]


def test_blackhole_shorter_than_the_lease_leaves_the_job_running(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    node = fabric.nodes[0]
    counter = tmp_path / "b.txt"
    code, body = _submit(fabric, tmp_path, node.node_id, counter, 4.0)
    assert code == 201, body
    _wait(fabric, body["job_id"], {"running"}, 30)
    node.link.blackhole(True)
    time.sleep(JOB_TIMING.offline_after_s / 2)  # well inside the lease
    node.link.blackhole(False)
    job = _wait(fabric, body["job_id"], TERMINAL, 30)
    assert job["state"] == "succeeded", _explain(job)
    assert _runs(counter) == 1


def test_graceful_stop_mid_run_reports_agent_shutdown_then_offline(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    node = fabric.nodes[0]
    code, body = _submit(
        fabric,
        tmp_path,
        node.node_id,
        tmp_path / "s.txt",
        120,
        retry={"on_lost": False},  # V0.6 G3: fail on the loss, as in V0.4
    )
    assert code == 201, body
    _wait(fabric, body["job_id"], {"running"}, 30)
    node.stop_graceful()
    job = _wait(fabric, body["job_id"], TERMINAL, 10)
    assert job["state"] == "failed" and job["failure_type"] == "node_lost", job
    assert job["attempts"][0]["detail"] == "agent_shutdown"
    view = fabric.node_view(node)
    assert view is not None and view["state"] == "offline"
