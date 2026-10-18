"""V0.9 G3 lane B items 3-5 on a real controller, real agents and PostgreSQL:
a campaign is created and submitted through ``/v1/bench/campaigns``; every run
becomes an ordinary pinned job at the lowest priority, linked to its run, the
planner's would-have-chosen node is recorded, and every run record carries its
campaign identity (checklist 29). A user job queued after 20 benchmark jobs is
dispatched first (checklist 30). Runs that cannot run now are deferred, runs that
can never run as planned are skipped - never silently."""

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
from sqlalchemy import text

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.telemetry.export import fetch_records

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

POLICY = {"policy": {"accept_jobs": True, "allow_unsandboxed": True}}


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


async def _agent(grpc_target: str, path: Path, **policy: Any) -> ExecAgent:
    overrides = {"policy": {**POLICY["policy"], **policy}}
    return await ExecAgent.start(grpc_target, path, agent_overrides=overrides)


def _spec(
    origin: str,
    *,
    strategies: str = "[local, mode1]",
    reps: int = 3,
    family: str = "cpu.primes",
    extra: str = "",
) -> str:
    return (
        "name: forced-runs\n"
        "seed: 5\n"
        f"families:\n  - family: {family}\n{extra}"
        f"strategies: {strategies}\n"
        f"origin: {origin}\n"
        f"repetitions: {reps}\n"
        "budget: {max_runs: 100}\n"
    )


async def _create(http: httpx.AsyncClient, spec: str) -> dict[str, Any]:
    resp = await http.post("/v1/bench/campaigns", json={"spec": spec})
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


async def _runs(http: httpx.AsyncClient, campaign_id: str) -> list[dict[str, Any]]:
    resp = await http.get(
        f"/v1/bench/campaigns/{campaign_id}/runs", params={"limit": 1000}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json()["runs"])


async def _wait_records(
    engine: Any, campaign_id: str, want: int, within: float = 30.0
) -> list[Any]:
    deadline = time.monotonic() + within
    while True:
        rows = await fetch_records(engine, campaign_id=campaign_id)
        if len(rows) >= want or time.monotonic() > deadline:
            return rows
        await asyncio.sleep(0.2)


async def test_campaign_submits_pinned_lowest_priority_jobs_and_records_them(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    b = await _agent(grpc_target, tmp_path / "b")
    try:
        created = await _create(http, _spec(a.node_id))
        campaign_id = created["campaign_id"]
        assert created["run_count"] == 6 and created["counts"]["planned"] == 6
        assert created["summary"]["configs"] == 2
        assert set(created["summary"]["nodes"]) == {a.node_id, b.node_id}

        submitted = await http.post(
            f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 6}
        )
        assert submitted.status_code == 200, submitted.text
        body = submitted.json()
        assert body["submitted"] == 6, body
        assert body["counts"]["submitted"] == 6
        again = await http.post(
            f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 6}
        )
        assert again.json()["outcomes"] == []  # idempotent: nothing planned is left

        runs = await _runs(http, campaign_id)
        jobs = {}
        for run in runs:
            job = (await http.get(f"/v1/jobs/{run['job_id']}")).json()
            assert job["placement"]["node_id"] == run["node_id"]
            assert job["placement"]["mode"] == (
                "local" if run["strategy"] == "local" else "offload"
            )
            assert job["priority"] == -10
            assert job["submitter_node_id"] == a.node_id
            assert job["benchmark"] == {
                "campaign_id": campaign_id,
                "run_id": run["run_id"],
            }
            jobs[run["run_id"]] = run["job_id"]
        assert {r["node_id"] for r in runs if r["strategy"] == "local"} == {a.node_id}
        assert {r["node_id"] for r in runs if r["strategy"] == "mode1"} == {b.node_id}

        for job_id in jobs.values():
            done = await wait_job(http, job_id, timeout_s=120)
            assert done["state"] == "succeeded", done
        runs = await _runs(http, campaign_id)
        assert {r["state"] for r in runs} == {"done"}
        detail = (await http.get(f"/v1/bench/campaigns/{campaign_id}")).json()
        assert detail["state"] == "done" and detail["counts"]["done"] == 6
        assert "name: forced-runs" in detail["spec"]

        decisions = (await http.get(f"/v1/jobs/{runs[0]['job_id']}/decisions")).json()
        dispatched = [d for d in decisions["decisions"] if d["outcome"] == "dispatched"]
        assert dispatched and dispatched[-1]["shadow_chosen_node_id"] in {
            a.node_id,
            b.node_id,
        }

        records = await _wait_records(engine, campaign_id, 6)
        assert len(records) == 6
        by_run = {r["run_id"]: r for r in runs}
        for record in records:
            assert record["campaign_id"] == campaign_id  # checklist 29
            assert record["failure_class"] == "ok"
            assert record["placement_mode"] == "pinned"
            assert record["planner_node_id"] in {a.node_id, b.node_id}  # D215
            assert record["workload_family"] == "cpu.primes"
            match = [r for r in by_run.values() if r["job_id"] == record["job_id"]]
            assert len(match) == 1
            run = match[0]
            assert record["group_id"] == run["group_id"]
            assert record["config_id"] == run["config_id"]
            assert record["repetition"] == run["repetition"]
            assert record["strategy"] == run["strategy"]
            assert record["node_id"] == run["node_id"]
            assert record["workload_seed"] == run["seed"]
    finally:
        await b.stop()
        await a.stop()


