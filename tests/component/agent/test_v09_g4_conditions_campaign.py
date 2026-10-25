"""V0.9 G4 lane B on a real controller, real (in-process) agents and PostgreSQL.

A campaign with conditions is created and submitted through
``/v1/bench/campaigns``. Checked:

* support at **create**: a node that does not advertise a condition makes the
  spec a 400 listing node x condition; ``on_unsupported: skip`` stores those
  runs ``skipped`` with the reason;
* every condition end to end on one node - ``cpu_load:1`` / ``cache:cold`` /
  ``cache:warm`` applied and verified by the agent (its ``ConditionReport`` in
  ``attempt.workload``), ``queue:2`` = two filler jobs queued ahead (derived
  from the job rows), ``cache:warm`` = one warm-up job first; fillers and
  warm-ups are never campaign records; every campaign record's condition
  outcome is verified and its class ``ok``;
* a forced verification failure -> the run fails ``config_error``
  ``condition_failed:``, the run is ``failed``, its record is
  ``condition_failed`` (never labelled);
* ``unavailable:T`` pauses the origin: nothing new starts there during the
  pause, the mode1 run is dispatched within it.

The ``condition`` / ``condition_measured`` record columns arrive with lane A's
schema v2; until then the outcome is computed here with the record builder's
own function from the same rows the recorder reads.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of
from sqlalchemy import select

from meshforge.agent.conditions import load as load_module
from meshforge.controller.bench.conditions import bench_identities
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.models.job import Attempt, Job
from meshforge.controller.telemetry.conditions import condition_outcome
from meshforge.controller.telemetry.export import fetch_records

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(400)]

POLICY = {"accept_jobs": True, "allow_unsandboxed": True}


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        telemetry={"record_interval_s": 0.2},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers={"authorization": f"Bearer {token_of(runtime)}"},
        timeout=30,
    ) as client:
        yield client


@pytest_asyncio.fixture
async def engine(pg_settings: Any) -> AsyncIterator[Any]:
    eng = create_engine(pg_settings)
    yield eng
    await eng.dispose()


async def _agent(
    grpc_target: str,
    path: Path,
    *,
    conditions: dict[str, Any] | None = None,
    **policy: Any,
) -> ExecAgent:
    overrides: dict[str, Any] = {"policy": {**POLICY, **policy}}
    overrides["conditions"] = {"verify_window_s": 1.0, **(conditions or {})}
    return await ExecAgent.start(grpc_target, path, agent_overrides=overrides)


def _spec(
    conditions: str, *, origin: str | None, strategies: str = "[local]", extra: str = ""
) -> str:
    return (
        "name: g4-conditions\n"
        "seed: 9\n"
        "families:\n  - family: cpu.primes\n"
        f"conditions: {conditions}\n"
        f"strategies: {strategies}\n"
        + (f"origin: {origin}\n" if origin else "")
        + "repetitions: 3\n"
        "session_label: wifi\n"
        f"{extra}"
        "budget: {max_runs: 100}\n"
    )


async def _runs(http: httpx.AsyncClient, campaign_id: str) -> list[dict[str, Any]]:
    resp = await http.get(
        f"/v1/bench/campaigns/{campaign_id}/runs", params={"limit": 1000}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json()["runs"])


async def _create(http: httpx.AsyncClient, spec: str) -> dict[str, Any]:
    resp = await http.post("/v1/bench/campaigns", json={"spec": spec})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _submit_all(http: httpx.AsyncClient, campaign_id: str, count: int) -> None:
    resp = await http.post(
        f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": count}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["submitted"] == count, resp.json()


async def _wait_runs_ended(
    http: httpx.AsyncClient, campaign_id: str, within: float = 300.0
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + within
    while True:
        runs = await _runs(http, campaign_id)
        if all(r["state"] in ("done", "failed", "skipped") for r in runs):
            return runs
        assert time.monotonic() < deadline, [r["state"] for r in runs]
        await asyncio.sleep(0.5)


async def _wait_records(
    engine: Any, campaign_id: str, want: int, within: float = 30.0
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + within
    while True:
        rows = await fetch_records(engine, campaign_id=campaign_id)
        if len(rows) >= want or time.monotonic() > deadline:
            return rows
        await asyncio.sleep(0.2)


async def _outcomes(engine: Any, job_ids: list[str]) -> dict[str, Any]:
    """job_id -> (condition outcome, attempt workload), from the recorder's
    own sources (the campaign hook + attempt.workload)."""
    sessions = create_session_factory(engine)
    async with sessions() as db:
        identities = await bench_identities(db, job_ids)
        attempts = list(
            await db.scalars(select(Attempt).where(Attempt.job_id.in_(job_ids)))
        )
    out: dict[str, Any] = {}
    for attempt in attempts:
        out[attempt.job_id] = (
            condition_outcome(
                campaign=identities.get(attempt.job_id),
                workload=attempt.workload,
                state=attempt.state,
            ),
            attempt.workload,
            identities.get(attempt.job_id),
        )
    return out


async def test_unsupported_condition_is_a_create_error_or_skipped(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    b = await _agent(grpc_target, tmp_path / "b", conditions={"enabled": False})
    try:
        spec = _spec("[none, cpu_load:1]", origin=None)
        refused = await http.post("/v1/bench/campaigns", json={"spec": spec})
        assert refused.status_code == 400, refused.text
        problems = refused.json()["error"]["problems"]
        assert problems[0] == (
            f"node {b.node_id} x condition cpu_load:1: the node does not offer "
            "cpu_load (bench-conditions capability)"
        ), problems
        assert not any(a.node_id in p for p in problems)

        created = await _create(
            http,
            _spec("[none, cpu_load:1]", origin=None, extra="on_unsupported: skip\n"),
        )
        assert created["counts"]["skipped"] == 3 and created["counts"]["planned"] == 9
        assert created["summary"]["unsupported"] == 3
        assert created["summary"]["session_label"] == "wifi"
        skipped = [
            r
            for r in await _runs(http, created["campaign_id"])
            if r["state"] == "skipped"
        ]
        assert {(r["node_id"], tuple(r["conditions"])) for r in skipped} == {
            (b.node_id, ("cpu_load:1",))
        }
        assert all(
            r["detail"].startswith("unsupported: the node does not offer cpu_load")
            for r in skipped
        )
        assert all(r["job_id"] is None for r in skipped)
    finally:
        await b.stop()
        await a.stop()


async def test_every_condition_end_to_end_on_one_node(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=1)
    try:
        created = await _create(
            http,
            _spec(
                "[none, cpu_load:1, queue:2, cache:cold, cache:warm]", origin=a.node_id
            ),
        )
        campaign_id = created["campaign_id"]
        assert created["run_count"] == 15
        await _submit_all(http, campaign_id, 15)
        runs = await _wait_runs_ended(http, campaign_id)
        assert {r["state"] for r in runs} == {"done"}, [
            (r["conditions"], r["state"], r["detail"]) for r in runs
        ]

        job_ids = [r["job_id"] for r in runs]
        outcomes = await _outcomes(engine, job_ids)
        assert set(outcomes) == set(job_ids)
        for run in runs:
            outcome, workload, identity = outcomes[run["job_id"]]
            condition = run["conditions"][0]
            assert identity["condition"] == condition
            assert outcome.condition == condition
            assert outcome.measured["verified"] is True, (condition, outcome.measured)
            assert outcome.failed is False
            assert outcome.measured["session_label"] == "wifi"
            report = (workload or {}).get("condition")
            if condition in ("cpu_load:1", "cache:cold", "cache:warm"):
                assert report and report["verified"] and report["reverted"], report
            else:
                assert report is None  # controller-side kinds: the agent got none
            if condition == "cpu_load:1":
                assert report["measured"]["load_cores"] >= 0.8
            if condition == "cache:cold":
                assert report["measured"]["blobs_hit"] == 0
                assert report["measured"]["blobs_fetched"] >= 1
            if condition == "cache:warm":
                assert report["measured"]["blobs_fetched"] == 0
                assert identity["condition_facts"] == {"warmup_state": "succeeded"}
            if condition == "queue:2":
                assert identity["condition_facts"] == {"fillers": 2, "fillers_ahead": 2}

        # fillers + warm-ups exist, ran, and are not campaign records
        sessions = create_session_factory(engine)
        async with sessions() as db:
            extra = list(
                await db.scalars(
                    select(Job).where(Job.idempotency_key.like("bench:%:%"))
                )
            )
        kinds = sorted(j.idempotency_key.split(":")[2] for j in extra)
        assert kinds == ["filler"] * 6 + ["warmup"] * 3
        assert all(
            j.priority == -10 and j.placement["node_id"] == a.node_id for j in extra
        )
        records = await _wait_records(engine, campaign_id, 15)
        assert len(records) == 15
        assert {r["job_id"] for r in records} == set(job_ids)
        assert {r["failure_class"] for r in records} == {"ok"}
        # fix round 1: their own records are tagged (training filters drop them)
        extra_ids = {j.id: j.idempotency_key.split(":") for j in extra}
        async with sessions() as db:
            tags = await bench_identities(db, list(extra_ids))
        assert {k: v["bench_role"] for k, v in tags.items()} == {
            k: parts[2] for k, parts in extra_ids.items()
        }
        assert all(tags[k]["run_id"] == parts[1] for k, parts in extra_ids.items())
        deadline = time.monotonic() + 30
        while True:
            tagged = [
                r for r in await fetch_records(engine) if r["job_id"] in extra_ids
            ]
            if len(tagged) >= len(extra_ids) or time.monotonic() > deadline:
                break
            await asyncio.sleep(0.2)
        assert {r["job_id"] for r in tagged} == set(extra_ids)
        for row in tagged:
            parts = extra_ids[row["job_id"]]
            assert row["campaign_id"] is None and row["condition"] is None
            assert row["condition_measured"] == {
                "bench_role": parts[2],
                "run_id": parts[1],
            }
    finally:
        await a.stop()


async def test_forced_verify_failure_is_condition_failed_never_labelled(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(load_module, "CPU_VERIFY_FRACTION", 50.0)  # cannot be met
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        created = await _create(http, _spec("[cpu_load:1]", origin=a.node_id))
        campaign_id = created["campaign_id"]
        await _submit_all(http, campaign_id, 3)
        runs = await _wait_runs_ended(http, campaign_id)
        assert {r["state"] for r in runs} == {"failed"}
        for run in runs:
            job = await wait_job(http, run["job_id"], timeout_s=30)
            assert job["failure_type"] == "config_error"
        records = await _wait_records(engine, campaign_id, 3)
        assert len(records) == 3
        assert {r["failure_class"] for r in records} == {"condition_failed"}
        assert {r["failure_type"] for r in records} == {"config_error"}
        outcomes = await _outcomes(engine, [r["job_id"] for r in runs])
        for outcome, workload, _identity in outcomes.values():
            assert outcome.failed and outcome.measured["verified"] is False
            assert "busy cores" in outcome.measured["reason"]
            assert workload["condition"]["reverted"] is True
            assert "family" not in workload  # the workload never ran
    finally:
        await a.stop()


async def test_unavailable_pauses_the_origin_during_a_mode1_run(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    b = await _agent(grpc_target, tmp_path / "b")
    try:
        created = await _create(
            http, _spec("[unavailable:20s]", origin=a.node_id, strategies="[mode1]")
        )
        campaign_id = created["campaign_id"]
        await _submit_all(http, campaign_id, 1)
        assert runtime.node_pauses.paused(a.node_id)
        assert not runtime.node_pauses.paused(b.node_id)
        # a user job pinned to the paused origin waits (planned DRAINING)
        user = tmp_path / "user"
        user.mkdir()
        (user / "main.py").write_text("print('user')\n")
        # A pinned submit refuses a SUSPECT node; on a loaded runner a long
        # event-loop stall can leave A briefly suspect - wait until it is back.
        for _ in range(100):
            node = (await http.get(f"/v1/nodes/{a.node_id}")).json()
            if node["state"] == "online":
                break
            await asyncio.sleep(0.1)
        user_id = await submit(
            http, user, a.node_id, {"name": "user", "entrypoint": ["python", "main.py"]}
        )
        await asyncio.sleep(2.0)
        assert (await http.get(f"/v1/jobs/{user_id}")).json()["state"] == "queued"
        first = (await _runs(http, campaign_id))[0]
        done = await wait_job(http, first["job_id"], timeout_s=60)
        assert done["state"] == "succeeded", done
        outcomes = await _outcomes(engine, [first["job_id"]])
        outcome, _workload, identity = outcomes[first["job_id"]]
        assert identity["condition_facts"]["paused_node_id"] == a.node_id
        assert identity["condition_facts"]["dispatched_after_s"] < 20
        assert outcome.measured["verified"] is True
        # cancel the rest; the user job runs once the pause ends
        for run in await _runs(http, campaign_id):
            if run["job_id"] and run["job_id"] != first["job_id"]:
                await http.post(f"/v1/jobs/{run['job_id']}/cancel")
        await http.post(f"/v1/jobs/{user_id}/cancel")
    finally:
        await b.stop()
        await a.stop()
