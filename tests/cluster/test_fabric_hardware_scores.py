"""Cluster scenario s: hardware scores end to end on real processes (0.3.4, G7-B).

One real agent process with background scoring **on** (``start_delay_s = 0``), a
real controller process, the real benchmark child. The scorer is made fast and
deterministic with the child's test-only environment hook
``MESHFORGE_BENCH_DISABLE=torch,numpy`` (passed to the agent process, inherited by
its child - never set by the agent itself): with no BLAS backend every host metric
is truthfully ``unavailable: no_blas_backend`` and every GPU metric ``unavailable``,
in about two seconds on any machine, GPU or not. The run is still a complete
``ScoreSet``, so the whole path is exercised: child -> runner -> ``scores.json`` ->
``send_report("hardware_scores")`` -> controller validation -> ``node.hardware_scores``
-> REST -> ``compute nodes --json`` (a real CLI subprocess).

Then the agent is restarted on the same data dir: the valid cache must ride in the
new ``Hello`` (``agent.scores.hello_cache_valid``) and no second run may start.

Fast profile (D25). Nothing asserts this machine's hardware: only statuses the
hook makes deterministic, and shapes.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")

from mf_cluster import FAST, START_TIMEOUT_S, Fabric, MakeFabric

pytestmark = [pytest.mark.docker]

RecordTiming = Callable[[str, float, "float | None", str], None]

SCORED_WITHIN_S = 30.0
"""Generous: the hooked run takes ~2 s (a 2 s load sample plus one child probe)."""

HOST_METRICS = {
    "cpu_fp32_gflops_1t",
    "cpu_fp32_gflops_all",
    "cpu_fp64_gflops_all",
    "mem_triad_gb_s",
}


def _compute(fabric: Fabric, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["MESHFORGE_CLI__CONTROLLER_URL"] = fabric.http_base
    env["PYTHONUTF8"] = "1"
    env["COLUMNS"] = "240"  # a pipe is 80 wide: folded cells would split words
    return subprocess.run(
        [sys.executable, "-m", "meshforge.cli", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_s_a_scored_agent_shows_measured_scores_in_compute_nodes(
    make_fabric: MakeFabric, record_timing: RecordTiming
) -> None:
    fabric: Fabric = make_fabric(
        agents=1,
        timing=FAST,
        agent_scores={
            "enabled": True,
            "start_delay_s": 0.0,
            "cache_poll_s": 1.0,
            "budget_s": 20.0,
            "case_timeout_s": 10.0,
        },
        agent_env={"MESHFORGE_BENCH_DISABLE": "torch,numpy"},
    )
    node = fabric.nodes[0]

    def measured() -> bool:
        detail = fabric.node_detail(node)
        return detail is not None and detail["scores"]["status"] == "measured"

    took = fabric.wait_until(measured, within=SCORED_WITHIN_S, what="scores measured")
    (reported,) = node.process.log.events("agent.scores.reported")
    run_id = reported["run_id"]

    result = _compute(fabric, "nodes", "--json")
    assert result.returncode == 0, result.stderr
    listed = {n["node_id"]: n for n in json.loads(result.stdout)["nodes"]}
    scores: dict[str, Any] = listed[node.node_id]["scores"]
    assert scores["status"] == "measured"
    assert scores["run_id"] == run_id
    by_name = {m["name"]: m for m in scores["metrics"] if m["gpu_index"] is None}
    assert set(by_name) == HOST_METRICS
    for metric in by_name.values():
        assert metric["status"] == "unavailable"
        assert metric["reason"] == "no_blas_backend"
        assert metric["mean"] is None  # never a 0.0 standing in for unknown
    for metric in scores["metrics"]:
        if metric["gpu_index"] is not None:  # a GPU machine: typed, never a number
            assert metric["status"] == "unavailable" and metric["mean"] is None
    assert scores["conditions"]["priority_lowered"] in (True, False)

    table = _compute(fabric, "nodes", node.node_id)
    assert table.returncode == 0, table.stderr
    assert "hardware scores (measured)" in table.stdout
    assert "no_blas_backend" in table.stdout

    # Restart on the same data dir: the cache is valid, so the Hello carries it
    # and nothing is measured again.
    node.restart(graceful=True)
    fabric.wait_log(
        node.process, "agent.scores.hello_cache_valid", within=START_TIMEOUT_S
    )
    fabric.wait_all_connected(within=START_TIMEOUT_S)
    fabric.wait_log(node.process, "agent.scores.cache_valid", within=START_TIMEOUT_S)
    assert node.process.log.events("agent.scores.scheduled") == []
    detail = fabric.node_detail(node)
    assert detail is not None
    assert detail["scores"]["status"] == "measured"
    assert detail["scores"]["run_id"] == run_id
    record_timing(
        "s agent start -> scores measured (hooked scorer)", took, None, FAST.name
    )
