"""V0.9 G5 lane B (0.9.7, D219/D246): the campaign runner on a real controller,
real (in-process) agents and PostgreSQL.

Checked:

* ``start`` hands the campaign to the runner: throttled (``max_concurrent_per_node``,
  ``cooldown_s``), manual ``submit`` refused ``409 campaign_running``, the
  campaign completes ``done``/``complete``, ``status`` adds up;
* settle rules through real ticks: an infra failure is requeued with
  ``tries + 1`` under a new generation key up to ``max_infra_retries``, then
  ``failed``; the old generation's job keeps its campaign identity;
  a second runner (a "restarted controller") submits nothing twice;
* ``pause`` / ``resume`` / ``cancel`` (benchmark jobs cancelled, runs skipped),
  budget hours stop submissions (``done``/``budget``);
* the owner guard: owner activity pre-empts the running benchmark attempt
  (``owner_preempted``, requeued with ``tries`` unchanged, excluded from the
  infra rate), dispatches nothing new while active, and a **user job** on the
  same node runs to success untouched; the ``pause_benchmarks`` switch holds
  every benchmark job while a user job runs;
* a node with a user job pinned to it gets no benchmark submission (checklist 30).
"""

from __future__ import annotations

import asyncio
import itertools
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
from sqlalchemy import select, text

from meshforge.agent.policy import policy_path, write_overlay
from meshforge.controller.bench.keys import run_key
from meshforge.controller.bench.runner import BenchRunner, RunnerSettings
from meshforge.controller.bench.store import campaign_identities
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.models.job import Job

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(400)]

POLICY = {"accept_jobs": True, "allow_unsandboxed": True}


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        telemetry={"record_interval_s": 0.2},
        bench={"runner": False, "tick_s": 0.2},  # ticks driven by the tests
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
    return await ExecAgent.start(
        grpc_target, path, agent_overrides={"policy": {**POLICY, **policy}}
    )


def _spec(*, reps: int = 3, extra: str = "", origin: str | None = None) -> str:
    return (
        "name: g5-runner\n"
        "seed: 5\n"
        "families:\n  - family: cpu.primes\n"
        "strategies: [local]\n"
        + (f"origin: {origin}\n" if origin else "")
        + f"repetitions: {reps}\n"
        "budget: {max_runs: 100" + extra + "}\n"
    )


async def _create(http: httpx.AsyncClient, spec: str) -> str:
    resp = await http.post("/v1/bench/campaigns", json={"spec": spec})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["campaign_id"])


async def _runs(http: httpx.AsyncClient, cid: str) -> list[dict[str, Any]]:
    resp = await http.get(f"/v1/bench/campaigns/{cid}/runs", params={"limit": 1000})
    assert resp.status_code == 200, resp.text
    return list(resp.json()["runs"])


async def _control(
    http: httpx.AsyncClient, cid: str, verb: str, body: Any = None
) -> dict[str, Any]:
    resp = await http.post(f"/v1/bench/campaigns/{cid}/{verb}", json=body)
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


