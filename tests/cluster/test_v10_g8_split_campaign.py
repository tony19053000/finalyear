"""V0.10 G8 (0.10.9; D271, D272) on the cluster harness: a mini campaign of a
Mode 3 family over REAL controller + agent processes.

``split.csv_aggregate`` (lines + merge-csv, standard library only) on 3 agents,
strategies ``mode1`` (forced "don't split" on each other node) and ``mode3``
(forced onto all three), 3 repetitions - 9 runs, through the real campaign
runner, the bench submitter (items resolved by the D278 resolver), the
planner's forced set, real lanes (one process per chunk, each checking its own
chunk), the real merge-csv combine. Checked:

* every run ``done``; the mode3 jobs ran as lanes on all three nodes, the mode1
  jobs as one lane on their node;
* the combined output of every run is **identical** and passes the family's
  whole-output check (criterion 1's mechanism at harness scale);
* run records: one ``ok`` job row per run (``attempt_role`` ``combine``) with
  ``split_job_ms``, strategy ``mode1`` / ``mode3``, the family identity;
* labels compare mode1 with mode3 (``best`` or ``tie``, every configuration
  with 3 measured repetitions);
* both exports (``runs`` and ``chunks``) of the campaign are byte-identical
  twice; ``chunks.parquet`` holds the mode3 jobs' chunks.
"""

from __future__ import annotations

import hashlib
import io
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

from meshforge.bench.workloads import get_family
from meshforge.common.bundle import unpack

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(1500)]

