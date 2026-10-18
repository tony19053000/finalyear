"""V0.9 G5 lane B exit criterion (brief §5f): the unattended runner on a 2-node harness.

A controller and two agents run as processes (subprocess runtime). A 32-run
campaign (2 families x local/mode1 x 8 repetitions, origin = node A) is
``compute bench start``-ed with the real CLI. Mid-way the controller is
**SIGKILLed and restarted**; later node B's owner **pauses benchmarks for
20 s** (``[agent.policy] pause_benchmarks`` in its live policy file - what
``meshforge-agent bench pause`` writes). Checked:

* the campaign completes: every run ends ``done`` / ``failed`` / ``skipped``
  (``done`` expected), campaign ``done`` / ``complete``;
* **no (config, repetition, generation) submitted twice**: every benchmark
  job key ``bench:<run>[:<g>]`` exists once, each run's jobs are its
  generations 0..g;
* while B is paused no new benchmark job is dispatched to it, and a B attempt
  stopped by the pause ends ``owner_preempted`` and its run is requeued (tries
  unchanged), excluded from the infra failure rate;
* ``compute bench status`` adds up; two exports are byte-identical
  (checklist 31). Labels determinism is lane A's route (checked after the
  lanes merge).
"""

from __future__ import annotations

import asyncio
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

from mf_cluster import Fabric, Timing
from sqlalchemy import text

from meshforge.agent.policy import write_overlay
from meshforge.controller.bench.keys import run_of_key
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(1500)]

TIMING = Timing(
    name="runner",
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
RUNS = 32
PAUSE_S = 20.0


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
        timeout=120,
        check=False,
    )


def _spec(origin: str) -> str:
    return (
        "name: g5-cluster\n"
        "seed: 51\n"
        "families:\n"
        "  - family: cpu.primes\n"
        "  - family: cpu.zlib\n"
        "strategies: [local, mode1]\n"
        f"origin: {origin}\n"
        "repetitions: 8\n"
        "budget: {max_runs: 40, max_hours: 2}\n"
    )


