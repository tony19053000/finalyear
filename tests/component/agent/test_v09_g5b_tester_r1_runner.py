"""V0.9 G5 lane B, tester round 1: break the runner on a real controller + PostgreSQL.

From the brief (0.9.7, D219/D246, checklist 30), not from the implementation:

* no double submission - crash between the job's commit and the link, a crash
  right after a settle, three syncs and two runners racing;
* retry rules through real ticks - an infra failure spends a try (bounded), a
  workload failure and a user cancel never retry, ``owner_preempted`` never
  spends a try and never fails the run however often it repeats;
* slots are shared by all campaigns, the submit cap holds per tick;
* controls - 404 / 409 / validation, concurrent identical controls, cancel
  racing a submission in flight (no orphan job may outlive the cancel);
* budget with a run in flight; a started-then-paused campaign refuses manual submit;
* owner guard - the pause switch pre-empts a running bench attempt and the run
  is requeued without a try; a user job arriving while the guard is on runs at
  once; a queued user job is dispatched before a queued bench job; an old agent
  (no G5 heartbeat fields) keeps working;
* the runner's supervision as /healthz sees it; progress events <= 1/s.
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
from sqlalchemy import text

from meshforge.agent.policy import policy_path, write_overlay
from meshforge.controller.bench.keys import run_key
from meshforge.controller.bench.runner import BenchRunner, RunnerSettings
from meshforge.controller.bench.store import BenchStoreError
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine, create_session_factory

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(400)]

POLICY = {"accept_jobs": True, "allow_unsandboxed": True}
USER_MANIFEST = {"name": "user-job", "entrypoint": ["python", "main.py"]}


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


def _spec(*, reps: int = 3, extra: str = "") -> str:
    return (
        "name: g5-tester\nseed: 5\n"
        "families:\n  - family: cpu.primes\n"
        "strategies: [local]\n"
        f"repetitions: {reps}\n"
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


async def _verb(
    http: httpx.AsyncClient, cid: str, verb: str, body: Any = None
) -> httpx.Response:
    return await http.post(f"/v1/bench/campaigns/{cid}/{verb}", json=body)


async def _ok(http: httpx.AsyncClient, cid: str, verb: str, body: Any = None) -> Any:
    resp = await _verb(http, cid, verb, body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _ended(runs: list[dict[str, Any]]) -> bool:
    return all(r["state"] in ("done", "failed", "skipped") for r in runs)


async def _drive(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    cid: str,
    *,
    until: Any,
    within: float = 240.0,
    each: Any = None,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + within
    while True:
        await runtime.bench_runner.tick()
        runs = await _runs(http, cid)
        if each is not None:
            await each(runs)
        if until(runs):
            return runs
        assert time.monotonic() < deadline, [(r["state"], r["detail"]) for r in runs]
        await asyncio.sleep(0.15)


async def _sql(engine: Any, query: str, **params: Any) -> list[tuple[Any, ...]]:
    sessions = create_session_factory(engine)
    async with sessions() as db:
        return [tuple(r) for r in (await db.execute(text(query), params)).all()]


async def _exec(engine: Any, query: str, **params: Any) -> None:
    sessions = create_session_factory(engine)
    async with sessions() as db, db.begin():
        await db.execute(text(query), params)


async def _bench_jobs(engine: Any, cid: str) -> list[tuple[Any, ...]]:
    """(key, state, failure_type, created_at) of every benchmark job of the campaign."""
    return await _sql(
        engine,
        "SELECT j.idempotency_key, j.state, j.failure_type, j.created_at "
        "FROM job j JOIN bench_run r ON split_part(substr(j.idempotency_key, 7), ':', 1) = r.id "
        "WHERE r.campaign_id = :c AND j.idempotency_key LIKE 'bench:%' "
        "ORDER BY j.created_at",
        c=cid,
    )


async def _kill(
    runtime: ControllerRuntime,
    http: httpx.AsyncClient,
    engine: Any,
    job_id: str,
    failure_type: str,
) -> None:
    """End a running benchmark job as failed with ``failure_type`` (cancel, then
    stamp - what ``settle`` reads)."""
    await runtime.dispatcher.cancel(job_id)
    await wait_job(http, job_id, timeout_s=60)
    await _exec(
        engine,
        "UPDATE job SET failure_type = :t WHERE id = :j",
        t=failure_type,
        j=job_id,
    )


def _user_project(tmp_path: Path, seconds: float, name: str = "user-proj") -> Path:
    project = tmp_path / name
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(
        f"import time\ntime.sleep({seconds})\nprint('ok')\n"
    )
    return project


async def _until(predicate: Any, within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while not await predicate():
        assert time.monotonic() < deadline
        await asyncio.sleep(0.1)


async def _first_submitted(
    runtime: ControllerRuntime, http: httpx.AsyncClient, cid: str
) -> dict[str, Any]:
    runs = await _drive(
        runtime,
        http,
        cid,
        until=lambda rs: any(r["state"] == "submitted" for r in rs),
    )
    return next(r for r in runs if r["state"] == "submitted")


# ------------------------------------------------------------ double submission


async def test_crash_between_job_commit_and_link_never_duplicates(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=2)
    try:
        cid = await _create(http, _spec(reps=4))
        await _ok(http, cid, "start", {"cooldown_s": 0, "max_concurrent_per_node": 2})
        real = runtime.bench.mark_submitted
        crashes = {"n": 0}

        async def flaky(*args: Any, **kwargs: Any) -> bool:
            if crashes["n"] < 2:  # the process "dies" right after the job's commit
                crashes["n"] += 1
                raise BenchStoreError("simulated crash after the job commit")
            return await real(*args, **kwargs)

        monkeypatch.setattr(runtime.bench, "mark_submitted", flaky)
        for _ in range(2):
            with pytest.raises(Exception):  # noqa: B017 - the tick surfaces it
                await runtime.bench_runner.tick()
        monkeypatch.setattr(runtime.bench, "mark_submitted", real)
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
        keys = [k for k, *_ in await _bench_jobs(engine, cid)]
        assert sorted(keys) == sorted(run_key(r["run_id"], 0) for r in runs)
        assert len(keys) == len(set(keys)) == len(runs)  # one job per run, ever
        # no run is linked to a job twice
        dup = await _sql(
            engine,
            "SELECT job_id FROM bench_run WHERE campaign_id=:c AND job_id IS NOT NULL "
            "GROUP BY job_id HAVING count(*) > 1",
            c=cid,
        )
        assert dup == []
    finally:
        await a.stop()


async def test_settle_is_exactly_once_under_concurrent_syncs_and_two_runners(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0, "max_infra_retries": 3})
        first = await _first_submitted(runtime, http, cid)
        victim, job = first["run_id"], first["job_id"]
        await _kill(runtime, http, engine, job, "node_lost")
        # three syncs at once: the retry is spent exactly once
        await asyncio.gather(*(runtime.bench.sync() for _ in range(3)))
        row = (
            await _sql(
                engine,
                "SELECT state, tries, generation, job_id FROM bench_run WHERE id=:r",
                r=victim,
            )
        )[0]
        assert row == ("planned", 1, 1, None), row
        # crash "after settle, before submit": nothing exists for generation 1 yet
        assert run_key(victim, 1) not in [k for k, *_ in await _bench_jobs(engine, cid)]
        # two runners (a restarted controller beside a slow one) submit it once
        other = BenchRunner(
            store=runtime.bench,
            control=runtime.bench_control,
            submitter=runtime.bench_submitter,
            settings=RunnerSettings(max_submit_per_tick=4),
            sessions=runtime.manager,
        )
        await asyncio.gather(runtime.bench_runner.tick(), other.tick())
        keys = [k for k, *_ in await _bench_jobs(engine, cid)]
        assert keys.count(run_key(victim, 1)) == 1
        assert len(keys) == len(set(keys))
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
        tries = await _sql(engine, "SELECT tries FROM bench_run WHERE id=:r", r=victim)
        assert tries == [(1,)]
    finally:
        await a.stop()


# ------------------------------------------------------------ retries


async def test_workload_failure_and_user_cancel_are_never_retried(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0, "max_infra_retries": 5})
        by_type = ["user_error", "cancelled", "oom"]
        victims: dict[str, str] = {}

        async def each(runs: list[dict[str, Any]]) -> None:
            for run in runs:
                if (
                    run["state"] == "submitted"
                    and run["job_id"]
                    and run["run_id"] not in victims
                    and len(victims) < len(by_type)
                ):
                    ftype = by_type[len(victims)]
                    victims[run["run_id"]] = ftype
                    await _kill(runtime, http, engine, run["job_id"], ftype)

        runs = await _drive(runtime, http, cid, until=_ended, each=each)
        assert len(victims) == 3
        for run in runs:
            assert run["state"] == "failed", run  # all three runs were killed
        rows = await _sql(
            engine,
            "SELECT id, tries, generation, last_failure FROM bench_run WHERE campaign_id=:c",
            c=cid,
        )
        for rid, tries, generation, last in rows:
            assert (tries, generation, last) == (0, 0, victims[rid])
        keys = [k for k, *_ in await _bench_jobs(engine, cid)]
        assert sorted(keys) == sorted(run_key(r["run_id"], 0) for r in runs)
    finally:
        await a.stop()


async def test_owner_preempted_repeats_never_spend_a_try_nor_fail_the_run(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0, "max_infra_retries": 1})
        first = await _first_submitted(runtime, http, cid)
        victim = first["run_id"]
        seen: list[str] = []

        async def each(runs: list[dict[str, Any]]) -> None:
            run = next(r for r in runs if r["run_id"] == victim)
            if (
                run["state"] == "submitted"
                and run["job_id"] not in seen
                and len(seen) < 4
            ):
                seen.append(run["job_id"])
                await _kill(runtime, http, engine, run["job_id"], "owner_preempted")

        await each(await _runs(http, cid))
        runs = await _drive(runtime, http, cid, until=_ended, each=each)
        assert len(seen) == 4  # four pre-emptions, max_infra_retries is 1
        assert {r["state"] for r in runs} == {"done"}
        row = (
            await _sql(
                engine,
                "SELECT tries, generation FROM bench_run WHERE id=:r",
                r=victim,
            )
        )[0]
        assert row == (0, 4), row
        status = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()
        assert status["retries"] == 0
    finally:
        await a.stop()


# ------------------------------------------------------------ slots + caps


async def test_two_campaigns_share_the_node_slot(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=4)
    try:
        c1 = await _create(http, _spec(reps=3))
        c2 = await _create(http, _spec(reps=3))
        for cid in (c1, c2):
            await _ok(
                http, cid, "start", {"cooldown_s": 0, "max_concurrent_per_node": 1}
            )
        peak = 0

        async def each(_runs: list[dict[str, Any]]) -> None:
            nonlocal peak
            live = await _sql(
                engine,
                "SELECT count(*) FROM job WHERE idempotency_key LIKE 'bench:%' "
                "AND state NOT IN ('succeeded','failed','cancelled') "
                "AND placement->>'node_id' = :n",
                n=a.node_id,
            )
            peak = max(peak, int(live[0][0]))

        deadline = time.monotonic() + 240
        while True:
            await runtime.bench_runner.tick()
            await each([])
            done = [_ended(await _runs(http, c)) for c in (c1, c2)]
            if all(done):
                break
            assert time.monotonic() < deadline
            await asyncio.sleep(0.1)
        assert peak == 1, f"bench jobs at once on one node: {peak}"
    finally:
        await a.stop()


async def test_submit_cap_per_tick_holds(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=4)
    b = await _agent(grpc_target, tmp_path / "b", max_cpu_jobs=4)
    try:
        cid = await _create(http, _spec(reps=4))
        await _ok(http, cid, "start", {"cooldown_s": 0, "max_concurrent_per_node": 4})
        capped = BenchRunner(
            store=runtime.bench,
            control=runtime.bench_control,
            submitter=runtime.bench_submitter,
            settings=RunnerSettings(max_submit_per_tick=2),
            sessions=runtime.manager,
        )
        report = await capped.tick()
        assert report.submitted == 2
        submitted = [r for r in await _runs(http, cid) if r["state"] == "submitted"]
        assert len(submitted) == 2
    finally:
        await a.stop()
        await b.stop()


# ------------------------------------------------------------ controls


async def test_controls_unknown_conflicting_and_invalid(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        ghost = "camp_01JZZZZZZZZZZZZZZZZZZZZZZZ"
        for verb in ("start", "pause", "resume", "cancel"):
            resp = await _verb(http, ghost, verb)
            assert resp.status_code == 404, (verb, resp.status_code, resp.text)
        assert (
            await http.get(f"/v1/bench/campaigns/{ghost}/status")
        ).status_code == 404
        for junk in ("nope", "camp_%27%3B--", "x" * 200):
            resp = await _verb(http, junk, "start")
            assert 400 <= resp.status_code < 500, (junk, resp.status_code)
        anon = httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
        )
        async with anon:
            resp = await anon.post(f"/v1/bench/campaigns/{ghost}/start")
            assert resp.status_code == 401

        cid = await _create(http, _spec(reps=3))
        bad_bodies: list[Any] = [
            {"max_concurrent_per_node": 0},
            {"max_concurrent_per_node": 65},
            {"max_concurrent_per_node": 1.5},
            {"cooldown_s": -1},
            {"cooldown_s": 86_401},
            {"cooldown_s": "NaN"},
            {"max_infra_retries": 11},
            {"max_infra_retries": -1},
            {"surprise": 1},
            [1, 2],
        ]
        for body in bad_bodies:
            resp = await _verb(http, cid, "start", body)
            assert 400 <= resp.status_code < 500, (body, resp.status_code, resp.text)
        # nothing of that changed the campaign: still un-started, defaults intact
        doc = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()
        assert doc["runner_owned"] is False and doc["max_concurrent_per_node"] == 1
        assert doc["cooldown_s"] == 5.0 and doc["max_infra_retries"] == 2
        assert (
            await http.post(
                f"/v1/bench/campaigns/{cid}/start",
                content=b"{not json",
                headers={"content-type": "application/json"},
            )
        ).status_code in (400, 422)

        # idempotent starts keep the first start time; throttle overrides apply
        first = await _ok(http, cid, "start", {"cooldown_s": 0.5})
        second = await _ok(http, cid, "start")
        assert first["started_at"] == second["started_at"]
        assert second["cooldown_s"] == 0.5
        assert (await _ok(http, cid, "resume"))["state"] == "active"  # not paused: noop
        # finished campaigns refuse everything but cancel's 409, deterministically
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
        await runtime.bench_runner.tick()
        for verb in ("start", "pause", "resume", "cancel"):
            resp = await _verb(http, cid, verb)
            assert resp.status_code == 409, (verb, resp.text)
    finally:
        await a.stop()


async def test_concurrent_identical_and_opposing_controls(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=6))
        await _ok(http, cid, "start", {"cooldown_s": 0})
        mixed = await asyncio.gather(
            *(_verb(http, cid, v) for v in ["pause", "resume"] * 8)
        )
        assert {r.status_code for r in mixed} == {200}, [r.text for r in mixed]
        state = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()["state"]
        assert state in ("active", "paused")
        # r2: the racing pause/resume may end paused (nothing would ever submit)
        await _ok(http, cid, "resume")
        await _first_submitted(runtime, http, cid)  # something is in flight
        racers = await asyncio.gather(*(_verb(http, cid, "cancel") for _ in range(6)))
        assert {r.status_code for r in racers} == {200}, [r.text for r in racers]
        states = {r.json()["state"] for r in racers}
        assert states == {"cancelled"}
        runs = await _runs(http, cid)
        assert {r["state"] for r in runs} == {"skipped"}

        # every benchmark job of the campaign ends cancelled / terminal; none survives
        async def quiet() -> bool:
            rows = await _bench_jobs(engine, cid)
            return all(r[1] in ("succeeded", "failed", "cancelled") for r in rows)

        await _until(quiet, within=60)
        for _ in range(5):
            report = await runtime.bench_runner.tick()
            assert report.submitted == 0 and report.campaigns == 0
        before = len(await _bench_jobs(engine, cid))
        await asyncio.sleep(0.5)
        assert len(await _bench_jobs(engine, cid)) == before
    finally:
        await a.stop()


async def test_cancel_racing_a_submission_in_flight_leaves_no_live_orphan_job(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Brief: cancel = cancel this campaign's non-terminal bench jobs, runs ->
    skipped. A tick that already read the campaign as active and is creating a
    job when the cancel lands must not leave a job that then RUNS."""
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0})
        jobs = runtime.bench_submitter._jobs
        real = jobs.create_queued_job
        entered, release = asyncio.Event(), asyncio.Event()

        async def gated(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            await release.wait()  # the cancel lands while the job is being created
            return await real(*args, **kwargs)

        monkeypatch.setattr(jobs, "create_queued_job", gated)
        tick = asyncio.create_task(runtime.bench_runner.tick())
        await asyncio.wait_for(entered.wait(), 30)
        cancelled = await _ok(http, cid, "cancel")
        assert cancelled["state"] == "cancelled"
        release.set()
        await tick
        monkeypatch.setattr(jobs, "create_queued_job", real)
        await asyncio.sleep(0.5)

        async def settled() -> bool:
            rows = await _bench_jobs(engine, cid)
            return all(r[1] in ("failed", "cancelled", "succeeded") for r in rows)

        # a job born from the racing submission must have been cancelled too
        try:
            await _until(settled, within=6)
        except AssertionError:
            rows = await _bench_jobs(engine, cid)
            pytest.fail(f"a cancelled campaign still has a live benchmark job: {rows}")
        rows = await _bench_jobs(engine, cid)
        assert all(r[1] == "cancelled" for r in rows), rows
    finally:
        await a.stop()


async def test_manual_submit_refused_once_started_even_when_paused(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        first = await http.post(
            f"/v1/bench/campaigns/{cid}/submit", params={"count": 1}
        )
        assert first.status_code == 200, first.text  # manual path works before start
        await _ok(http, cid, "start", {"cooldown_s": 0})
        await _ok(http, cid, "pause")
        for count in (1, 5):
            resp = await http.post(
                f"/v1/bench/campaigns/{cid}/submit", params={"count": count}
            )
            assert resp.status_code == 409, resp.text
        keys_before = len(await _bench_jobs(engine, cid))
        for _ in range(5):
            await runtime.bench_runner.tick()
        assert len(await _bench_jobs(engine, cid)) == keys_before  # paused = silent
        await _ok(http, cid, "resume")
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
        keys = [k for k, *_ in await _bench_jobs(engine, cid)]
        assert len(keys) == len(set(keys)) == len(runs)  # manual + runner: one each
    finally:
        await a.stop()


async def test_budget_stops_planned_but_lets_the_run_in_flight_finish(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=4, extra=", max_hours: 1"))
        await _ok(http, cid, "start", {"cooldown_s": 0})
        first = await _first_submitted(runtime, http, cid)
        await _exec(
            engine,
            "UPDATE bench_campaign SET started_at = now() - interval '61 minutes' "
            "WHERE id=:c",
            c=cid,
        )
        runs = await _drive(runtime, http, cid, until=_ended)
        by_id = {r["run_id"]: r for r in runs}
        assert by_id[first["run_id"]]["state"] == "done"  # in flight: finished
        others = [r for r in runs if r["run_id"] != first["run_id"]]
        assert others and {r["state"] for r in others} == {"skipped"}
        status = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()
        assert status["state"] == "done" and status["finished_reason"] == "budget"
        # nothing more is submitted afterwards
        count = len(await _bench_jobs(engine, cid))
        for _ in range(3):
            await runtime.bench_runner.tick()
        assert len(await _bench_jobs(engine, cid)) == count == 1
    finally:
        await a.stop()


# ------------------------------------------------------------ owner guard + user jobs


async def test_policy_pause_preempts_a_running_bench_attempt_and_requeues_free(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=2)
    try:
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0, "max_infra_retries": 0})
        first = await _first_submitted(runtime, http, cid)
        job = first["job_id"]
        await wait_job(http, job, states=frozenset({"running"}), timeout_s=60)
        write_overlay(policy_path(a.client.settings), {"pause_benchmarks": True})
        ended = await wait_job(http, job, timeout_s=60)
        assert ended["failure_type"] == "owner_preempted"
        live = runtime.manager.get(a.node_id)
        assert live is not None

        async def seen() -> bool:
            return live.policy is not None and live.policy.pause_benchmarks

        await _until(seen)
        # a user job arriving while the pause is on runs at once and is untouched
        uj = await submit(http, _user_project(tmp_path, 0.5), a.node_id, USER_MANIFEST)
        assert (await wait_job(http, uj, timeout_s=60))["state"] == "succeeded"
        for _ in range(8):
            report = await runtime.bench_runner.tick()
            assert report.submitted == 0
            await asyncio.sleep(0.1)
        row = (
            await _sql(
                engine,
                "SELECT state, tries, generation FROM bench_run WHERE id=:r",
                r=first["run_id"],
            )
        )[0]
        assert row == ("planned", 0, 1)  # requeued; max_infra_retries=0 did not fail it
        write_overlay(policy_path(a.client.settings), {"pause_benchmarks": False})

        async def resumed() -> bool:
            return live.policy is not None and not live.policy.pause_benchmarks

        await _until(resumed)
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
    finally:
        await a.stop()