TIMING = Timing(
    name="g8-campaign",
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
FAMILY = "split.csv_aggregate"
PARAMS = {"rows": 4000, "cols": 8, "rounds": 7000}
RUNS = 9


def _spec(origin: str) -> str:
    return (
        "name: g8-split\n"
        "seed: 81\n"
        "families:\n"
        f"  - family: {FAMILY}\n"
        "    params:\n"
        + "".join(f"      {k}: {v}\n" for k, v in PARAMS.items())
        + "strategies: [mode1, mode3]\n"
        f"origin: {origin}\n"
        "repetitions: 3\n"
        "budget: {max_runs: 20, max_hours: 2}\n"
    )


def _runs(fabric: Fabric, cid: str) -> list[dict[str, Any]]:
    resp = fabric.call("GET", f"/v1/bench/campaigns/{cid}/runs", params={"limit": 100})
    assert resp.status_code == 200, resp.text
    return list(resp.json()["runs"])


def _job(fabric: Fabric, job_id: str) -> dict[str, Any]:
    resp = fabric.call("GET", f"/v1/jobs/{job_id}")
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


def _output(fabric: Fabric, digest: str, dest: Path) -> dict[str, bytes]:
    data = fabric.call("GET", f"/v1/blobs/{digest.removeprefix('sha256:')}").content
    assert hashlib.sha256(data).hexdigest() == digest.removeprefix("sha256:")
    archive = dest.with_suffix(".bundle")
    dest.parent.mkdir(parents=True, exist_ok=True)
    archive.write_bytes(data)
    unpack(archive, dest)
    return {
        p.relative_to(dest).as_posix(): p.read_bytes()
        for p in sorted(dest.rglob("*"))
        if p.is_file()
    }


def test_a_split_family_campaign_labels_mode1_against_mode3(
    make_fabric: Callable[..., Fabric], tmp_path: Path
) -> None:
    fabric = make_fabric(
        agents=3,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra={
            "bench": {"tick_s": 0.5},
            "split": {"visibility_timeout_s": 60.0, "wait_retry_s": 0.3},
            "telemetry": {"record_interval_s": 0.5},
        },
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    nodes = sorted(n.node_id for n in fabric.nodes)
    origin = fabric.nodes[0].node_id
    created = fabric.call("POST", "/v1/bench/campaigns", json={"spec": _spec(origin)})
    assert created.status_code == 201, created.text
    cid = created.json()["campaign_id"]
    assert created.json()["run_count"] == RUNS
    started = fabric.call(
        "POST",
        f"/v1/bench/campaigns/{cid}/start",
        json={"max_concurrent_per_node": 1, "cooldown_s": 0.5},
    )
    assert started.status_code == 200, started.text

    deadline = time.monotonic() + 1200
    runs: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        runs = _runs(fabric, cid)
        if all(r["state"] in ("done", "failed", "skipped") for r in runs):
            break
        time.sleep(1.0)
    assert {r["state"] for r in runs} == {"done"}, [
        (r["strategy"], r["state"], r.get("detail")) for r in runs
    ]
    mode3 = [r for r in runs if r["strategy"] == "mode3"]
    mode1 = [r for r in runs if r["strategy"] == "mode1"]
    assert len(mode3) == 3 and len(mode1) == 6
    assert all(r["node_set"] == nodes for r in mode3)

    # lanes: all three nodes for mode3, one lane on its node for mode1;
    # every combined output identical and correct
    outputs: set[str] = set()
    for run in runs:
        job = _job(fabric, run["job_id"])
        assert job["state"] == "succeeded", job.get("failure_detail")
        lanes = {a["node_id"] for a in job["attempts"] if a.get("role") == "lane"}
        if run["strategy"] == "mode3":
            assert lanes == set(nodes), job["attempts"]
            placed = job["placement"]
            assert placed["mode"] == "auto" and placed["node_set"] == nodes
            assert placed.get("node_id") is None
        else:
            assert lanes == {run["node_id"]}
        outputs.add(job["output_digest"])
    assert len(outputs) == 1, outputs  # split == unsplit, byte for byte
    files = _output(fabric, outputs.pop(), tmp_path / "out")
    fam = get_family(FAMILY)
    verdict = fam.verify_output(files, PARAMS, mode3[0]["seed"])  # type: ignore[attr-defined]
    assert verdict.ok, verdict.problems

    # labels: one group, mode1 configs vs the mode3 config
    deadline = time.monotonic() + 120
    labels: dict[str, Any] = {}
    while time.monotonic() < deadline:
        resp = fabric.call("GET", f"/v1/bench/campaigns/{cid}/labels")
        assert resp.status_code == 200, resp.text
        labels = resp.json()
        if all(g["label"] != "pending" for g in labels["groups"]):
            break
        time.sleep(1.0)
    (group,) = labels["groups"]
    assert group["label"] in ("best", "tie"), group
    configs = group["configs"]
    assert {c["strategy"] for c in configs} == {"mode1", "mode3"}
    assert all(c["repetitions"] == 3 for c in configs), configs
    assert labels["rows"] == RUNS  # one job row per run, never a lane

    # the dataset: twice the same bytes, both tables
    runs_a = fabric.call("GET", "/v1/telemetry/export", params={"campaign": cid})
    runs_b = fabric.call("GET", "/v1/telemetry/export", params={"campaign": cid})
    assert runs_a.status_code == 200 and runs_a.content == runs_b.content
    table = pq.read_table(io.BytesIO(runs_a.content)).to_pylist()
    rows = [
        r
        for r in table
        if r["attempt_role"] == "combine" and r["failure_class"] == "ok"
    ]
    assert len(rows) == RUNS
    assert all(r["split_job_ms"] and r["workload_status"] == "ok" for r in rows)
    assert {r["strategy"] for r in rows} == {"mode1", "mode3"}
    assert all(r["workload_family"] == FAMILY for r in rows)
    chunks_a = fabric.call(
        "GET", "/v1/telemetry/export", params={"campaign": cid, "table": "chunks"}
    )
    chunks_b = fabric.call(
        "GET", "/v1/telemetry/export", params={"campaign": cid, "table": "chunks"}
    )
    assert chunks_a.status_code == 200 and chunks_a.content == chunks_b.content
    chunks = pq.read_table(io.BytesIO(chunks_a.content)).to_pylist()
    mode3_jobs = {r["job_id"] for r in mode3}
    committed = [c for c in chunks if c["job_id"] in mode3_jobs and c["won"]]
    assert {c["node_id"] for c in committed} >= {nodes[0]}
    assert all(c["run_ms"] is not None and c["bytes_out"] for c in committed)
    for job_id in mode3_jobs:
        mine = [c for c in committed if c["job_id"] == job_id]
        assert sum(c["items"] for c in mine) == PARAMS["rows"]