def _runs(fabric: Fabric, cid: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 60
    while True:
        try:
            resp = fabric.call(
                "GET", f"/v1/bench/campaigns/{cid}/runs", params={"limit": 100}
            )
        except Exception:  # noqa: BLE001 - the controller is restarting
            resp = None
        if resp is not None and resp.status_code == 200:
            return list(resp.json()["runs"])
        assert time.monotonic() < deadline, "controller unreachable"
        time.sleep(0.5)


def _wait_runs(
    fabric: Fabric,
    cid: str,
    until: Callable[[list[dict[str, Any]]], bool],
    within: float,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + within
    while True:
        runs = _runs(fabric, cid)
        if until(runs):
            return runs
        assert time.monotonic() < deadline, sorted(
            (r["state"], r["node_id"]) for r in runs
        )
        time.sleep(0.5)


def _done(n: int) -> Callable[[list[dict[str, Any]]], bool]:
    return lambda runs: sum(1 for r in runs if r["state"] == "done") >= n


def _ended(runs: list[dict[str, Any]]) -> bool:
    return all(r["state"] in ("done", "failed", "skipped") for r in runs)


def _db_rows(pg_settings: Any, sql: str, **params: Any) -> list[tuple[Any, ...]]:
    async def go() -> list[tuple[Any, ...]]:
        engine = create_engine(pg_settings)
        try:
            async with engine.connect() as conn:
                result = await conn.execute(text(sql), params)
                return [tuple(r) for r in result]
        finally:
            await engine.dispose()

    return asyncio.run(go())


def _db_now(pg_settings: Any) -> Any:
    return _db_rows(pg_settings, "SELECT now()")[0][0]


def test_runner_survives_a_controller_sigkill_and_an_owner_pause(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra={"bench": {"tick_s": 0.5}},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    a, b = fabric.nodes[0], fabric.nodes[1]
    work = tmp_path / "cli"
    work.mkdir()
    spec = work / "campaign.yaml"
    spec.write_text(_spec(a.node_id))
    created = _compute(
        fabric, a.data_dir, "bench", "create", str(spec), "--json", cwd=work
    )
    assert created.returncode == 0, created.stderr
    cid = json.loads(created.stdout)["campaign_id"]
    assert json.loads(created.stdout)["run_count"] == RUNS
    started = _compute(
        fabric,
        a.data_dir,
        "bench",
        "start",
        cid,
        "--per-node",
        "1",
        "--cooldown",
        "0.5",
        "--json",
        cwd=work,
    )
    assert started.returncode == 0, started.stderr
    assert json.loads(started.stdout)["runner_owned"] is True
    t0 = time.monotonic()

    # 1. controller SIGKILL + restart mid-way: the runner resumes from Postgres
    _wait_runs(fabric, cid, _done(6), within=300)
    fabric.restart_controller(graceful=False)
    _wait_runs(fabric, cid, _done(12), within=300)

    # 2. B's owner pauses benchmarks for 20 s (what `bench pause` writes)
    overlay = b.data_dir / "policy.toml"
    write_overlay(overlay, {"pause_benchmarks": True})
    time.sleep(3.0)  # the agent reloads (1 s) and the controller hears of it
    window_start = _db_now(pg_settings)
    time.sleep(PAUSE_S - 3.0)
    window_end = _db_now(pg_settings)
    write_overlay(overlay, {"pause_benchmarks": False})
    started_on_b = _db_rows(
        pg_settings,
        "SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id "
        "WHERE a.node_id = :b AND j.idempotency_key LIKE 'bench:%' "
        "AND a.created_at BETWEEN :s AND :e",
        b=b.node_id,
        s=window_start,
        e=window_end,
    )[0][0]
    assert started_on_b == 0  # no benchmark dispatched to B while paused

    runs = _wait_runs(fabric, cid, _ended, within=900)
    took = time.monotonic() - t0
    assert {r["state"] for r in runs} == {"done"}, [
        (r["state"], r["detail"]) for r in runs if r["state"] != "done"
    ]
    detail = fabric.call("GET", f"/v1/bench/campaigns/{cid}").json()
    assert detail["state"] == "done"

    # 3. no (config, repetition, generation) twice
    keys = [
        str(k)
        for (k,) in _db_rows(
            pg_settings,
            "SELECT idempotency_key FROM job WHERE idempotency_key LIKE 'bench:%'",
        )
    ]
    assert len(keys) == len(set(keys))
    by_run: dict[str, list[int]] = {}
    for key in keys:
        found = run_of_key(key)
        if found is not None:
            by_run.setdefault(found[0], []).append(found[1])
    run_ids = {r["run_id"] for r in runs}
    assert set(by_run) == run_ids
    rows = {
        str(rid): (int(tries), int(gen))
        for rid, tries, gen in _db_rows(
            pg_settings,
            "SELECT id, tries, generation FROM bench_run WHERE campaign_id = :c",
            c=cid,
        )
    }
    for rid, gens in by_run.items():
        assert sorted(gens) == list(range(rows[rid][1] + 1)), (rid, gens, rows[rid])
    combos = [(r["config_id"], r["repetition"]) for r in runs]
    assert len(combos) == len(set(combos)) == RUNS

    # 4. a B attempt stopped by the pause: owner_preempted, run requeued
    preempted = _db_rows(
        pg_settings,
        "SELECT j.id, j.idempotency_key FROM job j "
        "WHERE j.failure_type = 'owner_preempted' AND j.idempotency_key LIKE 'bench:%'",
    )
    for _job, key in preempted:
        found = run_of_key(key)
        assert found is not None and rows[found[0]][1] > found[1]  # requeued
    status_out = _compute(
        fabric, a.data_dir, "bench", "status", cid, "--json", cwd=work
    )
    assert status_out.returncode == 0, status_out.stderr
    status = json.loads(status_out.stdout)
    assert status["state"] == "done" and status["finished_reason"] == "complete"
    assert status["counts"]["done"] == RUNS

    # 5. export determinism (checklist 31)
    deadline = time.monotonic() + 60
    while True:
        answer = fabric.call("GET", "/v1/telemetry/export", params={"campaign": cid})
        assert answer.status_code == 200, answer.text
        if int(answer.headers["x-meshforge-telemetry-rows"]) >= RUNS + len(preempted):
            break
        assert time.monotonic() < deadline, answer.headers
        time.sleep(0.5)
    status = json.loads(
        _compute(fabric, a.data_dir, "bench", "status", cid, "--json", cwd=work).stdout
    )
    assert status["preempted"] == len(preempted)
    # pre-emptions are not infrastructure failures (D246)
    assert status["infra_failures"] <= status["records"] - status["preempted"]
    first = fabric.call("GET", "/v1/telemetry/export", params={"campaign": cid}).content
    second = fabric.call(
        "GET", "/v1/telemetry/export", params={"campaign": cid}
    ).content
    assert first == second
    print(  # noqa: T201
        f"campaign {cid}: {RUNS} runs in {took:.1f} s on 2 nodes across a controller "
        f"SIGKILL and a {PAUSE_S:g} s owner pause; {len(preempted)} attempt(s) "
        f"pre-empted; {len(keys)} benchmark jobs"
    )