async def test_a_queued_user_job_is_dispatched_before_a_queued_bench_job(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    """Checklist 30: a bench job never jumps a user job; one slot, a user job
    running, a bench job queued, a SECOND user job arrives later - it goes first."""
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=1)
    try:
        u1 = await submit(
            http, _user_project(tmp_path, 3.0, "u1"), a.node_id, USER_MANIFEST
        )
        await wait_job(http, u1, states=frozenset({"running"}), timeout_s=60)
        cid = await _create(http, _spec(reps=3))
        out = await http.post(f"/v1/bench/campaigns/{cid}/submit", params={"count": 1})
        assert out.status_code == 200, out.text  # manual path: queued behind u1
        bench_job = out.json()["outcomes"][0]["job_id"]
        u2 = await submit(
            http, _user_project(tmp_path, 0.3, "u2"), a.node_id, USER_MANIFEST
        )
        for jid in (u1, u2, bench_job):
            await wait_job(http, jid, timeout_s=120)
        rows = dict(
            await _sql(
                engine,
                "SELECT job_id, min(started_at) FROM attempt "
                "WHERE job_id = ANY(:j) GROUP BY job_id",
                j=[u2, bench_job],
            )
        )
        assert rows[u2] < rows[bench_job], (
            "a benchmark job started before a user job queued earlier than it ran"
        )
    finally:
        await a.stop()


