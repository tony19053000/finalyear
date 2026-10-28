"""Tester (CI fix round 4): the orphan-sweep / submit link race on a real
controller + PostgreSQL, at many random interleavings. Invariants: exactly one
``submitted`` outcome per run, one job per run, no legit job ever cancelled by
the orphan path, a job whose run moved on IS cancelled."""

from __future__ import annotations

import asyncio
import random
from pathlib import Path
from typing import Any

import httpx
import pytest

from meshforge.controller.server import ControllerRuntime

from test_v09_g3_forced_runs import (  # type: ignore[import-not-found]  # noqa: F401
    _agent,
    _create,
    _runs,
    _spec,
    engine,
    http,
    settings,
)

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]


async def _setup(
    http: httpx.AsyncClient, runtime: ControllerRuntime, a: Any, reps: int
) -> tuple[Any, Any, Any]:
    created = await _create(
        http, _spec(a.node_id, strategies="[local]", reps=reps)
    )
    submitter, store = runtime.bench_submitter, runtime.bench
    assert submitter is not None and store is not None
    campaign = await store.get_campaign(created["campaign_id"])
    assert campaign is not None
    return campaign, submitter, store


async def _bench_jobs(http: httpx.AsyncClient) -> list[dict[str, Any]]:
    jobs = (await http.get("/v1/jobs", params={"limit": 500})).json()["jobs"]
    return [j for j in jobs if j["benchmark"]]


@pytest.mark.parametrize("seed", range(6))
async def test_random_interleavings_one_submitted_per_run_no_legit_cancel(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: int,
) -> None:
    rng = random.Random(seed)
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        campaign, submitter, store = await _setup(http, runtime, a, reps=6)
        real_mark = store.mark_submitted
        cancelled: list[str] = []
        real_cancel = submitter.cancel_job

        async def spy_cancel(job_id: str) -> Any:
            cancelled.append(job_id)
            if real_cancel is not None:
                return await real_cancel(job_id)

        submitter.cancel_job = spy_cancel

        async def racy_mark(run_id: str, job_id: str, **kw: Any) -> bool:
            r = rng.random()
            if r < 0.4:
                await store.sync(campaign.id)  # sweep wins
            elif r < 0.7:
                await asyncio.sleep(rng.random() * 0.05)
            return await real_mark(run_id, job_id, **kw)

        monkeypatch.setattr(store, "mark_submitted", racy_mark)
        stop = asyncio.Event()

        async def sweeper() -> None:
            while not stop.is_set():
                await store.sync(campaign.id)
                await asyncio.sleep(rng.random() * 0.02)

        sweep_task = asyncio.create_task(sweeper())
        runs = await store.runs(campaign.id, limit=100)
        assert len(runs) == 6
        tasks = [
            submitter.submit_run(campaign, run, owner_name=None)
            for run in runs
            for _ in range(rng.randint(1, 3))  # duplicate concurrent submitters
        ]
        rng.shuffle(tasks)
        outcomes = await asyncio.gather(*tasks)
        stop.set()
        await sweep_task
        monkeypatch.setattr(store, "mark_submitted", real_mark)
        await store.sync(campaign.id)

        per_run: dict[str, list[str]] = {}
        jobs_of: dict[str, set[str | None]] = {}
        for o in outcomes:
            per_run.setdefault(o.run_id, []).append(o.outcome)
            jobs_of.setdefault(o.run_id, set()).add(o.job_id)
        for run_id, kinds in per_run.items():
            assert kinds.count("submitted") == 1, (seed, run_id, kinds)
            assert len(jobs_of[run_id]) == 1, (seed, run_id, jobs_of[run_id])
        assert not cancelled, f"legit job cancelled: {cancelled}"
        final = await _runs(http, campaign.id)
        assert all(r["state"] == "submitted" and r["job_id"] for r in final)
        bench = await _bench_jobs(http)
        assert len(bench) == 6
        assert {j["job_id"] for j in bench} == {r["job_id"] for r in final}
        for j in bench:
            await http.post(f"/v1/jobs/{j['job_id']}/cancel")
    finally:
        await a.stop()