async def _drive(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    cid: str,
    *,
    until: Any,
    within: float = 240.0,
    tick_s: float = 0.2,
    watch: Any = None,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + within
    while True:
        await runtime.bench_runner.tick()
        runs = await _runs(http, cid)
        if watch is not None:
            watch(runs)
        if until(runs):
            return runs
        assert time.monotonic() < deadline, [(r["state"], r["detail"]) for r in runs]
        await asyncio.sleep(tick_s)


def _ended(runs: list[dict[str, Any]]) -> bool:
    return all(r["state"] in ("done", "failed", "skipped") for r in runs)


async def _job_keys(engine: Any, prefix: str) -> list[str]:
    sessions = create_session_factory(engine)
    async with sessions() as db:
        return [
            str(k)
            for k in await db.scalars(
                select(Job.idempotency_key).where(
                    Job.idempotency_key.like(f"{prefix}%")
                )
            )
        ]


# ------------------------------------------------------------ throttle + complete


async def test_runner_runs_a_started_campaign_throttled_and_refuses_manual_submit(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=2)
    b = await _agent(grpc_target, tmp_path / "b", max_cpu_jobs=2)
    try:
        cid = await _create(http, _spec(reps=3))  # 2 nodes x 3 reps = 6 runs
        # manual first: G3's submit still works on an un-started campaign
        started = await _control(
            http, cid, "start", {"max_concurrent_per_node": 1, "cooldown_s": 1.0}
        )
        assert started["runner_owned"] and started["state"] == "active"
        refused = await http.post(
            f"/v1/bench/campaigns/{cid}/submit", params={"count": 1}
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["reason"] == "campaign_running"
        peak: dict[str, int] = {}

        def watch(runs: list[dict[str, Any]]) -> None:
            for node in (a.node_id, b.node_id):
                live = sum(
                    1
                    for r in runs
                    if r["node_id"] == node and r["state"] == "submitted"
                )
                peak[node] = max(peak.get(node, 0), live)

        runs = await _drive(runtime, http, cid, until=_ended, watch=watch)
        assert {r["state"] for r in runs} == {"done"}
        assert peak == {a.node_id: 1, b.node_id: 1}  # never 2 at once per node
        for node in (a.node_id, b.node_id):
            mine = sorted(
                (r for r in runs if r["node_id"] == node),
                key=lambda r: r["submitted_at"],
            )
            for before, after in itertools.pairwise(mine):
                gap = _seconds(after["submitted_at"]) - _seconds(before["ended_at"])
                assert gap >= 0.9, (before, after)  # cool-down 1 s (DB clock)
        await runtime.bench_runner.tick()
        status = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()
        assert status["state"] == "done" and status["finished_reason"] == "complete"
        assert status["done_percent"] == 100.0 and status["counts"]["done"] == 6
        assert status["preempted"] == 0
        again = await http.post(f"/v1/bench/campaigns/{cid}/start")
        assert again.status_code == 409  # a done campaign cannot start
    finally:
        await a.stop()
        await b.stop()


def _seconds(value: str) -> float:
    import datetime as dt

    return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


# ------------------------------------------------------------ settle + restart


async def test_infra_failures_retry_under_new_generations_then_fail(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        await _control(http, cid, "start", {"cooldown_s": 0, "max_infra_retries": 2})
        # one run's job "fails on infrastructure" every time: cancel it and
        # stamp node_lost (what the settle rule reads)
        first = (await _runs(http, cid))[0]
        victim = first["run_id"]
        seen_jobs: list[str] = []
        sessions = create_session_factory(engine)

        async def fail_victim(runs: list[dict[str, Any]]) -> None:
            run = next(r for r in runs if r["run_id"] == victim)
            job = run["job_id"]
            if run["state"] == "submitted" and job and job not in seen_jobs:
                seen_jobs.append(job)
                await runtime.dispatcher.cancel(job)
                # stamp the type once the cancel ended (it writes its own type)
                await wait_job(http, job, timeout_s=60)
                async with sessions() as db, db.begin():
                    await db.execute(
                        text("UPDATE job SET failure_type = 'node_lost' WHERE id = :j"),
                        {"j": job},
                    )

        deadline = time.monotonic() + 240
        while True:
            await runtime.bench_runner.tick()
            runs = await _runs(http, cid)
            await fail_victim(runs)
            if _ended(runs):
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.2)
        run = next(r for r in runs if r["run_id"] == victim)
        assert run["state"] == "failed", run
        assert len(seen_jobs) == 3  # the first try + 2 infra retries
        keys = sorted(await _job_keys(engine, f"bench:{victim}"))
        assert keys == sorted(
            run_key(victim, g) for g in range(3)
        )  # one per generation
        async with sessions() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT tries, generation, last_failure FROM bench_run WHERE id = :r"
                    ),
                    {"r": victim},
                )
            ).one()
            identities = await campaign_identities(db, seen_jobs)
        assert tuple(row) == (2, 2, "node_lost")
        # every generation's job keeps its campaign identity (checklist 29)
        assert {i["run_id"] for i in identities.values()} == {victim}
        assert set(identities) == set(seen_jobs)
        others = [r for r in runs if r["run_id"] != victim]
        assert {r["state"] for r in others} == {"done"}
    finally:
        await a.stop()