async def test_user_job_is_never_preempted_by_a_bench_start_and_runs_beside_it(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=2)
    try:
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0})
        first = await _first_submitted(runtime, http, cid)
        await wait_job(
            http, first["job_id"], states=frozenset({"running"}), timeout_s=60
        )
        uj = await submit(http, _user_project(tmp_path, 1.0), a.node_id, USER_MANIFEST)
        assert (await wait_job(http, uj, timeout_s=60))["state"] == "succeeded"
        # the runner stays off the node while the user job exists
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
        user = (await http.get(f"/v1/jobs/{uj}")).json()
        assert user["failure_type"] in (None, "")
    finally:
        await a.stop()


async def test_an_agent_without_the_g5_heartbeat_fields_keeps_working(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        a.client.owner_heartbeat = None  # a pre-G5 agent sends no owner bit at all
        a.executor._owner_guard = None
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0})
        runs = await _drive(runtime, http, cid, until=_ended)
        assert {r["state"] for r in runs} == {"done"}
        live = runtime.manager.get(a.node_id)
        assert live is not None and live.owner_active is False
    finally:
        await a.stop()


# ------------------------------------------------------------ supervision + events


async def test_runner_crash_streak_degrades_healthz_and_recovers(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = runtime.bench_runner
    assert runner.degraded is False
    health = (await http.get("/healthz")).json()
    assert health["status"] == "ok"
    real_sync = runtime.bench.sync
    broken = {"on": True}

    async def sync(*args: Any, **kwargs: Any) -> int:
        if broken["on"]:
            raise RuntimeError("simulated database trouble")
        return await real_sync(*args, **kwargs)

    monkeypatch.setattr(runtime.bench, "sync", sync)

    async def fast_sleep(_s: float) -> None:
        await asyncio.sleep(0.01)

    monkeypatch.setattr(runner, "_sleep", fast_sleep)
    task = asyncio.create_task(runner.run())
    try:

        async def degraded() -> bool:
            return runner.degraded

        await _until(degraded, within=30)
        health = (await http.get("/healthz")).json()
        assert health["status"] == "degraded" and "bench-runner" in health["degraded"]
        assert not task.done()  # the controller's task did not die
        broken["on"] = False

        async def healthy() -> bool:
            return not runner.degraded

        await _until(healthy, within=30)
        assert (await http.get("/healthz")).json()["status"] == "ok"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_progress_events_are_rate_limited_on_a_real_runner(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0})
        events: list[tuple[float, dict[str, Any]]] = []
        runner = BenchRunner(
            store=runtime.bench,
            control=runtime.bench_control,
            submitter=runtime.bench_submitter,
            settings=RunnerSettings(),
            sessions=runtime.manager,
            publish=lambda d: events.append((time.monotonic(), dict(d))),
        )
        started = time.monotonic()
        for _ in range(60):  # as fast as the ticks go
            await runner.tick()
        elapsed = time.monotonic() - started
        mine = [e for t, e in events if e["campaign_id"] == cid]
        assert 1 <= len(mine) <= int(elapsed) + 2, (len(mine), elapsed)
        assert all(0.0 <= e["done_percent"] <= 100.0 for e in mine)
    finally:
        await a.stop()


