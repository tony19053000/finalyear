"""V0.6 G3 (D118): the chaos suite's disk-full fault, checked on its own.

One agent process runs in its own user + mount namespace with its workdir on
a 4 MiB tmpfs (``mf_cluster`` ``agent_tmpfs_workdir``; no root). A job runs
fine; the tmpfs is then filled from outside (``/proc/<pid>/root``) and the
next job's staging hits a real ``ENOSPC``: ``FAILED(disk_full)`` - not
retryable by default, so **not** retried (one attempt). Freed again, a third
job succeeds on the same node.
"""

from __future__ import annotations

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


def test_a_full_workdir_fails_disk_full_without_retry_and_recovers(
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
    )
    (node,) = fabric.nodes
    ledger = make_ledger(tmp_path / "ledger")
    digest = upload_workload(fabric, tmp_path)
    manifest = ledger_manifest(ledger, sleep_s=0.1, retry={"backoff_s": 0})

    ok = _terminal(fabric, submit(fabric, digest, manifest))
    assert ok["state"] == "succeeded", ok

    assert node.fill_disk() > 0
    full = _terminal(fabric, submit(fabric, digest, manifest))
    assert (full["state"], full["failure_type"]) == ("failed", "disk_full"), full
    assert len(full["attempts"]) == 1  # disk_full is not retried by default

    node.free_disk()
    again = _terminal(fabric, submit(fabric, digest, manifest))
    assert again["state"] == "succeeded", again