async def test_a_second_runner_after_a_restart_submits_nothing_twice(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=2)
    try:
        cid = await _create(http, _spec(reps=4))
        await _control(
            http, cid, "start", {"cooldown_s": 0, "max_concurrent_per_node": 2}
        )
        await runtime.bench_runner.tick()
        # the "restarted controller": a fresh runner on the same rows, ticking
        # concurrently with the old one (worst case of a slow shutdown)
        other = BenchRunner(
            store=runtime.bench,
            control=runtime.bench_control,
            submitter=runtime.bench_submitter,
            settings=RunnerSettings(max_submit_per_tick=4),
            sessions=runtime.manager,
        )
        deadline = time.monotonic() + 240
        while True:
            await asyncio.gather(runtime.bench_runner.tick(), other.tick())
            runs = await _runs(http, cid)
            if _ended(runs):
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.2)
        assert {r["state"] for r in runs} == {"done"}
        keys = await _job_keys(engine, "bench:")
        mine = [k for k in keys if k.split(":")[1] in {r["run_id"] for r in runs}]
        assert len(mine) == len(set(mine)) == len(runs)  # one job per run
    finally:
        await a.stop()


# ------------------------------------------------------------ controls


async def test_pause_resume_cancel_and_budget(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        await _control(http, cid, "start", {"cooldown_s": 0})
        paused = await _control(http, cid, "pause")
        assert paused["state"] == "paused"
        assert (await _control(http, cid, "pause"))["state"] == "paused"  # idempotent
        for _ in range(5):
            await runtime.bench_runner.tick()
        assert {r["state"] for r in await _runs(http, cid)} == {"planned"}
        assert (await _control(http, cid, "resume"))["state"] == "active"
        await _drive(
            runtime,
            http,
            cid,
            until=lambda runs: any(r["state"] == "submitted" for r in runs),
        )
        cancelled = await _control(http, cid, "cancel")
        assert cancelled["state"] == "cancelled"
        assert cancelled["finished_reason"] == "cancelled"
        assert cancelled["cancelled_jobs"] >= 1
        runs = await _runs(http, cid)
        assert {r["state"] for r in runs} == {"skipped"}
        assert {r["detail"] for r in runs} == {"cancelled"}
        for run in runs:
            if run["job_id"]:
                job = await wait_job(http, run["job_id"], timeout_s=60)
                assert job["state"] == "cancelled"
        assert (await _control(http, cid, "cancel"))["state"] == "cancelled"
        bad = await http.post(f"/v1/bench/campaigns/{cid}/resume")
        assert bad.status_code == 409

        # budget: max_hours passed -> nothing more submitted, done/budget
        cid2 = await _create(http, _spec(reps=3, extra=", max_hours: 1"))
        await _control(http, cid2, "start", {"cooldown_s": 0})
        # an hour and a bit of database time has passed since the start
        sessions = create_session_factory(engine)
        async with sessions() as db, db.begin():
            await db.execute(
                text(
                    "UPDATE bench_campaign SET started_at = now() - interval '61 minutes' "
                    "WHERE id = :c"
                ),
                {"c": cid2},
            )
        runs = await _drive(runtime, http, cid2, until=_ended)
        assert {r["state"] for r in runs} == {"skipped"}
        assert all(r["detail"].startswith("budget") for r in runs)
        status = (await http.get(f"/v1/bench/campaigns/{cid2}/status")).json()
        assert status["state"] == "done" and status["finished_reason"] == "budget"
    finally:
        await a.stop()


# ------------------------------------------------------------ owner guard


def _user_project(tmp_path: Path, seconds: float) -> Path:
    project = tmp_path / "user-proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(
        f"import time\ntime.sleep({seconds})\nprint('ok')\n"
    )
    return project


USER_MANIFEST = {"name": "user-job", "entrypoint": ["python", "main.py"]}


