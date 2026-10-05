"""V0.6 G3 tester round 1, real processes: retry edges the implementer's
cluster tests do not cover.

* ``not_before`` survives a controller SIGKILL (the retry waits its backoff);
* a cancel during the backoff wins for good (the workload never runs again);
* two lost attempts with ``max_attempts: 2`` -> FAILED(node_lost)
  retries_exhausted, nothing runs a third time;
* ``on_lost: false``: a SIGKILLed agent's job fails, never runs elsewhere;
* exclusion in a real fabric: the node that lost the attempt (back online in
  time) is skipped by the retry while another node is free.
"""

from __future__ import annotations

import datetime as dt
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import START_TIMEOUT_S, Fabric, Node, Timing
from mf_ledger import (
    Ledger,
    job_view,
    ledger_manifest,
    make_ledger,
    submit,
    upload_workload,
)

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(240)]

TIMING = Timing(
    name="g3-tester",
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
JOBS = {"dispatch_interval_s": 0.1, "reconnect_grace_s": 30.0}
ENV = {"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"}


def _until(fabric: Fabric, what: str, check: Callable[[], Any], within: float) -> Any:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        got = check()
        if got:
            return got
        time.sleep(0.05)
    fabric.fail(f"not reached within {within} s: {what}")


def _attempts(fabric: Fabric, job_id: str) -> list[dict[str, Any]]:
    view = job_view(fabric, job_id)
    return [] if view is None else list(view["attempts"])


def _first_running(fabric: Fabric, job_id: str) -> dict[str, Any]:
    return _until(
        fabric,
        "first attempt running",
        lambda: [a for a in _attempts(fabric, job_id) if a["state"] == "running"],
        30,
    )[0]


def _node(fabric: Fabric, node_id: str) -> Node:
    return next(n for n in fabric.nodes if n.node_id == node_id)


def _fabric(make_fabric: Callable[..., Fabric], agents: int = 2) -> Fabric:
    return make_fabric(
        agents=agents,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs=JOBS,
        agent_env=ENV,
    )


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_not_before_survives_a_controller_sigkill(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    ledger = Ledger(make_ledger(tmp_path / "ledger"))
    digest = upload_workload(fabric, tmp_path)
    job_id = submit(
        fabric,
        digest,
        ledger_manifest(
            ledger.root, sleep_s=0.1, wait_release=True, retry={"backoff_s": 8}
        ),
    )
    first = _first_running(fabric, job_id)
    _node(fabric, first["node_id"]).kill()
    _until(
        fabric,
        "attempt 1 lost",
        lambda: [a for a in _attempts(fabric, job_id) if a["state"] == "lost"],
        30,
    )
    fabric.kill_controller()  # inside the 8 s backoff
    time.sleep(1.0)
    fabric.restart_controller()
    ledger.release([job_id])
    second = _until(
        fabric,
        "attempt 2 started",
        lambda: [a for a in _attempts(fabric, job_id) if a["fencing_token"] == 2],
        60,
    )[0]
    lost = _attempts(fabric, job_id)[0]
    _until(
        fabric,
        "attempt 2 running",
        lambda: _attempts(fabric, job_id)[1]["started_at"],
        30,
    )
    second = _attempts(fabric, job_id)[1]
    gap = (_ts(second["started_at"]) - _ts(lost["ended_at"])).total_seconds()
    assert gap >= 7.0, f"retry ran {gap:.1f} s after the loss; backoff_s was 8"
    final = _until(
        fabric,
        "job succeeded",
        lambda: (v := job_view(fabric, job_id)) and v["state"] == "succeeded" and v,
        30,
    )
    assert [a["state"] for a in final["attempts"]] == ["lost", "done"]


def test_cancel_during_backoff_means_the_workload_never_runs_again(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    ledger = Ledger(make_ledger(tmp_path / "ledger"))
    digest = upload_workload(fabric, tmp_path)
    job_id = submit(
        fabric,
        digest,
        ledger_manifest(
            ledger.root, sleep_s=0.1, wait_release=True, retry={"backoff_s": 6}
        ),
    )
    first = _first_running(fabric, job_id)
    _node(fabric, first["node_id"]).kill()
    _until(
        fabric,
        "job queued again after the loss",
        lambda: (v := job_view(fabric, job_id)) and v["state"] == "queued" and v,
        30,
    )
    resp = fabric.call("POST", f"/v1/jobs/{job_id}/cancel")
    assert resp.status_code == 202, resp.text
    ledger.release([job_id])
    time.sleep(9.0)  # well past not_before
    view = job_view(fabric, job_id)
    assert view is not None and view["state"] == "cancelled", view
    assert [a["fencing_token"] for a in view["attempts"]] == [1]
    assert [ln.kind for ln in ledger.lines()] == ["start"]  # never ran again
    assert ledger.side_effects(job_id) == []


def test_two_losses_with_max_attempts_two_fail_retries_exhausted_and_stay_failed(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    ledger = Ledger(make_ledger(tmp_path / "ledger"))
    digest = upload_workload(fabric, tmp_path)
    job_id = submit(
        fabric,
        digest,
        ledger_manifest(
            ledger.root,
            sleep_s=0.1,
            wait_release=True,
            retry={"max_attempts": 2, "backoff_s": 0},
        ),
    )
    first = _first_running(fabric, job_id)
    x = _node(fabric, first["node_id"])
    (y,) = [n for n in fabric.nodes if n is not x]
    x.kill()
    second = _until(
        fabric,
        "retry running on Y",
        lambda: [
            a
            for a in _attempts(fabric, job_id)
            if a["fencing_token"] == 2 and a["state"] == "running"
        ],
        30,
    )[0]
    assert second["node_id"] == y.node_id
    y.kill()
    final = _until(
        fabric,
        "job failed",
        lambda: (v := job_view(fabric, job_id)) and v["state"] == "failed" and v,
        30,
    )
    assert final["failure_type"] == "node_lost", final
    assert "retries_exhausted after 2 attempts" in final["failure_detail"], final
    assert [a["state"] for a in final["attempts"]] == ["lost", "lost"]
    # both nodes come back: nothing resurrects the job, no third run
    x.restart(graceful=False)
    y.restart(graceful=False)
    fabric.wait_all_connected(within=START_TIMEOUT_S)
    ledger.release([job_id])
    time.sleep(3.0)
    again = job_view(fabric, job_id)
    assert again is not None and again["state"] == "failed"
    assert len(again["attempts"]) == 2
    assert [ln.kind for ln in ledger.lines()].count("start") == 2
    assert ledger.side_effects(job_id) == []


def test_on_lost_false_fails_the_job_and_never_runs_it_elsewhere(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    ledger = Ledger(make_ledger(tmp_path / "ledger"))
    digest = upload_workload(fabric, tmp_path)
    job_id = submit(
        fabric,
        digest,
        ledger_manifest(
            ledger.root,
            sleep_s=0.1,
            wait_release=True,
            retry={"on_lost": False, "backoff_s": 0},
        ),
    )
    first = _first_running(fabric, job_id)
    _node(fabric, first["node_id"]).kill()
    final = _until(
        fabric,
        "job failed",
        lambda: (v := job_view(fabric, job_id)) and v["state"] == "failed" and v,
        30,
    )
    assert final["failure_type"] == "node_lost", final
    ledger.release([job_id])
    time.sleep(3.0)
    again = job_view(fabric, job_id)
    assert again is not None and len(again["attempts"]) == 1
    assert [ln.kind for ln in ledger.lines()] == ["start"]


def test_the_retry_avoids_the_node_that_lost_the_attempt_even_when_it_is_back(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = _fabric(make_fabric)
    ledger = Ledger(make_ledger(tmp_path / "ledger"))
    digest = upload_workload(fabric, tmp_path)
    for round_no in range(3):
        _exclusion_round(fabric, ledger, digest, round_no)


def _exclusion_round(
    fabric: Fabric, ledger: Ledger, digest: str, round_no: int
) -> None:
    job_id = submit(
        fabric,
        digest,
        ledger_manifest(
            ledger.root,
            sleep_s=0.1,
            wait_release=True,
            name=f"excl-{round_no}",
            retry={"backoff_s": 0},
        ),
    )
    first = _first_running(fabric, job_id)
    x = _node(fabric, first["node_id"])
    (y,) = [n for n in fabric.nodes if n is not x]
    x.restart(graceful=False)  # killed and back inside the lease
    second = _until(
        fabric,
        "attempt 2 placed",
        lambda: [a for a in _attempts(fabric, job_id) if a["fencing_token"] == 2],
        60,
    )[0]
    assert second["node_id"] == y.node_id, (round_no, second, x.node_id)
    ledger.release([job_id])
    _until(
        fabric,
        "job succeeded",
        lambda: (v := job_view(fabric, job_id)) and v["state"] == "succeeded" and v,
        60,
    )
    fabric.wait_all_connected(within=START_TIMEOUT_S)