@pytest.mark.parametrize("how", ["pause_switch", "owner_activity"])
async def test_a_bench_job_already_queued_is_held_not_failed_while_the_guard_is_on(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
    how: str,
) -> None:
    """The guard is a LIVE rejection: a benchmark job queued when the owner pauses
    (or comes back) waits - it is never failed ``no_feasible_plan`` - and starts
    when the guard clears. User jobs are not held."""
    a = await _agent(grpc_target, tmp_path / "a", max_cpu_jobs=1)
    try:
        u1 = await submit(
            http, _user_project(tmp_path, 2.0, "u1"), a.node_id, USER_MANIFEST
        )
        await wait_job(http, u1, states=frozenset({"running"}), timeout_s=60)
        cid = await _create(http, _spec(reps=3))
        out = await http.post(f"/v1/bench/campaigns/{cid}/submit", params={"count": 1})
        assert out.status_code == 200, out.text
        bench_job = out.json()["outcomes"][0]["job_id"]  # queued behind u1
        live = runtime.manager.get(a.node_id)
        assert live is not None
        if how == "pause_switch":
            write_overlay(policy_path(a.client.settings), {"pause_benchmarks": True})

            async def on() -> bool:
                return live.policy is not None and live.policy.pause_benchmarks

        else:
            await a.set_owner_active(True)

            async def on() -> bool:
                return bool(live.owner_active)

        await _until(on)
        assert (await wait_job(http, u1, timeout_s=60))["state"] == "succeeded"
        u2 = await submit(
            http, _user_project(tmp_path, 0.2, "u2"), a.node_id, USER_MANIFEST
        )
        assert (await wait_job(http, u2, timeout_s=60))["state"] == "succeeded"
        await asyncio.sleep(1.5)  # several dispatcher passes with the slot free
        held = (await http.get(f"/v1/jobs/{bench_job}")).json()
        assert held["state"] in ("queued", "planning", "submitted"), held
        assert held.get("failure_type") in (None, "")
        if how == "pause_switch":
            write_overlay(policy_path(a.client.settings), {"pause_benchmarks": False})
        else:
            await a.set_owner_active(False)

            async def off() -> bool:
                return not live.owner_active

            await _until(off)
        done = await wait_job(http, bench_job, timeout_s=90)
        assert done["state"] == "succeeded", done
    finally:
        await a.stop()
