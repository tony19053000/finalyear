"""V0.6 G3 (D118, D126): the chaos suite's disk-full fault, checked on its own.

One agent process runs in its own user + mount namespace with its workdir on
a 4 MiB tmpfs (``mf_cluster`` ``agent_tmpfs_workdir``; no root). A job runs
fine; the tmpfs is then filled from outside (``/proc/<pid>/root``) and the
next job's staging hits a real ``ENOSPC``:

* **one node** - a job pinned to it fails ``disk_full`` after one attempt
  (D126: its node would fail it again); an auto-placed job is retried (D126),
  and although no other node could ever run it the ``disk_full`` exclusion is
  **not** lifted early (D129): the retry waits out ``retry_exclude_s`` before
  its second attempt, then fails ``disk_full`` with ``retries_exhausted``.
  Freed again, a job succeeds;
* **two nodes** (fix round 1) - with the first node full, auto jobs all
  succeed: an attempt that hit the full disk is retried on the other node.
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

from mf_cluster import Fabric, Timing, tmpfs_unsupported_reason
from mf_ledger import job_view, ledger_manifest, make_ledger, submit, upload_workload

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(120)]

TIMING = Timing(
    name="disk-full",
    heartbeat_interval_s=0.5,
    miss_allowance_s=0.5,
    lease_multiplier=6,
    controller_hello_timeout_s=3.0,
    shutdown_drain_s=2.0,
    agent_reconnect_base_s=0.1,
    agent_reconnect_cap_s=1.0,
    agent_hello_timeout_s=3.0,
)
EXCLUDE_S = 4.0
EXEC = {"enabled": True, "allow_unsandboxed": True, "grace_s": 1.0}
TERMINAL = {"succeeded", "failed", "cancelled"}


def _terminal(fabric: Fabric, job_id: str, within: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + within
    view: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        view = job_view(fabric, job_id)
        if view is not None and view["state"] in TERMINAL:
            return view
        time.sleep(0.05)
    fabric.fail(f"job {job_id} not terminal: {view}")


def test_a_full_workdir_fails_disk_full_and_recovers(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    reason = tmpfs_unsupported_reason()
    if reason is not None:
        pytest.skip(reason)
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_tmpfs_workdir={0: 4 * 1024 * 1024},
        controller_extra={"planner": {"retry_exclude_s": EXCLUDE_S}},
    )
    (node,) = fabric.nodes
    ledger = make_ledger(tmp_path / "ledger")
    digest = upload_workload(fabric, tmp_path)
    manifest = ledger_manifest(
        ledger, sleep_s=0.1, retry={"backoff_s": 0, "max_attempts": 2}
    )

    ok = _terminal(fabric, submit(fabric, digest, manifest))
    assert ok["state"] == "succeeded", ok

    assert node.fill_disk() > 0
    pinned = _terminal(
        fabric, submit(fabric, digest, manifest, {"node_id": node.node_id})
    )
    assert (pinned["state"], pinned["failure_type"]) == ("failed", "disk_full")
    assert len(pinned["attempts"]) == 1, pinned  # D126: pinned is not retried

    lone = _terminal(fabric, submit(fabric, digest, manifest), within=30 + EXCLUDE_S)
    assert (lone["state"], lone["failure_type"]) == ("failed", "disk_full"), lone
    first, second = lone["attempts"]
    assert [first["failure_type"], second["failure_type"]] == ["disk_full"] * 2
    assert "retries_exhausted" in (lone["failure_detail"] or ""), lone
    # D129: the full node was not lifted onto before its window ended
    dispatched = [
        dt.datetime.fromisoformat(t["at"])
        for t in lone["transitions"]
        if (t["from_state"], t["to_state"]) == ("queued", "preparing")
    ]
    assert len(dispatched) == 2, lone
    gap = dispatched[1] - dt.datetime.fromisoformat(first["ended_at"])
    assert gap.total_seconds() >= EXCLUDE_S - 0.5, lone

    node.free_disk()
    again = _terminal(fabric, submit(fabric, digest, manifest))
    assert again["state"] == "succeeded", again


def test_auto_jobs_hitting_a_full_node_are_retried_on_the_other(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    reason = tmpfs_unsupported_reason()
    if reason is not None:
        pytest.skip(reason)
    fabric = make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_tmpfs_workdir={0: 4 * 1024 * 1024},
    )
    full, healthy = fabric.nodes
    ledger = make_ledger(tmp_path / "ledger")
    digest = upload_workload(fabric, tmp_path)
    manifest = ledger_manifest(ledger, sleep_s=0.1, retry={"backoff_s": 0})
    assert full.fill_disk() > 0

    jobs = [submit(fabric, digest, manifest) for _ in range(8)]
    hit = 0
    for job_id in jobs:
        view = _terminal(fabric, job_id)
        assert view["state"] == "succeeded", view
        *failed, last = view["attempts"]
        assert last["node_id"] == healthy.node_id, view
        for attempt in failed:
            assert (attempt["node_id"], attempt["failure_type"]) == (
                full.node_id,
                "disk_full",
            ), view
        assert len(failed) <= 1, view  # the full node is excluded after one
        hit += len(failed)
    # one CPU slot per agent: the first tick puts a job on each node
    assert hit >= 1, "no job was ever placed on the full node"
