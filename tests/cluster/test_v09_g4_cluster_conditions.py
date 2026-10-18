"""V0.9 G4 lane B exit criterion (brief §5e): conditions on a 2-node harness.

A controller and two agents run as processes (subprocess runtime). With the
real CLI: ``compute bench create`` a campaign with ``none`` + ``cpu_load:1`` +
``queue:2`` + ``cache:cold`` x (local on A, mode1 on B) x 3 repetitions,
``compute bench submit --wait``, ``compute telemetry export`` twice. Then one
**forced verification failure**: ``netem:loss=1%`` on B - the agents run a
fake ``tc`` (never the real one, never sudo) with ``netem_interface = eth9``,
while their traffic to the controller leaves through loopback, so the agent
refuses to shape and fails the run ``condition_failed``.

Checked: every run ``done``; one record per run, each with its campaign
identity and class ``ok``; every record's condition outcome verified with a
measured value (agent-side: load cores, cache blobs; controller-side: fillers
ahead); the fillers are no campaign records; exports byte-identical; the
forced failure is ``failed`` with records ``condition_failed`` and the fake
``tc`` never asked to change anything. Once lane A's schema v2 is merged the
export itself carries ``condition`` / ``condition_measured`` and they are
checked there too.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import subprocess
import sys
import textwrap
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
from sqlalchemy import select

from meshforge.controller.bench.conditions import bench_identities
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.models.job import Attempt, Job
from meshforge.controller.telemetry.conditions import condition_outcome
from meshforge.controller.telemetry.schema import COLUMN_NAMES

pytestmark = [pytest.mark.cluster, pytest.mark.timeout(1200)]

TIMING = Timing(
    name="conditions",
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
FAKE_TC = textwrap.dedent(
    """
    import json, sys
    log_file, *args = sys.argv[1:]
    with open(log_file, "a") as log:
        log.write(json.dumps(args) + "\\n")
    if args[:2] == ["qdisc", "show"]:
        print("qdisc fq_codel 0: root refcnt 2")
        sys.exit(0)
    sys.exit(2)  # this test never lets anything be shaped
    """
)


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
        timeout=1100,
        check=False,
    )


def _spec(
    name: str, conditions: str, *, origin: str, strategies: str, reps: int = 3
) -> str:
    return (
        f"name: {name}\n"
        "seed: 41\n"
        "families:\n"
        "  - family: cpu.primes\n"
        f"conditions: {conditions}\n"
        f"strategies: {strategies}\n"
        f"origin: {origin}\n"
        f"repetitions: {reps}\n"
        "session_label: harness-loopback\n"
        "budget: {max_runs: 50, max_hours: 2}\n"
    )


def _rows(fabric: Fabric, campaign_id: str) -> list[dict[str, Any]]:
    resp = fabric.call(
        "GET", f"/v1/bench/campaigns/{campaign_id}/runs", params={"limit": 100}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json()["runs"])


def _create_and_run(
    fabric: Fabric, a_data: Path, work: Path, spec: str, runs: int
) -> str:
    path = work / f"spec-{time.monotonic_ns()}.yaml"
    path.write_text(spec)
    created = _compute(fabric, a_data, "bench", "create", str(path), "--json", cwd=work)
    assert created.returncode == 0, created.stderr
    campaign_id = str(json.loads(created.stdout)["campaign_id"])
    submitted = _compute(
        fabric,
        a_data,
        "bench",
        "submit",
        campaign_id,
        "-n",
        str(runs),
        "--wait",
        "--timeout",
        "1000",
        "--json",
        cwd=work,
    )
    # exit 126 = a waited run failed (the forced-failure campaign expects it)
    assert submitted.returncode in (0, 126), (
        submitted.stdout[-3000:],
        submitted.stderr[-3000:],
    )
    assert json.loads(submitted.stdout)["submitted"] == runs
    return campaign_id


def _export(fabric: Fabric, campaign_id: str, rows: int) -> bytes:
    deadline = time.monotonic() + 30
    while True:
        answer = fabric.call(
            "GET", "/v1/telemetry/export", params={"campaign": campaign_id}
        )
        assert answer.status_code == 200, answer.text
        if answer.headers["x-meshforge-telemetry-rows"] == str(rows):
            return answer.content
        assert time.monotonic() < deadline, answer.headers
        time.sleep(0.5)


async def _outcomes(pg_settings: Any, job_ids: list[str]) -> dict[str, Any]:
    """The record builder's condition outcome from the rows the recorder
    reads (until lane A's columns carry it in the export)."""
    engine = create_engine(pg_settings)
    try:
        async with create_session_factory(engine)() as db:
            identities = await bench_identities(db, job_ids)
            attempts = list(
                await db.scalars(select(Attempt).where(Attempt.job_id.in_(job_ids)))
            )
            extra = list(
                await db.scalars(
                    select(Job.idempotency_key).where(
                        Job.idempotency_key.like("bench:%:filler:%")
                    )
                )
            )
    finally:
        await engine.dispose()
    out = {
        a.job_id: condition_outcome(
            campaign=identities.get(a.job_id), workload=a.workload, state=a.state
        )
        for a in attempts
    }
    out["__fillers__"] = extra
    return out


def test_conditions_campaign_on_two_nodes_with_a_forced_failure(
    make_fabric: Callable[..., Fabric], tmp_path: Path, pg_settings: Any
) -> None:
    fake_tc = tmp_path / "fake_tc.py"
    fake_tc.write_text(FAKE_TC)
    tc_log = tmp_path / "tc-log.jsonl"
    fabric = make_fabric(
        agents=2,
        timing=TIMING,
        agent_execution=EXEC,
        controller_jobs={"dispatch_interval_s": 0.1},
        agent_env={"DOCKER_HOST": "unix:///nonexistent/meshforge-test.sock"},
        agent_extra={
            "conditions": {
                "verify_window_s": 1.0,
                "netem": True,
                "netem_interface": "eth9",
                "tc_command": [sys.executable, str(fake_tc), str(tc_log)],
            }
        },
    )
    a, b = fabric.nodes[0], fabric.nodes[1]
    work = tmp_path / "cli"
    work.mkdir()

    # ---------------------------------------------------- the campaign
    started = time.monotonic()
    campaign_id = _create_and_run(
        fabric,
        a.data_dir,
        work,
        _spec(
            "g4-cluster",
            "[none, cpu_load:1, queue:2, cache:cold]",
            origin=a.node_id,
            strategies="[local, mode1]",
        ),
        24,
    )
    took = time.monotonic() - started
    runs = _rows(fabric, campaign_id)
    assert {r["state"] for r in runs} == {"done"}, [
        (r["conditions"], r["state"], r["detail"]) for r in runs
    ]
    first = _export(fabric, campaign_id, 24)
    second = _export(fabric, campaign_id, 24)
    assert first == second  # checklist 31
    table = pq.read_table(io.BytesIO(first))
    records = table.to_pylist()
    by_job = {r["job_id"]: r for r in runs}
    assert {r["job_id"] for r in records} == set(by_job)  # fillers are no records here
    outcomes = asyncio.run(_outcomes(pg_settings, list(by_job)))
    assert len(outcomes.pop("__fillers__")) == 12  # 6 queue runs x 2 fillers
    for record in records:
        run = by_job[record["job_id"]]
        condition = run["conditions"][0]
        assert record["campaign_id"] == campaign_id  # checklist 29
        assert record["config_id"] == run["config_id"]
        assert record["repetition"] == run["repetition"]
        assert record["failure_class"] == "ok", record
        outcome = outcomes[record["job_id"]]
        assert outcome.condition == condition
        measured = outcome.measured
        assert measured is not None and measured["verified"] is True, (
            condition,
            measured,
        )
        assert measured["session_label"] == "harness-loopback"
        if condition == "cpu_load:1":
            assert measured["agent"]["load_cores"] >= 0.8
        if condition == "cache:cold":
            assert measured["agent"]["blobs_hit"] == 0
        if condition == "queue:2":
            assert measured["controller"]["fillers_ahead"] == 2
        # schema v2 columns, written by the one writer (condition_outcome)
        assert {"condition", "condition_measured"} <= set(COLUMN_NAMES)
        assert record["condition"] == condition
        stored = json.loads(record["condition_measured"])
        assert stored["verified"] is True
        assert stored["session_label"] == "harness-loopback"
        assert stored == json.loads(json.dumps(measured))  # same as the outcome

    # ---------------------------------------------------- forced failure
    failing = _create_and_run(
        fabric,
        a.data_dir,
        work,
        _spec(
            "g4-forced-fail", "[netem:loss=1%]", origin=b.node_id, strategies="[local]"
        ),
        3,
    )
    failed_runs = _rows(fabric, failing)
    assert {r["state"] for r in failed_runs} == {"failed"}
    failed_records = pq.read_table(io.BytesIO(_export(fabric, failing, 3))).to_pylist()
    assert {r["failure_class"] for r in failed_records} == {"condition_failed"}
    assert {r["failure_type"] for r in failed_records} == {"config_error"}
    for record in failed_records:
        assert record["condition"] == "netem:loss=1%"
        assert json.loads(record["condition_measured"])["verified"] is False
    forced = asyncio.run(_outcomes(pg_settings, [r["job_id"] for r in failed_runs]))
    forced.pop("__fillers__")
    for outcome in forced.values():
        assert outcome.failed and "leaves through" in outcome.measured["reason"]
    calls = [json.loads(line) for line in tc_log.read_text().splitlines()]
    assert calls and all(c[:2] == ["qdisc", "show"] for c in calls)  # nothing shaped
    print(  # noqa: T201
        f"campaign {campaign_id}: 24 runs (+12 fillers) in {took:.1f} s on 2 nodes; "
        f"forced netem failure -> {len(failed_records)} condition_failed records"
    )
