"""V0.9 G2 exit criterion: a benchmark job runs through a real fabric.

A controller and one agent run as processes (subprocess runtime, pinned as in
the other cluster scenarios). The smallest ``cpu.matmul`` job folder from
:func:`build_job` is submitted with the real ``compute run --node ... -f`` and
its output fetched with ``compute get`` - the same path a campaign (G3) and a
user take. The agent installs the folder's pinned ``requirements.txt``
(``numpy==2.5.3``) through its environment cache, so the first run needs the
package index (or a configured wheelhouse); without network the test skips.

Checked: the job succeeds, ``results/workload.json`` comes back verified with a
positive monotonic ``compute_ns``, and the attempt recorded its phases.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import certifi
import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_cluster import Fabric, Timing

from meshforge.bench.workloads import WorkloadResult, build_job, get_family

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

TIMING = Timing(
    name="bench-job",
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


def _index_reachable() -> bool:
    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


def _compute(
    fabric: Fabric, local_data: Path, *args: str, cwd: Path
) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CA_FILE": str(fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": fabric.api_token(),
            "MESHFORGE_DATA_DIR": str(local_data),
            "XDG_CONFIG_HOME": str(cwd / "cli-config"),
        }
    )
    return subprocess.run(
        [sys.executable, "-m", "meshforge.cli", *args],
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=540,
        check=False,
    )


def test_smallest_matmul_benchmark_job_through_the_fabric(
    make_fabric: Callable[..., Fabric], tmp_path: Path, record_property: Any
) -> None:
    if not _index_reachable():
        pytest.skip("the job's pinned numpy must be installed from the package index")
    family = get_family("cpu.matmul")
    built = build_job(family, family.smallest_params(), 7, tmp_path / "bench-job")
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={
            "DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock",
            # The harness points SSL_CERT_FILE at the test fabric CA (for plain
            # clients of the controller); the agent's env builder (uv) needs the
            # public roots to reach the package index, as on a real laptop. The
            # agent trusts the controller through its enrolled ca.crt, not this.
            "SSL_CERT_FILE": certifi.where(),
        },
    )
    node = fabric.nodes[0]
    work = tmp_path / "cli"
    work.mkdir()
    done = _compute(
        fabric,
        node.data_dir,
        "run",
        str(built.path),
        "--node",
        node.node_id,
        "-f",
        cwd=work,
    )
    assert done.returncode == 0, (done.stdout[-3000:], done.stderr[-3000:])
    job_id = done.stdout.splitlines()[0].strip()

    job = fabric.call("GET", f"/v1/jobs/{job_id}").json()
    assert job["state"] == "succeeded", job
    assert job["name"] == built.manifest.name
    (attempt,) = job["attempts"]
    assert attempt["output_digest"], attempt

    dest = tmp_path / "out"
    got = _compute(fabric, node.data_dir, "get", job_id, "--dest", str(dest), cwd=work)
    assert got.returncode == 0, got.stderr
    document = json.loads((dest / "results" / "workload.json").read_text())
    result = WorkloadResult.model_validate(document)
    verdict = family.verify(result)
    assert verdict.ok, verdict.problems
    assert result.params == built.params and result.seed == 7
    assert result.libraries["numpy"] == "2.5.3"
    assert result.compute_ns is not None and result.compute_ns > 0
    record_property("bench_job_compute_s", round(result.compute_ns / 1e9, 3))

    # The fabric's own phase timings for the attempt (what G1's run record joins).
    decisions = fabric.call("GET", f"/v1/jobs/{job_id}/decisions").json()["decisions"]
    actual = decisions[-1]["actual"]
    assert actual["attempt_id"] == attempt["attempt_id"], (actual, attempt)
    phases = actual["phases_ms"]
    # V0.9 G1: six phases - the sixth, compute, is the workload's own time.
    assert len(phases) == 6 and phases["compute"] is not None, phases
    run_ms = max(v for v in phases.values() if v is not None)
    # The workload's own compute time fits inside the attempt's longest phase.
    assert result.compute_ms is not None and result.compute_ms <= run_ms, phases
    print(  # noqa: T201
        f"bench job {job_id}: compute {result.compute_ms:.0f} ms, "
        f"{result.check_detail}, phases_ms {phases}"
    )
