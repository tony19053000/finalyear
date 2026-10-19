"""V0.9 G7 tester: an agent SIGKILLed while a benchmark run is in flight.

The G7 campaign test kills the *controller*; agents keep running their jobs,
so the infrastructure-retry path (``tries + 1``, outcome.py) is never taken.

dies with it), restarted, and the campaign must still finish with **exactly
one ok record per run**, the killed run re-executed (second attempt of the same job), the
failed attempt recorded as an infrastructure failure (not silently dropped
and not double-counted), unique job keys with contiguous generations, and an
export that is equal to the API and stable across a controller SIGKILL.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("psutil", reason="the cluster harness needs the agent extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from mf_cluster import Fabric, Timing
from sqlalchemy import text

from meshforge.controller.bench.keys import run_of_key
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(900)]

TIMING = Timing(
    name="g7-infra-retry",
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
REPETITIONS = 5
RUNS = 3 * REPETITIONS


def _cli(fabric: Fabric, cwd: Path, *args: str) -> dict[str, Any]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": fabric.http_base,
            "MESHFORGE_CLI__CA_FILE": str(fabric.ca_dir / "ca.crt"),
            "MESHFORGE_CLI__TOKEN": fabric.api_token(),
            "MESHFORGE_DATA_DIR": str(cwd / "local"),
            "XDG_CONFIG_HOME": str(cwd / "cli-config"),
        }
    )
    deadline = time.monotonic() + 60
    while True:
        out = subprocess.run(
            [sys.executable, "-m", "meshforge.cli", *args, "--json"],
            env=env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if out.returncode == 0:
            return dict(json.loads(out.stdout))
        assert time.monotonic() < deadline, (args, out.stdout, out.stderr)
        time.sleep(1.0)


def _rows(pg_settings: Any, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async def go() -> list[tuple[Any, ...]]:
        engine = create_engine(pg_settings)
        try:
            async with engine.connect() as conn:
                return [tuple(r) for r in await conn.execute(text(sql), params)]
        finally:
            await engine.dispose()

    return asyncio.run(go())


def test_an_agent_killed_mid_run_is_retried_with_exactly_one_ok_record_per_run(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = make_fabric(
        agents=3,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra={"bench": {"tick_s": 0.5}},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    a, b, _c = fabric.nodes
    (tmp_path / "local").mkdir()
    spec = tmp_path / "c.yaml"
    spec.write_text(
        "name: g7-retry\nseed: 72\nfamilies:\n"
        "  - family: cpu.primes\n"
        "    params: {limit: 100000000, repeats: 4}\n"
        "strategies: [local, mode1]\nnodes: all\n"
        f"origin: {a.node_id}\nrepetitions: {REPETITIONS}\n"
        "budget: {max_runs: 40, max_hours: 2}\n"
    )
    cid = _cli(fabric, tmp_path, "bench", "create", str(spec))["campaign_id"]
    _cli(
        fabric, tmp_path, "bench", "start", cid, "--per-node", "1", "--cooldown", "0.5"
    )

    # wait for a benchmark attempt to be running on B, then SIGKILL its agent
    running_sql = (
        "SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id "
        "WHERE a.node_id = :n AND a.state = 'running' "
        "AND j.idempotency_key LIKE 'bench:%'"
    )
    deadline = time.monotonic() + 120
    while not int(_rows(pg_settings, running_sql, n=b.node_id)[0][0]):
        assert time.monotonic() < deadline, "no benchmark attempt ran on B"
        time.sleep(0.05)
    time.sleep(1.0)  # well inside the ~3-4 s of compute
    b.kill()
    time.sleep(2.0)
    b.process.start()
    b.restarts += 1

    deadline = time.monotonic() + 420
    while True:
        status = _cli(fabric, tmp_path, "bench", "status", cid)
        assert sum(int(v) for v in status["counts"].values()) == RUNS, status
        assert 0.0 <= float(status["done_percent"]) <= 100.0
        if status["state"] == "done":
            break
        assert time.monotonic() < deadline, status
        time.sleep(1.0)

    runs = _cli(fabric, tmp_path, "bench", "runs", cid)["runs"]
    assert len(runs) == RUNS
    assert Counter(r["state"] for r in runs) == {"done": RUNS}, runs
    # The kill is absorbed by the job layer (a second attempt of the SAME job
    # after ``node_lost``), so the bench-level ``tries`` normally stays 0; if
    # the bench layer did requeue, ``tries`` and the generation must agree.
    lost = _rows(
        pg_settings,
        "SELECT j.idempotency_key, a.node_id FROM attempt a JOIN job j "
        "ON j.id = a.job_id WHERE j.idempotency_key LIKE 'bench:%' "
        "AND a.failure_type = 'node_lost'",
    )
    assert lost, "the kill on B produced no node_lost attempt"
    assert {str(n) for _k, n in lost} == {b.node_id}
    assert status["retries"] == sum(r["tries"] for r in runs)

    # job keys unique; generations of each run are contiguous 0..generation
    keys = [
        str(k)
        for (k,) in _rows(
            pg_settings,
            "SELECT idempotency_key FROM job WHERE idempotency_key LIKE 'bench:%'",
        )
    ]
    assert len(keys) == len(set(keys))
    gens: dict[str, list[int]] = {}
    for key in keys:
        found = run_of_key(key)
        assert found is not None, key
        gens.setdefault(found[0], []).append(found[1])
    assert set(gens) == {r["run_id"] for r in runs}
    for rid, seen in gens.items():
        assert sorted(seen) == list(range(len(seen))), (rid, seen)
    tries = {r["run_id"]: r["tries"] for r in runs}
    for rid, seen in gens.items():
        assert len(seen) <= 1 + tries[rid], (rid, seen, tries[rid])

    attempts = int(
        _rows(
            pg_settings,
            "SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id "
            "WHERE j.idempotency_key LIKE 'bench:%'",
        )[0][0]
    )
    assert attempts >= RUNS + 1, attempts

    def export_rows(expected: int) -> bytes:
        deadline = time.monotonic() + 90
        while True:
            try:
                ans = fabric.call(
                    "GET", "/v1/telemetry/export", params={"campaign": cid}
                )
            except Exception:  # noqa: BLE001 - controller restarting
                ans = None
            if ans is not None and ans.status_code == 200:
                n = int(ans.headers["x-meshforge-telemetry-rows"])
                if n == expected:
                    return bytes(ans.content)
                assert n < expected, (n, expected)
            assert time.monotonic() < deadline, expected
            time.sleep(0.5)

    first = export_rows(attempts)
    fabric.restart_controller(graceful=False)
    fabric.wait_all_connected(within=60)
    second = export_rows(attempts)
    assert first == second

    records = pq.read_table(io.BytesIO(first)).to_pylist()
    ok = Counter(
        (r["config_id"], r["repetition"]) for r in records if r["failure_class"] == "ok"
    )
    assert len(ok) == RUNS and set(ok.values()) == {1}, ok
    for run in runs:
        assert ok[(run["config_id"], run["repetition"])] == 1
    failed = [r for r in records if r["failure_class"] != "ok"]
    assert len(failed) == attempts - RUNS >= 1
    assert all(r["failure_class"] == "infrastructure" for r in failed), failed
    assert all(r["node_id"] == b.node_id for r in failed)
    # a job retried after node_lost has one failed attempt and ONE ok record
    ok_jobs = Counter(r["job_id"] for r in records if r["failure_class"] == "ok")
    assert set(ok_jobs.values()) == {1}
    assert {r["job_id"] for r in failed} <= set(ok_jobs)