async def test_run_link_distinguishes_sweep_from_mark(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        campaign, submitter, store = await _setup(http, runtime, a, reps=3)
        assert await store.run_link("nope") == (None, False)
        r0, r1, r2 = await store.runs(campaign.id, limit=3)
        assert await store.run_link(r0.id) == (None, False)
        # run 0: linked by mark_submitted -> never "by sweep"
        o0 = await submitter.submit_run(campaign, r0, owner_name=None)
        assert o0.outcome == "submitted"
        assert await store.run_link(r0.id) == (o0.job_id, False)
        # run 1: swept
        real_mark = store.mark_submitted

        async def sweep(run_id: str, job_id: str, **kw: Any) -> bool:
            await store.sync(campaign.id)
            return await real_mark(run_id, job_id, **kw)

        monkeypatch.setattr(store, "mark_submitted", sweep)
        o1 = await submitter.submit_run(campaign, r1, owner_name=None)
        assert o1.outcome == "submitted"
        assert await store.run_link(r1.id) == (o1.job_id, True)
        assert r2.id
        for j in await _bench_jobs(http):
            await http.post(f"/v1/jobs/{j['job_id']}/cancel")
    finally:
        await a.stop()


async def test_run_skipped_during_window_cancels_the_created_job(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        campaign, submitter, store = await _setup(http, runtime, a, reps=3)
        run = (await store.runs(campaign.id, limit=1))[0]
        real_mark = store.mark_submitted

        async def skip_first(run_id: str, job_id: str, **kw: Any) -> bool:
            assert await store.mark_skipped(run_id, "cancelled by test")
            return await real_mark(run_id, job_id, **kw)

        monkeypatch.setattr(store, "mark_submitted", skip_first)
        o = await submitter.submit_run(campaign, run, owner_name=None)
        assert o.outcome == "already"
        monkeypatch.setattr(store, "mark_submitted", real_mark)
        await store.sync(campaign.id)  # backstop must not link a skipped run
        final = (await _runs(http, campaign.id))[0]
        assert final["state"] == "skipped" and not final["job_id"]
        for _ in range(50):
            job = (await http.get(f"/v1/jobs/{o.job_id}")).json()
            if job["state"] in ("cancelled", "canceled"):
                break
            await asyncio.sleep(0.2)
        assert job["state"] in ("cancelled", "canceled"), job["state"]
    finally:
        await a.stop()


async def test_submit_next_batch_continues_past_swept_runs_and_counts_once(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        campaign, submitter, store = await _setup(http, runtime, a, reps=5)
        real_mark = store.mark_submitted
        n = 0

        async def alt(run_id: str, job_id: str, **kw: Any) -> bool:
            nonlocal n
            n += 1
            if n % 2 == 1:  # every other run is swept first
                await store.sync(campaign.id)
            return await real_mark(run_id, job_id, **kw)

        monkeypatch.setattr(store, "mark_submitted", alt)
        batch = await submitter.submit_next(campaign, 4)
        assert batch.submitted == 4 and not batch.stale
        assert [o.outcome for o in batch.outcomes] == ["submitted"] * 4
        counts = [r["state"] for r in await _runs(http, campaign.id)]
        assert counts.count("submitted") == 4 and counts.count("planned") == 1
        assert len(await _bench_jobs(http)) == 4
        # second call finishes the last one, then nothing is left
        monkeypatch.setattr(store, "mark_submitted", real_mark)
        b2 = await submitter.submit_next(campaign, 4)
        assert b2.submitted == 1
        b3 = await submitter.submit_next(campaign, 4)
        assert b3.submitted == 0
        for j in await _bench_jobs(http):
            await http.post(f"/v1/jobs/{j['job_id']}/cancel")
    finally:
        await a.stop()
