"""V0.9 G3 exit criterion (brief §5d): a real campaign on a 2-node harness.

A controller and two agents run as processes (subprocess runtime). With the
real CLI: ``compute bench plan`` (local expansion against the live nodes),
``compute bench create`` a 2-family x 2-strategy x 3-repetition campaign
(origin = node A: ``local`` on A, ``mode1`` on B), ``compute bench submit -n 12
--wait``, then ``compute telemetry export --campaign`` twice.

Checked: the CLI's plan equals the stored runs (same config ids, order);
every run ended ``done``; one record per run, every record carrying
``campaign_id`` / ``group_id`` / ``config_id`` / ``repetition`` (checklist
29), the right ``strategy`` for its node, ``placement_mode`` pinned and a
``planner_node_id`` (the shadow plan, D215); both exports byte-identical
(checklist 31). The two families need no package download (stdlib kernels).
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from mf_cluster import Fabric, Timing

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(900)]

TIMING = Timing(
    name="campaign",
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
        timeout=840,
        check=False,
    )


def _spec(origin: str) -> str:
    return (
        "name: g3-cluster\n"
        "seed: 31\n"
        "families:\n"
        "  - family: cpu.primes\n"
        "  - family: cpu.zlib\n"
        "strategies: [local, mode1]\n"
        f"origin: {origin}\n"
        "repetitions: 3\n"
        "budget: {max_runs: 12, max_hours: 1}\n"
    )


def _rows(fabric: Fabric, campaign_id: str) -> list[dict[str, Any]]:
    resp = fabric.call(
        "GET", f"/v1/bench/campaigns/{campaign_id}/runs", params={"limit": 100}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json()["runs"])


def test_two_node_campaign_end_to_end_with_export(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    a, b = fabric.nodes[0], fabric.nodes[1]
    work = tmp_path / "cli"
    work.mkdir()
    spec = work / "campaign.yaml"
    spec.write_text(_spec(a.node_id))

    planned = _compute(
        fabric,
        a.data_dir,
        "bench",
        "plan",
        str(spec),
        "--json",
        "--show",
        "12",
        cwd=work,
    )
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    assert plan["runs"] == 12 and plan["configs"] == 4 and plan["groups"] == 2

    created = _compute(
        fabric, a.data_dir, "bench", "create", str(spec), "--json", cwd=work
    )
    assert created.returncode == 0, created.stderr
    campaign = json.loads(created.stdout)
    campaign_id = campaign["campaign_id"]
    assert campaign["run_count"] == 12

    stored = _rows(fabric, campaign_id)
    assert [(r["seq"], r["config_id"], r["repetition"]) for r in stored] == [
        (r["seq"], r["config_id"], r["repetition"]) for r in plan["first"]
    ], "the CLI's local plan and the controller's expansion differ"

    started = time.monotonic()
    submitted = _compute(
        fabric,
        a.data_dir,
        "bench",
        "submit",
        campaign_id,
        "-n",
        "12",
        "--wait",
        "--timeout",
        "720",
        "--json",
        cwd=work,
    )
    assert submitted.returncode == 0, (
        submitted.stdout[-3000:],
        submitted.stderr[-3000:],
    )
    result = json.loads(submitted.stdout)
    assert result["submitted"] == 12
    assert set(result["ended"].values()) == {"done"}, result["ended"]
    took = time.monotonic() - started

    runs = _rows(fabric, campaign_id)
    assert {r["state"] for r in runs} == {"done"}
    detail = fabric.call("GET", f"/v1/bench/campaigns/{campaign_id}").json()
    assert detail["state"] == "done"

    # every record written (the sweep runs every 2 s)
    deadline = time.monotonic() + 30
    while True:
        answer = fabric.call(
            "GET", "/v1/telemetry/export", params={"campaign": campaign_id}
        )
        assert answer.status_code == 200, answer.text
        if answer.headers["x-meshforge-telemetry-rows"] == "12":
            break
        assert time.monotonic() < deadline, answer.headers
        time.sleep(0.5)

    files = [work / "a.parquet", work / "b.parquet"]
    for target in files:
        out = _compute(
            fabric,
            a.data_dir,
            "telemetry",
            "export",
            "--campaign",
            campaign_id,
            "-o",
            str(target),
            "--json",
            cwd=work,
        )
        assert out.returncode == 0, out.stderr
        assert json.loads(out.stdout)["rows"] == 12
    first, second = (f.read_bytes() for f in files)
    assert first == second  # checklist 31

    records = pq.read_table(io.BytesIO(first)).to_pylist()
    by_job = {r["job_id"]: r for r in runs}
    assert {r["job_id"] for r in records} == set(by_job)
    for record in records:
        run = by_job[record["job_id"]]
        assert record["campaign_id"] == campaign_id  # checklist 29
        assert record["group_id"] == run["group_id"]
        assert record["config_id"] == run["config_id"]
        assert record["repetition"] == run["repetition"]
        assert record["strategy"] == run["strategy"]
        assert record["node_id"] == run["node_id"]
        assert record["node_id"] == (
            a.node_id if run["strategy"] == "local" else b.node_id
        )
        assert record["submitter_node_id"] == a.node_id
        assert record["placement_mode"] == "pinned"
        assert record["planner_node_id"] in {a.node_id, b.node_id}, record
        assert record["failure_class"] == "ok"
        assert record["workload_family"] in {"cpu.primes", "cpu.zlib"}
        assert record["workload_seed"] == run["seed"]
        assert record["compute_ns"] is not None and record["compute_ns"] > 0
    print(  # noqa: T201
        f"campaign {campaign_id}: 12 runs in {took:.1f} s on 2 nodes; "
        f"export {len(first)} bytes; planner would pick "
        f"{sum(1 for r in records if r['planner_node_id'] == r['node_id'])}/12 "
        "of the forced nodes"
    )