async def _wait(predicate: Any, within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while not await predicate():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.1)


@pytest.mark.security
async def test_owner_activity_preempts_requeues_and_never_touches_a_user_job(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=2)
    try:
        cid = await _create(http, _spec(reps=3))
        await _control(http, cid, "start", {"cooldown_s": 0})
        runs = await _drive(
            runtime,
            http,
            cid,
            until=lambda rs: any(r["state"] == "submitted" for r in rs),
        )
        bench_job = next(r["job_id"] for r in runs if r["state"] == "submitted")
        await wait_job(http, bench_job, states=frozenset({"running"}), timeout_s=60)
        user_job = await submit(
            http, _user_project(tmp_path, 3.0), a.node_id, USER_MANIFEST
        )
        await wait_job(http, user_job, states=frozenset({"running"}), timeout_s=60)

        assert await a.set_owner_active(True) == 1  # the bench attempt only
        stopped = await wait_job(http, bench_job, timeout_s=60)
        assert stopped["state"] == "failed"
        assert stopped["failure_type"] == "owner_preempted"

        live = runtime.manager.get(a.node_id)
        assert live is not None

        async def seen() -> bool:
            return bool(live.owner_active)

        await _wait(seen)
        user = await wait_job(http, user_job, timeout_s=60)
        assert user["state"] == "succeeded"  # never touched (checklist 30)
        for _ in range(10):  # 2 s of ticks while the owner is active (no user job)
            report = await runtime.bench_runner.tick()
            assert report.submitted == 0
            await asyncio.sleep(0.2)
        runs = await _runs(http, cid)
        assert not [r for r in runs if r["state"] == "submitted"]
        assert [r for r in runs if r["job_id"] is None and r["state"] == "planned"]

        await a.set_owner_active(False)

        async def clear() -> bool:
            return not live.owner_active

        await _wait(clear)
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
        status = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()
        assert status["retries"] == 0  # tries unchanged by a pre-emption

        async def recorded() -> bool:
            doc = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()
            return int(doc["records"]) >= 4

        await _wait(recorded, within=60)
        status = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()
        assert status["preempted"] == 1 and status["infra_failures"] == 0
        assert status["infra_failure_rate"] == 0.0  # excluded, not a failure
    finally:
        await a.stop()


async def test_pause_switch_holds_bench_jobs_while_a_user_job_runs(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=2)
    try:
        write_overlay(policy_path(a.client.settings), {"pause_benchmarks": True})
        live = runtime.manager.get(a.node_id)
        assert live is not None

        async def paused() -> bool:
            return live.policy is not None and live.policy.pause_benchmarks

        await _wait(paused)
        cid = await _create(http, _spec(reps=3))
        await _control(http, cid, "start", {"cooldown_s": 0})
        for _ in range(10):
            await runtime.bench_runner.tick()
            await asyncio.sleep(0.2)
        assert {r["state"] for r in await _runs(http, cid)} == {"planned"}
        user_job = await submit(
            http, _user_project(tmp_path, 0.5), a.node_id, USER_MANIFEST
        )
        assert (await wait_job(http, user_job, timeout_s=60))["state"] == "succeeded"
        write_overlay(policy_path(a.client.settings), {"pause_benchmarks": False})

        async def resumed() -> bool:
            return live.policy is not None and not live.policy.pause_benchmarks

        await _wait(resumed)
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
    finally:
        await a.stop()


@pytest.mark.security
async def test_no_bench_submission_to_a_node_with_a_user_job(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=2)
    try:
        cid = await _create(http, _spec(reps=3))
        user_job = await submit(
            http, _user_project(tmp_path, 4.0), a.node_id, USER_MANIFEST
        )
        await wait_job(http, user_job, states=frozenset({"running"}), timeout_s=60)
        await _control(http, cid, "start", {"cooldown_s": 0})
        report = await runtime.bench_runner.tick()
        assert report.excluded.get(a.node_id) == "user_job" and report.submitted == 0
        assert (await wait_job(http, user_job, timeout_s=60))["state"] == "succeeded"
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
    finally:
        await a.stop()
