"""V0.6 G3 deliverable 4 (D117, ADR-0015): a partition is at-least-once
execution, **exactly-once commit** - with the overlap counted, not hidden.

Two agent processes. A ledger job (``mf_ledger``) runs on agent X and waits
at its release gate. X's link to the controller is blackholed (the laptop's
Wi-Fi drops): X keeps running the workload, its lease runs out, the controller
marks the attempt ``LOST`` and retries the job on Y - in the same transaction.
Y's workload finds X's lock held: the ledger records the **overlap**. The job
is released, Y's run commits (``SUCCEEDED``), X's run also ends while still
cut off. Then X's link heals: X's Reconcile still holds its attempt (its final
report never got through), the controller counts the overlap once
(``controller.jobs.execution_overlap``), answers ``CancelAttempt{release}`` and
nothing X reports is committed: the job stays ``SUCCEEDED`` with Y's attempt.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing
from mf_ledger import (
    Ledger,
    job_view,
    ledger_manifest,
    make_ledger,
    submit,
    upload_workload,
)

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(180)]

TIMING = Timing(
    name="partition",
    # SUSPECT at 0.7 s, OFFLINE at 2.0 s after the last heartbeat
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.2,
    lease_multiplier=4,
    controller_hello_timeout_s=2.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=1.0,
    agent_hello_timeout_s=2.0,
)
EXEC = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
JOBS = {"dispatch_interval_s": 0.1}


def _until(fabric: Fabric, what: str, check: Callable[[], Any], within: float) -> Any:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        got = check()
        if got:
            return got
        time.sleep(0.05)
    fabric.fail(f"not reached within {within} s: {what}")


def test_partitioned_agent_keeps_running_retry_commits_overlap_counted_and_released(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs=JOBS,
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    ledger = Ledger(make_ledger(tmp_path / "ledger"))
    digest = upload_workload(fabric, tmp_path)
    job_id = submit(
        fabric,
        digest,
        ledger_manifest(
            ledger.root, sleep_s=0.2, wait_release=True, retry={"backoff_s": 0}
        ),
    )

    def attempts() -> list[dict[str, Any]]:
        view = job_view(fabric, job_id)
        return [] if view is None else list(view["attempts"])

    first = _until(
        fabric,
        "first attempt running",
        lambda: [a for a in attempts() if a["state"] == "running"],
        30,
    )[0]
    by_id = {n.node_id: n for n in fabric.nodes}
    x = by_id[first["node_id"]]
    (y,) = [n for n in fabric.nodes if n is not x]
    _until(
        fabric,
        "X's workload started",
        lambda: [ln for ln in ledger.lines() if ln.attempt_id == first["attempt_id"]],
        10,
    )

    x.link.blackhole(True)  # X's Wi-Fi drops; X keeps running
    second = _until(
        fabric,
        "retry running on Y",
        lambda: [
            a for a in attempts() if a["fencing_token"] == 2 and a["state"] == "running"
        ],
        30,
    )[0]
    assert second["node_id"] == y.node_id
    lost = attempts()[0]
    assert (lost["state"], lost["failure_type"]) == ("lost", "node_lost")
    overlap = _until(
        fabric,
        "Y's run recorded the overlap",
        lambda: [ln for ln in ledger.lines() if ln.kind == "overlap"],
        10,
    )
    assert [ln.attempt_id for ln in overlap] == [second["attempt_id"]]

    ledger.release([job_id])
    _until(
        fabric,
        "both runs ended",
        lambda: len([ln for ln in ledger.lines() if ln.kind == "end"]) == 2,
        20,
    )
    view = _until(
        fabric,
        "job succeeded",
        lambda: (v := job_view(fabric, job_id)) and v["state"] == "succeeded" and v,
        20,
    )
    assert [a["state"] for a in view["attempts"]] == ["lost", "done"]

    x.link.blackhole(False)  # the partition heals
    event = fabric.wait_log(
        fabric.controller,  # type: ignore[arg-type]
        "controller.jobs.execution_overlap",
        within=30,
        attempt_id=first["attempt_id"],
    )
    assert event["node_id"] == x.node_id
    assert event["current_attempt"] == 2 and event["job_id"] == job_id
    fabric.wait_log(
        x.process,
        "agent.execution.attempt_released",
        within=30,
        attempt_id=first["attempt_id"],
    )
    time.sleep(1.0)  # any late report of X's has been replayed by now

    final = job_view(fabric, job_id)
    assert final is not None and final["state"] == "succeeded"
    assert [(a["attempt_id"], a["state"]) for a in final["attempts"]] == [
        (first["attempt_id"], "lost"),
        (second["attempt_id"], "done"),
    ]
    overlaps = [
        r
        for r in fabric.controller_records()
        if r.get("event") == "controller.jobs.execution_overlap"
    ]
    assert len(overlaps) == 1, overlaps  # counted once per attempt
    # the duplicate execution is real and reported: both runs left their side
    # effect, exactly one of them is the committed attempt
    assert sorted(ledger.side_effects(job_id)) == sorted(
        [first["attempt_id"], second["attempt_id"]]
    )