async def test_user_job_after_twenty_bench_jobs_is_dispatched_first(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    """Checklist 30: benchmark jobs never jump a user's job."""
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=1)
    try:
        blocker = tmp_path / "blocker"
        blocker.mkdir()
        (blocker / "main.py").write_text("import time\ntime.sleep(3)\n")
        blocker_id = await submit(
            http,
            blocker,
            a.node_id,
            {"name": "blocker", "entrypoint": ["python", "main.py"]},
        )
        await wait_job(http, blocker_id, states=frozenset({"running"}), timeout_s=30)

        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=20))
        campaign_id = created["campaign_id"]
        body = (
            await http.post(
                f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 20}
            )
        ).json()
        assert body["submitted"] == 20, body

        user = tmp_path / "user"
        user.mkdir()
        (user / "main.py").write_text("print('user')\n")
        user_id = await submit(
            http, user, a.node_id, {"name": "user", "entrypoint": ["python", "main.py"]}
        )
        done = await wait_job(http, user_id, timeout_s=60)
        assert done["state"] == "succeeded", done

        bench_jobs = [r["job_id"] for r in await _runs(http, campaign_id)]
        async with engine.connect() as conn:
            user_at = (
                await conn.execute(
                    text("SELECT min(created_at) FROM attempt WHERE job_id = :j"),
                    {"j": user_id},
                )
            ).scalar_one()
            bench_first = (
                await conn.execute(
                    text("SELECT min(created_at) FROM attempt WHERE job_id = ANY(:j)"),
                    {"j": bench_jobs},
                )
            ).scalar_one()
        assert user_at is not None
        assert bench_first is None or user_at < bench_first, (user_at, bench_first)
        for job_id in bench_jobs:  # do not run 20 benchmarks here
            await http.post(f"/v1/jobs/{job_id}/cancel")
        for job_id in bench_jobs:
            await wait_job(http, job_id, timeout_s=60)
        runs = await _runs(http, campaign_id)
        assert {r["state"] for r in runs} <= {"failed", "done"}
        records = await _wait_records(engine, campaign_id, 1, within=10)
        assert all(r["campaign_id"] == campaign_id for r in records)
    finally:
        await a.stop()


