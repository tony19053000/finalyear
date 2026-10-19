"""V0.9 G5 lane B, tester r2 (delta): try to break the D250 D-1 fix (real controller + PostgreSQL).

* a cancel (and a pause) landing at EVERY await point of a submission leaves no
  live benchmark job of the cancelled campaign, and no duplicate / unlinked
  job in the paused one;
* the sync backstop never cancels a user job, a job of another campaign, or a
  job of a run that is linked and live; its re-ask is throttled to 30 s per
  job and a failing cancel never breaks ``sync``;
* the strict ``start`` body.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
import types
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_jobs_harness import token_of
from sqlalchemy import text

from meshforge.controller.bench import store as store_module
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine, create_session_factory

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(400)]

POLICY = {"accept_jobs": True, "allow_unsandboxed": True}
TERMINAL = ("succeeded", "failed", "cancelled")


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        telemetry={"record_interval_s": 0.2},
        bench={"runner": False, "tick_s": 0.2},
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


async def _agent(grpc_target: str, path: Path) -> ExecAgent:
    return await ExecAgent.start(
        grpc_target, path, agent_overrides={"policy": dict(POLICY)}
    )


def _spec(*, reps: int = 3, conditions: str = "[none]") -> str:
    return (
        "name: g5-tester-r2\nseed: 5\n"
        "families:\n  - family: cpu.primes\n"
        f"conditions: {conditions}\n"
        "strategies: [local]\n"
        f"repetitions: {reps}\n"
        "budget: {max_runs: 100}\n"
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


async def _sql(engine: Any, query: str, **params: Any) -> list[tuple[Any, ...]]:
    sessions = create_session_factory(engine)
    async with sessions() as db:
        return [tuple(r) for r in (await db.execute(text(query), params)).all()]


async def _bench_jobs(engine: Any, cid: str) -> list[tuple[Any, ...]]:
    return await _sql(
        engine,
        "SELECT j.idempotency_key, j.state, r.id, r.state, r.job_id, j.id "
        "FROM job j JOIN bench_run r ON "
        "split_part(substr(j.idempotency_key, 7), ':', 1) = r.id "
        "WHERE r.campaign_id = :c AND j.idempotency_key LIKE 'bench:%' "
        "ORDER BY j.created_at",
        c=cid,
    )


async def _until(predicate: Any, within: float = 30.0) -> None:
    deadline = time.monotonic() + within
    while not await predicate():
        assert time.monotonic() < deadline, "condition not reached"
        await asyncio.sleep(0.1)


def _hook(owner: Any, name: str, when: str, fire: Any, *, nth: int = 1) -> None:
    """Wrap ``owner.name``: ``fire()`` runs once, before / after its ``nth`` call."""
    real = getattr(owner, name)
    count = 0

    async def wrapper(*args: Any, **kwargs: Any) -> Any:
        nonlocal count
        count += 1
        hit = count == nth
        if hit and when == "before":
            await fire()
        result = await real(*args, **kwargs)
        if hit and when == "after":
            await fire()
        return result

    setattr(owner, name, wrapper)


# (object, method, before/after, nth call)
POINTS = [
    ("registry", "get_node", "before", 1),
    ("jobs", "create_queued_job", "before", 1),
    ("jobs", "create_queued_job", "after", 1),
    ("store", "mark_submitted", "before", 1),
    ("store", "mark_submitted", "after", 1),
]


def _owner(runtime: ControllerRuntime, which: str) -> Any:
    sub = runtime.bench_submitter
    return {"registry": sub._registry, "jobs": sub._jobs, "store": sub._store}[which]


@pytest.mark.parametrize(("which", "name", "when", "nth"), POINTS)
async def test_cancel_at_every_await_point_of_a_submission_leaves_no_live_job(
    which: str,
    name: str,
    when: str,
    nth: int,
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

        async def cancel() -> None:
            assert (await _ok(http, cid, "cancel"))["state"] == "cancelled"

        _hook(_owner(runtime, which), name, when, cancel, nth=nth)
        await runtime.bench_runner.tick()  # the cancel lands inside this submission
        for _ in range(3):  # later ticks: nothing may start again
            await runtime.bench_runner.tick()

        async def settled() -> bool:
            await _runs(http, cid)  # a read = a sync = the backstop
            return all(r[1] in TERMINAL for r in await _bench_jobs(engine, cid))

        await _until(settled, within=40)
        rows = await _bench_jobs(engine, cid)
        # nothing succeeded after the cancel: every job born is cancelled
        assert all(r[1] == "cancelled" for r in rows), rows
        runs = await _runs(http, cid)
        assert {r["state"] for r in runs} <= {"skipped", "done", "failed"}, runs
        camp = (await http.get(f"/v1/bench/campaigns/{cid}")).json()
        assert camp["state"] == "cancelled"
    finally:
        await a.stop()


async def test_cancel_between_the_second_filler_and_the_run_job(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    """queue:2 creates two fillers and then the run job: the cancel lands after
    the 2nd create. Fillers and run job all end cancelled."""
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3, conditions="[queue:2]"))
        await _ok(http, cid, "start", {"cooldown_s": 0})

        async def cancel() -> None:
            await _ok(http, cid, "cancel")

        _hook(_owner(runtime, "jobs"), "create_queued_job", "after", cancel, nth=2)
        await runtime.bench_runner.tick()

        async def settled() -> bool:
            await _runs(http, cid)
            rows = await _bench_jobs(engine, cid)
            return bool(rows) and all(r[1] in TERMINAL for r in rows)

        await _until(settled, within=40)
        rows = await _bench_jobs(engine, cid)
        assert all(r[1] == "cancelled" for r in rows), rows
    finally:
        await a.stop()


@pytest.mark.parametrize(("which", "name", "when", "nth"), POINTS)
async def test_pause_at_every_await_point_keeps_one_linked_job_per_run(
    which: str,
    name: str,
    when: str,
    nth: int,
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

        async def pause() -> None:
            await _ok(http, cid, "pause")

        _hook(_owner(runtime, which), name, when, pause, nth=nth)
        await runtime.bench_runner.tick()
        await runtime.bench_runner.tick()
        await _runs(http, cid)
        rows = await _bench_jobs(engine, cid)
        by_run: dict[str, list[tuple[Any, ...]]] = {}
        for r in rows:
            by_run.setdefault(r[2], []).append(r)
        # a pause does not cancel: every job is the linked job of its run,
        # exactly one per run, nothing cancelled as an "orphan"
        for run_id, jobs in by_run.items():
            assert len(jobs) == 1, (run_id, jobs)
            assert jobs[0][4] == jobs[0][5], f"unlinked job of {run_id}: {jobs}"
            assert jobs[0][1] != "cancelled", jobs
        await _ok(http, cid, "resume")
        await _ok(http, cid, "cancel")
    finally:
        await a.stop()


async def test_backstop_spares_user_jobs_other_campaigns_and_linked_live_runs(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        keep = await _create(http, _spec(reps=3))  # stays active
        kill = await _create(http, _spec(reps=3))
        await _ok(http, keep, "start", {"cooldown_s": 0})
        await _ok(http, kill, "start", {"cooldown_s": 0})
        # park a submitted job per campaign: stop the agent running them
        asked: list[str] = []
        real_cancel = runtime.bench.cancel_job
        assert real_cancel is not None

        async def spy(job_id: str) -> object:
            asked.append(job_id)
            return await real_cancel(job_id)

        runtime.bench.cancel_job = spy
        for _ in range(4):
            await runtime.bench_runner.tick()
        keep_before = {r[5] for r in await _bench_jobs(engine, keep)}
        assert keep_before
        # a user job (plain, and one that merely uses a bench-looking key)
        for key in (None, "bench-notes", "benchmark:1"):
            headers = {} if key is None else {"Idempotency-Key": key}
            resp = await http.post(
                "/v1/jobs",
                headers=headers,
                json={
                    "manifest": {
                        "name": "user-job",
                        "entrypoint": ["python", "-c", "import time;time.sleep(30)"],
                    },
                    "placement": {"mode": "local"},
                },
            )
            assert resp.status_code in (200, 201, 202, 400, 422), resp.text
        await _ok(http, kill, "cancel")
        for _ in range(3):
            await runtime.bench.sync()
        killed = {r[5] for r in await _bench_jobs(engine, kill)}
        assert set(asked) <= killed, "the backstop asked for a job outside the campaign"
        # the other, active campaign and its linked jobs are untouched by the backstop
        assert not (set(asked) & keep_before)
        for row in await _bench_jobs(engine, keep):
            assert row[1] != "cancelled", row
        users = await _sql(
            engine,
            "SELECT state FROM job WHERE idempotency_key IS NULL "
            "OR idempotency_key NOT LIKE 'bench:%'",
        )
        assert all(s[0] != "cancelled" for s in users), users
        await _ok(http, keep, "cancel")
    finally:
        await a.stop()


async def test_backstop_reask_is_throttled_and_a_failing_cancel_never_breaks_sync(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        await _ok(http, cid, "start", {"cooldown_s": 0})
        calls: list[str] = []

        async def failing(job_id: str) -> object:
            calls.append(job_id)
            raise RuntimeError("cancel is broken")

        async def lost(_job_id: str) -> None:
            return None

        async def cancel() -> None:
            await _ok(http, cid, "cancel")

        # the cancel lands mid-submission and the immediate cancel is lost:
        # a live orphan only the backstop can end; the backstop's cancel fails
        runtime.bench_submitter.cancel_job = lost
        runtime.bench.cancel_job = None
        _hook(_owner(runtime, "jobs"), "create_queued_job", "before", cancel)
        await runtime.bench_runner.tick()
        await a.stop()  # nothing may run the orphan before the backstop is probed
        assert await runtime.bench.orphan_jobs(cid), await _bench_jobs(engine, cid)
        runtime.bench.cancel_job = failing
        calls.clear()
        for _ in range(5):
            assert await runtime.bench.sync() >= 0  # never raises
        per_job = {j: calls.count(j) for j in set(calls)}
        assert per_job, (
            await runtime.bench.orphan_jobs(cid),
            runtime.bench._orphans_asked,
            await _bench_jobs(engine, cid),
        )
        assert all(n == 1 for n in per_job.values()), per_job  # throttled
        now = [time.monotonic()]
        fake = types.SimpleNamespace(monotonic=lambda: now[0])
        monkeypatch.setattr(store_module, "time", fake)
        # entries stamped with the real clock are < 30 s old only relative to it;
        # move the fake clock far ahead of every stamp
        now[0] = time.monotonic() + 31.0
        calls.clear()
        await runtime.bench.sync()
        assert set(calls) == set(per_job), "not re-asked after 30 s"
        await runtime.bench.sync()
        assert len(calls) == len(set(calls)), "re-asked inside the 30 s window"
    finally:
        with contextlib.suppress(Exception):
            await a.stop()


STRICT_BAD = [
    {"max_concurrent_per_node": "2"},
    {"max_concurrent_per_node": True},
    {"max_concurrent_per_node": 2.0},
    {"max_concurrent_per_node": 0},
    {"max_concurrent_per_node": 65},
    {"cooldown_s": "1"},
    {"cooldown_s": True},
    {"cooldown_s": -0.1},
    {"cooldown_s": float("inf")},
    {"max_infra_retries": "1"},
    {"max_infra_retries": False},
    {"max_infra_retries": 1.0},
    {"max_infra_retries": 11},
    {"max_infra_retries": None, "bogus": 1},
]


@pytest.mark.parametrize("body", STRICT_BAD)
async def test_start_body_is_strict(
    body: dict[str, Any],
    http: httpx.AsyncClient,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        resp = await http.post(
            f"/v1/bench/campaigns/{cid}/start",
            content=json.dumps(body),
            headers={"content-type": "application/json"},
        )
        assert resp.status_code in (400, 422), (body, resp.status_code, resp.text)
        assert resp.json()["error"]["code"] == "invalid_input"
        started = await _sql(
            engine, "SELECT started_at FROM bench_campaign WHERE id = :c", c=cid
        )
        assert started == [(None,)], "a refused start must not hand the campaign over"
    finally:
        await a.stop()


@pytest.mark.parametrize(
    "body",
    [
        None,
        {},
        {"max_concurrent_per_node": 2, "cooldown_s": 0, "max_infra_retries": 0},
        {"cooldown_s": 1.5},
        {"cooldown_s": 3},
        {"max_concurrent_per_node": None, "cooldown_s": None},
    ],
)
async def test_start_body_accepts_the_cli_and_ui_shapes(
    body: Any, http: httpx.AsyncClient, grpc_target: str, tmp_path: Path
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = await _create(http, _spec(reps=3))
        resp = await _verb(http, cid, "start", body)
        assert resp.status_code == 200, (body, resp.text)
        await _ok(http, cid, "cancel")
    finally:
        await a.stop()
