"""V0.9 G1 exit criterion: real jobs -> run records -> the Parquet dataset, twice.

A controller and one agent run as processes (subprocess runtime). Three real
jobs: one that succeeds, one that fails (exit 1), and the smallest
``cpu.matmul`` benchmark job from :func:`build_job`, run with the real
``compute run --node ... -f`` (its pinned numpy comes from the package index:
without network the test skips, like G2's). Then the real
``compute telemetry export -o`` runs twice.

Checked: one record per attempt with its failure class; the benchmark's record
carries its family, parameters, seed and the workload's own ``compute_ns``
(inside ``run_ms``); the clock offset of a one-host fabric is ~0 within its
bound; the two exports are byte-identical, read back with pyarrow, and equal
``GET /v1/telemetry/export``.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import certifi
import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from mf_cluster import Fabric, Timing

from meshforge.bench.workloads import build_job, get_family
from meshforge.common.bundle import pack_to_file

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(600)]

TIMING = Timing(
    name="telemetry",
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


def _plain_job(
    fabric: Fabric, tmp_path: Path, node_id: str, name: str, code: int
) -> str:
    project = tmp_path / name
    project.mkdir()
    (project / "main.py").write_text(f"print('{name}')\nraise SystemExit({code})\n")
    packed = pack_to_file(project, tmp_path / f"{name}.bundle")
    put = fabric.call(
        "PUT",
        f"/v1/blobs/{packed.digest.hex}",
        content=(tmp_path / f"{name}.bundle").read_bytes(),
    )
    assert put.status_code in (200, 201), put.text
    resp = fabric.call(
        "POST",
        "/v1/jobs",
        json={
            "manifest": {
                "name": name,
                "entrypoint": ["python", "main.py"],
                "retry": {"max_attempts": 1},
            },
            "bundle_digest": str(packed.digest),
            "placement": {"node_id": node_id},
        },
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["job_id"])


def _wait(fabric: Fabric, job_id: str, within: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        job = dict(fabric.call("GET", f"/v1/jobs/{job_id}").json())
        if job["state"] in {"succeeded", "failed", "cancelled"}:
            return job
        time.sleep(0.1)
    fabric.fail(f"job {job_id} did not end within {within} s")


def test_three_real_jobs_export_twice_to_identical_parquet(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    if not _index_reachable():
        pytest.skip("the benchmark job's pinned numpy comes from the package index")
    family = get_family("cpu.matmul")
    built = build_job(family, family.smallest_params(), 7, tmp_path / "bench-job")
    fabric = make_fabric(
        agents=1,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={
            "DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock",
            "SSL_CERT_FILE": certifi.where(),
        },
    )
    node = fabric.nodes[0]
    work = tmp_path / "cli"
    work.mkdir()
    ok_id = _plain_job(fabric, tmp_path, node.node_id, "plain-ok", 0)
    fail_id = _plain_job(fabric, tmp_path, node.node_id, "plain-fail", 1)
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
    bench_id = done.stdout.splitlines()[0].strip()
    assert _wait(fabric, ok_id)["state"] == "succeeded"
    assert _wait(fabric, fail_id)["state"] == "failed"
    assert _wait(fabric, bench_id)["state"] == "succeeded"

    # The sweep runs every 2 s: wait until all three records are exported.
    deadline = time.monotonic() + 30
    while True:
        answer = fabric.call("GET", "/v1/telemetry/export")
        assert answer.status_code == 200, answer.text
        if answer.headers["x-meshforge-telemetry-rows"] == "3":
            break
        assert time.monotonic() < deadline, answer.headers
        time.sleep(0.5)

    files = [work / "a.parquet", work / "b.parquet"]
    for target in files:
        out = _compute(
            fabric,
            node.data_dir,
            "telemetry",
            "export",
            "-o",
            str(target),
            "--json",
            cwd=work,
        )
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["rows"] == 3
    first, second = (f.read_bytes() for f in files)
    assert first == second == fabric.call("GET", "/v1/telemetry/export").content

    rows = {r["job_id"]: r for r in pq.read_table(io.BytesIO(first)).to_pylist()}
    assert set(rows) == {ok_id, fail_id, bench_id}
    assert rows[ok_id]["failure_class"] == "ok"
    assert rows[fail_id]["failure_class"] == "workload"
    bench = rows[bench_id]
    assert bench["failure_class"] == "ok"
    assert bench["workload_family"] == "cpu.matmul" and bench["workload_seed"] == 7
    assert json.loads(bench["workload_params"]) == built.params
    assert bench["p_n"] == built.params.get("n")
    assert bench["compute_ns"] is not None and bench["compute_ns"] > 0
    assert bench["compute_ns"] <= bench["run_ms"] * 1_000_000
    for record in rows.values():
        assert record["schema_version"] == 1 and record["node_id"] == node.node_id
        assert record["clock_offset_ms"] is not None, record
        # One host, one clock: ~0 within the bound (+1 ms for rounding).
        assert abs(record["clock_offset_ms"]) <= record["clock_offset_bound_ms"] + 1.0
    print(  # noqa: T201
        f"telemetry export: {len(first)} bytes, bench compute "
        f"{bench['compute_ns'] / 1e6:.1f} ms of run {bench['run_ms']} ms, clock "
        f"{bench['clock_offset_ms']} +- {bench['clock_offset_bound_ms']} ms"
    )