async def test_offline_node_defers_and_impossible_runs_are_skipped(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    b = await _agent(grpc_target, tmp_path / "b")
    try:
        # a GPU family on CPU-only agents: the pinned check refuses it statically
        gpu = await _create(
            http, _spec(a.node_id, strategies="[local]", family="gpu.matmul")
        )
        body = (
            await http.post(
                f"/v1/bench/campaigns/{gpu['campaign_id']}/submit", params={"count": 3}
            )
        ).json()
        assert [o["outcome"] for o in body["outcomes"]] == ["skipped"] * 3, body
        assert all("cannot run it" in o["detail"] for o in body["outcomes"])
        gpu_detail = (
            await http.get(f"/v1/bench/campaigns/{gpu['campaign_id']}")
        ).json()
        assert gpu_detail["state"] == "done" and gpu_detail["counts"]["skipped"] == 3

        created = await _create(http, _spec(a.node_id, strategies="[mode1]"))
        campaign_id = created["campaign_id"]
        await b.stop()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if runtime.manager.get(b.node_id) is None:
                break
            await asyncio.sleep(0.1)
        body = (
            await http.post(
                f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 2}
            )
        ).json()
        # G4 item 0.5 (D239.1): an offline node defers node-wide - tried once,
        # its other runs passed over (not listed) for the rest of the call
        assert [o["outcome"] for o in body["outcomes"]] == ["deferred"], body
        assert body["counts"]["planned"] == 3  # still planned: tried again next time

        # the library refuses a run's params (e.g. a library upgrade): skipped, with why
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE bench_run SET params = '{\"limit\": 1}'::jsonb "
                    "WHERE campaign_id = :c AND seq = 1"
                ),
                {"c": campaign_id},
            )
        body = (
            await http.post(
                f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 1}
            )
        ).json()
        assert body["outcomes"][0]["outcome"] == "skipped"
        assert "library refuses" in body["outcomes"][0]["detail"]
    finally:
        await a.stop()


async def test_concurrent_submits_of_one_run_make_one_job(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        created = await _create(http, _spec(a.node_id, strategies="[local]"))
        submitter, store = runtime.bench_submitter, runtime.bench
        assert submitter is not None and store is not None
        campaign = await store.get_campaign(created["campaign_id"])
        assert campaign is not None
        run = (await store.runs(campaign.id, limit=1))[0]
        outcomes = await asyncio.gather(
            *(submitter.submit_run(campaign, run, owner_name=None) for _ in range(4))
        )
        job_ids = {o.job_id for o in outcomes}
        assert len(job_ids) == 1, outcomes
        assert sorted(o.outcome for o in outcomes).count("submitted") == 1
        jobs = (await http.get("/v1/jobs", params={"limit": 50})).json()["jobs"]
        assert len([j for j in jobs if j["benchmark"]]) == 1
        await http.post(f"/v1/jobs/{job_ids.pop()}/cancel")
    finally:
        await a.stop()


async def test_api_refusals(
    http: httpx.AsyncClient, runtime: ControllerRuntime
) -> None:
    bad = await http.post("/v1/bench/campaigns", json={"spec": "name: x\nseed: 1\n"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_input"
    assert any("families" in p for p in bad.json()["error"]["problems"])
    unknown = await http.post(
        "/v1/bench/campaigns",
        json={
            "spec": _spec("node_nope", strategies="[local]").replace(
                "cpu.primes", "cpu.nope"
            )
        },
    )
    assert unknown.status_code == 400
    assert "unknown workload family" in unknown.text
    assert (await http.get("/v1/bench/campaigns/camp_nope")).status_code == 400
    missing = await http.get("/v1/bench/campaigns/camp_01J0000000000000000000000A")
    assert missing.status_code == 404
    listed = await http.get("/v1/bench/campaigns")
    assert listed.status_code == 200 and listed.json()["count"] == 0
    too_many = await http.post(
        "/v1/bench/campaigns/camp_01J0000000000000000000000A/submit",
        params={"count": 501},
    )
    assert too_many.status_code == 422 or too_many.status_code == 400
