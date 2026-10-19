"""V0.9 G7 (brief §5i): a >= 200-run campaign end to end on a 3-agent harness.

Success criteria 1-3 of roadmap V0.9 at harness scale. A controller and three
agents run as processes (subprocess runtime); everything the operator does
goes through the real ``compute`` CLI:

1. ``bench plan`` (count = expected) -> ``bench create`` -> ``bench start``.
   The spec: 2 stdlib families (``cpu.primes``, ``cpu.zlib``: no package
   download - pypi.org is unreachable from the dev host) x 2 parameter points
   x 3 nodes (origin A: ``local`` on A, ``mode1`` pinned to B and C) x 17
   repetitions = 204 runs.
2. When ``bench status`` shows 30-60 % done the controller is **SIGKILLed**
   and restarted on the same database / data dir; nobody calls ``start``
   again - the runner resumes by itself.
3. Node C's owner pauses benchmarks (the live policy overlay
   ``meshforge-agent bench pause`` writes) while a benchmark attempt runs
   there: it is pre-empted (``owner_preempted``, not an infrastructure
   failure), nothing is dispatched to C while paused, and C gets benchmark
   work again after the resume.
4. The campaign completes. Checked from the records (the Parquet export):
   every run has **exactly one** ok record (no run lost, none executed
   twice; a run in flight at the kill is followed to its end), every
   benchmark job key is unique, ``bench status`` equals ``bench runs`` and
   the records, the infrastructure failure rate is < 2 % (criterion 1, from
   ``bench status`` and recomputed), ``bench report`` shows every
   configuration with n >= 3 and a CV (criterion 3; ``--strict`` exits 0),
   ``bench labels`` is non-empty.
5. ``compute telemetry export --campaign`` before and after one more
   controller SIGKILL + restart: byte-identical files, equal to
   ``GET /v1/telemetry/export?campaign=``, one row per run record
   (criterion 2).

"No confidently-wrong output": every status poll is checked for
``done_percent`` in 0..100, a non-negative ETA and counts that add up to the
run count.
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

from meshforge.agent.policy import write_overlay
from meshforge.controller.bench.keys import run_of_key
from meshforge.controller.store.engine import create_engine

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(1800)]

TIMING = Timing(
    name="g7-campaign",
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
REPETITIONS = 17
GROUPS = 4  # 2 families x 2 parameter points
NODES = 3
RUNS = GROUPS * NODES * REPETITIONS  # 204
PAUSE_S = 10.0
COOLDOWN_S = 0.5
OWNER_PREEMPTED = "owner_preempted"
RUN_STATES = ("planned", "submitted", "done", "failed", "skipped")
TERMINAL_RUN = ("done", "failed", "skipped")


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
        timeout=180,
        check=False,
    )


def _spec(origin: str) -> str:
    # Smallest parameters inside the 5 s - 3 min window (G2): ~3-4 s of
    # compute per run on the dev host; no numpy (needs_numpy = False).
    return (
        "name: g7-e2e\n"
        "seed: 71\n"
        "families:\n"
        "  - family: cpu.primes\n"
        "    params: {limit: [100000000, 110000000], repeats: 4}\n"
        "  - family: cpu.zlib\n"
        "    params: {size_mb: [64, 72], level: 6, repeats: 2}\n"
        "strategies: [local, mode1]\n"
        "nodes: all\n"
        f"origin: {origin}\n"
        f"repetitions: {REPETITIONS}\n"
        "budget: {max_runs: 250, max_hours: 4}\n"
    )


class Campaign:
    """The CLI view of one campaign plus the checks every poll makes."""

    def __init__(self, fabric: Fabric, local_data: Path, work: Path, cid: str):
        self.fabric = fabric
        self.local_data = local_data
        self.work = work
        self.cid = cid

    def cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return _compute(self.fabric, self.local_data, *args, cwd=self.work)

    def cli_json(self, *args: str, within: float = 60.0) -> dict[str, Any]:
        """``compute <args> --json``; retried while the controller restarts."""
        deadline = time.monotonic() + within
        while True:
            out = self.cli(*args, "--json")
            if out.returncode == 0:
                return dict(json.loads(out.stdout))
            assert time.monotonic() < deadline, (args, out.stdout, out.stderr)
            time.sleep(1.0)

    def status(self) -> dict[str, Any]:
        doc = self.cli_json("bench", "status", self.cid)
        counts = doc["counts"]
        # no confidently-wrong output: counters add up, % and ETA in range
        assert sum(int(counts.get(s, 0)) for s in RUN_STATES) == RUNS, doc
        assert 0.0 <= float(doc["done_percent"]) <= 100.0, doc
        assert doc["eta_s"] is None or float(doc["eta_s"]) >= 0.0, doc
        return doc

    def runs(self) -> list[dict[str, Any]]:
        doc = self.cli_json("bench", "runs", self.cid)
        assert doc["count"] == len(doc["runs"]) == RUNS, doc["count"]
        return list(doc["runs"])

    def dump(self) -> str:
        """Diagnostics for a failed wait: status + the runs not done."""
        try:
            status = self.cli("bench", "status", self.cid, "--json").stdout
            runs = json.loads(self.cli("bench", "runs", self.cid, "--json").stdout)
            open_runs = [
                (r["seq"], r["state"], r["node_id"][:12], r["tries"], r["last_failure"])
                for r in runs["runs"]
                if r["state"] != "done"
            ]
        except Exception as exc:  # noqa: BLE001 - diagnostics only
            return f"(no diagnostics: {exc!r})\n{self.fabric.describe()}"
        return f"status: {status}\nnot done: {open_runs[:60]}\n{self.fabric.describe()}"

    def wait_status(
        self, until: Callable[[dict[str, Any]], bool], within: float, what: str
    ) -> dict[str, Any]:
        deadline = time.monotonic() + within
        while True:
            doc = self.status()
            if until(doc):
                return doc
            if time.monotonic() > deadline:
                pytest.fail(f"{what}: not reached in {within:g} s\n{self.dump()}")
            time.sleep(1.0)


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


def _bench_attempts_on(pg_settings: Any, node_id: str, *, since: Any = None) -> int:
    sql = (
        "SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id "
        "WHERE a.node_id = :n AND j.idempotency_key LIKE 'bench:%'"
    )
    if since is not None:
        sql += " AND a.created_at > :s"
        return int(_db_rows(pg_settings, sql, n=node_id, s=since)[0][0])
    return int(_db_rows(pg_settings, sql, n=node_id)[0][0])


def _running_bench_attempt(pg_settings: Any, node_id: str) -> bool:
    return bool(
        _db_rows(
            pg_settings,
            "SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id "
            "WHERE a.node_id = :n AND a.state = 'running' "
            "AND j.idempotency_key LIKE 'bench:%'",
            n=node_id,
        )[0][0]
    )


def _preempted_on(pg_settings: Any, node_id: str) -> int:
    return int(
        _db_rows(
            pg_settings,
            "SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id "
            "WHERE a.node_id = :n AND j.idempotency_key LIKE 'bench:%' "
            "AND a.failure_type = :f",
            n=node_id,
            f=OWNER_PREEMPTED,
        )[0][0]
    )


def _db_now(pg_settings: Any) -> Any:
    return _db_rows(pg_settings, "SELECT now()")[0][0]


def _owner_pause(campaign: Campaign, pg_settings: Any, node: Any) -> tuple[int, float]:
    """Pause benchmarks on ``node`` while one of its benchmark attempts runs
    (retried until the pause catches one), hold it for :data:`PAUSE_S`,
    resume. Returns (pre-empted attempts, seconds until the node got
    benchmark work again)."""
    overlay = node.data_dir / "policy.toml"
    window_start = None
    for _try in range(8):
        deadline = time.monotonic() + 240
        while not _running_bench_attempt(pg_settings, node.node_id):
            if time.monotonic() > deadline:
                pytest.fail(f"no benchmark attempt ran on C\n{campaign.dump()}")
            time.sleep(0.05)
        write_overlay(overlay, {"pause_benchmarks": True})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _preempted_on(pg_settings, node.node_id):
                window_start = _db_now(pg_settings)
                break
            time.sleep(0.2)
        if window_start is not None:
            break
        # the attempt ended before the agent saw the file: resume, try again
        write_overlay(overlay, {"pause_benchmarks": False})
        time.sleep(2.0)
    if window_start is None:
        pytest.fail(f"no running benchmark attempt was pre-empted\n{campaign.dump()}")
    time.sleep(PAUSE_S)
    window_end = _db_now(pg_settings)
    write_overlay(overlay, {"pause_benchmarks": False})
    resumed_at = time.monotonic()
    started = int(
        _db_rows(
            pg_settings,
            "SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id "
            "WHERE a.node_id = :n AND j.idempotency_key LIKE 'bench:%' "
            "AND a.created_at BETWEEN :s AND :e",
            n=node.node_id,
            s=window_start,
            e=window_end,
        )[0][0]
    )
    assert started == 0, "a benchmark was dispatched to C while its owner paused"
    deadline = time.monotonic() + 120
    while not _bench_attempts_on(pg_settings, node.node_id, since=window_end):
        if time.monotonic() > deadline:
            pytest.fail(f"C got no benchmark work after the resume\n{campaign.dump()}")
        time.sleep(0.2)
    return _preempted_on(pg_settings, node.node_id), time.monotonic() - resumed_at


def _wait_export_rows(fabric: Fabric, cid: str, expected: int) -> None:
    """The record sweep runs every 2 s: wait until every attempt has its row."""
    deadline = time.monotonic() + 90
    while True:
        try:
            answer = fabric.call(
                "GET", "/v1/telemetry/export", params={"campaign": cid}
            )
        except Exception:  # noqa: BLE001 - the controller is restarting
            answer = None
        if answer is not None and answer.status_code == 200:
            rows = int(answer.headers["x-meshforge-telemetry-rows"])
            if rows == expected:
                return
            assert rows < expected, (rows, expected)
        assert time.monotonic() < deadline, (expected, answer and answer.headers)
        time.sleep(0.5)


def _export(campaign: Campaign, target: Path) -> bytes:
    out = campaign.cli_json(
        "telemetry", "export", "--campaign", campaign.cid, "-o", str(target)
    )
    data = target.read_bytes()
    assert out["rows"] == pq.read_metadata(io.BytesIO(data)).num_rows
    return data


def test_a_200_run_campaign_survives_a_controller_sigkill_and_an_owner_pause(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fabric = make_fabric(
        agents=NODES,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        controller_extra={"bench": {"tick_s": 0.5}},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
    )
    a, b, c = fabric.nodes
    work = tmp_path / "cli"
    work.mkdir()
    spec = work / "campaign.yaml"
    spec.write_text(_spec(a.node_id))
    t_start = time.monotonic()

    # 1. plan (count = expected) -> create -> start, through the real CLI
    planned = _compute(
        fabric, a.data_dir, "bench", "plan", str(spec), "--json", cwd=work
    )
    assert planned.returncode == 0, planned.stderr
    plan = json.loads(planned.stdout)
    assert (plan["runs"], plan["configs"], plan["groups"]) == (
        RUNS,
        GROUPS * NODES,
        GROUPS,
    ), plan
    created = _compute(
        fabric, a.data_dir, "bench", "create", str(spec), "--json", cwd=work
    )
    assert created.returncode == 0, created.stderr
    cid = json.loads(created.stdout)["campaign_id"]
    assert json.loads(created.stdout)["run_count"] == RUNS
    campaign = Campaign(fabric, a.data_dir, work, cid)
    stored = campaign.runs()
    assert {r["node_id"] for r in stored} == {a.node_id, b.node_id, c.node_id}
    assert Counter(r["node_id"] for r in stored) == {
        n.node_id: RUNS // NODES for n in (a, b, c)
    }
    started = campaign.cli_json(
        "bench",
        "start",
        cid,
        "--per-node",
        "1",
        "--cooldown",
        str(COOLDOWN_S),
    )
    assert started["runner_owned"] is True and started["state"] == "active", started

    # 2. SIGKILL the controller at 30-60 % done; the runner resumes by itself
    before_kill = campaign.wait_status(
        lambda d: float(d["done_percent"]) >= 30.0, within=600, what="30 % done"
    )
    assert float(before_kill["done_percent"]) <= 60.0, before_kill
    in_flight = {
        r["run_id"]: r["job_id"] for r in campaign.runs() if r["state"] == "submitted"
    }
    fabric.restart_controller(graceful=False)
    t_killed = time.monotonic()
    fabric.wait_all_connected(within=60)
    after = campaign.wait_status(
        lambda d: int(d["counts"]["done"]) >= int(before_kill["counts"]["done"]) + 6,
        within=180,
        what="progress after the restart (no second `bench start`)",
    )
    assert after["state"] == "active" and after["runner_owned"] is True, after

    # 3. owner guard on C: pre-empted, nothing dispatched while paused, resumes
    preempted_on_c, resume_s = _owner_pause(campaign, pg_settings, c)
    assert preempted_on_c >= 1

    # 4. completion
    final = campaign.wait_status(
        lambda d: d["state"] == "done", within=900, what="campaign done"
    )
    took = time.monotonic() - t_start
    assert final["finished_reason"] == "complete", final
    runs = campaign.runs()
    states = Counter(r["state"] for r in runs)
    # bench status == bench runs (counted over every page)
    assert {s: int(final["counts"].get(s, 0)) for s in RUN_STATES} == {
        s: states.get(s, 0) for s in RUN_STATES
    }
    assert states["planned"] == states["submitted"] == states["skipped"] == 0, states
    assert float(final["done_percent"]) == 100.0
    by_run = {r["run_id"]: r for r in runs}
    assert len({(r["config_id"], r["repetition"]) for r in runs}) == RUNS

    # no (run, generation) submitted twice; generations are 0..g per run
    keys = [
        str(k)
        for (k,) in _db_rows(
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
    assert set(gens) == set(by_run)
    generation = {
        str(rid): int(g)
        for rid, g in _db_rows(
            pg_settings,
            "SELECT id, generation FROM bench_run WHERE campaign_id = :c",
            c=cid,
        )
    }
    for rid, seen in gens.items():
        assert sorted(seen) == list(range(generation[rid] + 1)), (rid, seen)

    # every attempt has its record before anything is exported
    attempts = int(
        _db_rows(
            pg_settings,
            "SELECT count(*) FROM attempt a JOIN job j ON j.id = a.job_id "
            "WHERE j.idempotency_key LIKE 'bench:%'",
        )[0][0]
    )
    _wait_export_rows(fabric, cid, attempts)
    final = campaign.status()

    # 5. export before and after one more controller SIGKILL: byte-identical
    first = _export(campaign, work / "before.parquet")
    fabric.restart_controller(graceful=False)
    fabric.wait_all_connected(within=60)
    _wait_export_rows(fabric, cid, attempts)
    second = _export(campaign, work / "after.parquet")
    api = fabric.call("GET", "/v1/telemetry/export", params={"campaign": cid})
    assert api.status_code == 200, api.text
    assert first == second == api.content  # criterion 2

    records = pq.read_table(io.BytesIO(first)).to_pylist()
    assert len(records) == attempts == final["records"]
    assert all(r["campaign_id"] == cid for r in records)
    assert all(
        r["condition_measured"] is None
        or "bench_role" not in json.loads(r["condition_measured"])
        for r in records
    )

    # exactly one measured (ok) record per run: none lost, none executed twice
    ok = Counter(
        (r["config_id"], r["repetition"]) for r in records if r["failure_class"] == "ok"
    )
    for run in runs:
        combo = (run["config_id"], run["repetition"])
        if run["state"] == "done":
            assert ok[combo] == 1, (run, ok[combo])
        else:  # failed: only after its infra retries were spent, never silent
            assert run["state"] == "failed" and ok[combo] == 0, run
            assert run["last_failure"], run
    assert sum(ok.values()) == states["done"]
    ok_jobs = {r["job_id"] for r in records if r["failure_class"] == "ok"}
    for run in runs:
        if run["state"] == "done":
            assert run["job_id"] in ok_jobs, run
    in_flight_end = Counter(by_run[rid]["state"] for rid in in_flight)

    # criterion 1: infrastructure failures < 2 %, owner pre-emptions excluded
    infra = [
        r
        for r in records
        if r["failure_class"] == "infrastructure"
        and r["failure_type"] != OWNER_PREEMPTED
    ]
    preempted = [r for r in records if r["failure_type"] == OWNER_PREEMPTED]
    assert all(r["failure_class"] == "infrastructure" for r in preempted)
    assert final["infra_failures"] == len(infra)
    assert final["preempted"] == len(preempted) >= preempted_on_c
    assert all(r["node_id"] == c.node_id for r in preempted)
    rate = len(infra) / len(records)
    assert final["infra_failure_rate"] == pytest.approx(rate, abs=1e-4)
    assert rate < 0.02, [(r["failure_type"], r["node_id"]) for r in infra]

    # criterion 3: every configuration n >= 3 with a CV; --strict exits 0
    report = campaign.cli_json("bench", "report", cid)
    summary = report["summary"]
    assert summary["pending"] is False, summary
    assert summary["crit1_infra_ok"] is True and summary["crit3_reps_ok"] is True
    assert summary["infra_failures"] == len(infra)
    assert summary["owner_preempted"] == len(preempted)
    assert summary["records"] == len(records)
    assert len(report["configs"]) == GROUPS * NODES
    for config in report["configs"]:
        assert config["state"] == "measured", config
        assert config["n"] >= 3 and config["total_ms"]["n"] == config["n"], config
        assert config["total_ms"]["cv"] is not None, config
        assert config["reps_flag"] is False, config
    strict = campaign.cli("bench", "report", cid, "--strict")
    assert strict.returncode == 0, (strict.stdout[-2000:], strict.stderr[-2000:])
    labels = campaign.cli_json("bench", "labels", cid)
    assert len(labels["groups"]) == GROUPS, labels
    assert {g["label"] for g in labels["groups"]} <= {"best", "tie"}, labels

    cvs = sorted(float(cfg["total_ms"]["cv"]) for cfg in report["configs"])
    print(  # noqa: T201
        f"\ncampaign {cid}: {RUNS} runs on {NODES} nodes in {took:.0f} s "
        f"(controller SIGKILL at {float(before_kill['done_percent']):.1f} % done, "
        f"{time.monotonic() - t_killed:.0f} s before the end); "
        f"{len(in_flight)} run(s) in flight at the kill -> {dict(in_flight_end)}; "
        f"{len(records)} records, {len(infra)} infra failure(s) "
        f"({100 * rate:.2f} %), {len(preempted)} owner pre-emption(s), C busy "
        f"again {resume_s:.1f} s after the resume; {len(keys)} benchmark jobs; "
        f"retries {final['retries']}; CV total_ms min {cvs[0]:.3f} median "
        f"{cvs[len(cvs) // 2]:.3f} max {cvs[-1]:.3f}; labels "
        f"{dict(Counter(g['label'] for g in labels['groups']))}; export "
        f"{len(first)} bytes identical across a restart"
    )
