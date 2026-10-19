"""V0.9 G5c tester r2: the per-node cool-down across campaigns (D257 T-2) -
real PostgreSQL. Different ``cooldown_s`` per campaign, every bench job kind,
every terminal state, the lookback boundary, and the runner's exact edge."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from test_v09_g5c_runner_cooldown import (  # type: ignore[import-not-found]
    _bench_job,
    _first_run,
    _runner,
    _setup,
    engine,  # noqa: F401  (fixture)
)

from meshforge.common.states import JobState
from meshforge.controller.bench.keys import filler_key, run_key, warmup_key
from meshforge.controller.jobs.transitions import JobEvent

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]


async def _age(engine: AsyncEngine, job_id: str, seconds: float) -> None:
    """Move the job's final transition + updated_at ``seconds`` into the past."""
    old = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=seconds)
    async with engine.begin() as conn:
        for sql in (
            "UPDATE job SET updated_at = :t WHERE id = :j",
            "UPDATE job_transition SET at = :t WHERE job_id = :j",
        ):
            await conn.execute(text(sql), {"t": old, "j": job_id})


async def _end_job(jobs: Any, job_id: str) -> None:
    await jobs.transition_job(job_id, JobEvent.CANCEL, expected=JobState.QUEUED)


async def test_tester_r2_short_campaign_job_holds_node_for_long_campaign_only(
    engine: AsyncEngine,  # noqa: F811
) -> None:
    """A (cooldown 5) ended a job 100 s ago on node_a. B (cooldown 300) must
    still see it (gap 100 < 300); A (cooldown 5) need not."""
    store, control, jobs, cid_a = await _setup(engine, cooldown_s=5.0)
    _s, _c, _j, cid_b = await _setup(engine, cooldown_s=300.0)
    run_a = await _first_run(store, cid_a, "node_a")
    job = await _bench_job(jobs, "node_a", run_key(run_a.id))
    await _end_job(jobs, job)
    await _age(engine, job, 100)
    _l, since_b = await control.node_slots(cid_b, {"node_a"})
    assert 99 < since_b["node_a"] < 130, since_b
    # A's own window (5 + 60 s) does not reach 100 s: absent, and 100 >= 5 anyway
    _l, since_a = await control.node_slots(cid_a, {"node_a"})
    assert since_a == {}


@pytest.mark.parametrize(
    "kind", ["run", "run_gen3", "filler", "warmup", "warmup_gen2"]
)
async def test_tester_r2_every_bench_job_kind_of_another_campaign_holds(
    engine: AsyncEngine,  # noqa: F811
    kind: str,
) -> None:
    store, control, jobs, cid_a = await _setup(engine, cooldown_s=30.0)
    _s, _c, _j, cid_b = await _setup(engine, cooldown_s=30.0)
    run_b = await _first_run(store, cid_b, "node_b")
    key = {
        "run": run_key(run_b.id),
        "run_gen3": run_key(run_b.id, 3),
        "filler": filler_key(run_b.id, 7),
        "warmup": warmup_key(run_b.id),
        "warmup_gen2": warmup_key(run_b.id, 2),
    }[kind]
    job = await _bench_job(jobs, "node_b", key)
    load, since = await control.node_slots(cid_a, {"node_a", "node_b"})
    assert load == {"node_b": 1} and since == {}
    await _end_job(jobs, job)
    load, since = await control.node_slots(cid_a, {"node_a", "node_b"})
    assert load == {} and set(since) == {"node_b"} and since["node_b"] < 30


@pytest.mark.parametrize("final", ["failed", "succeeded", "cancelled"])
async def test_tester_r2_every_terminal_state_counts(
    engine: AsyncEngine,  # noqa: F811
    final: str,
) -> None:
    _store, control, jobs, cid = await _setup(engine, cooldown_s=30.0)
    job = await _bench_job(jobs, "node_a", "bench:run_zzz:filler:1")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE job SET state = :s, updated_at = now() WHERE id = :j"),
            {"s": final, "j": job},
        )
    _l, since = await control.node_slots(cid, {"node_a"})
    assert set(since) == {"node_a"} and since["node_a"] < 30


async def test_tester_r2_non_terminal_and_unpinned_and_user_jobs_hold_nothing(
    engine: AsyncEngine,  # noqa: F811
) -> None:
    _store, control, jobs, cid = await _setup(engine, cooldown_s=30.0)
    user = await _bench_job(jobs, "node_a", "benchmark-but-not-bench-colon")
    await _end_job(jobs, user)
    lookalike = await _bench_job(jobs, "node_a", "xbench:run_1")
    await _end_job(jobs, lookalike)
    other_node = await _bench_job(jobs, "node_zzz", "bench:run_9")
    await _end_job(jobs, other_node)
    _l, since = await control.node_slots(cid, {"node_a", "node_b"})
    assert since == {}, since


async def test_tester_r2_latest_of_several_ends_wins(
    engine: AsyncEngine,  # noqa: F811
) -> None:
    _store, control, jobs, cid = await _setup(engine, cooldown_s=300.0)
    old = await _bench_job(jobs, "node_a", "bench:run_old")
    await _end_job(jobs, old)
    await _age(engine, old, 200)
    new = await _bench_job(jobs, "node_a", "bench:run_new:filler:1")
    await _end_job(jobs, new)
    await _age(engine, new, 20)
    _l, since = await control.node_slots(cid, {"node_a"})
    assert 19 < since["node_a"] < 40, since


async def test_tester_r2_lookback_boundary_uses_the_asking_campaigns_cooldown(
    engine: AsyncEngine,  # noqa: F811
) -> None:
    """Window = cooldown_s + 60 s. Just inside: read; just outside: absent -
    and absent never lets a node be submitted to early: anything outside the
    window is older than cooldown_s anyway."""
    _store, control, jobs, cid = await _setup(engine, cooldown_s=10.0)
    inside = await _bench_job(jobs, "node_a", "bench:run_in")
    await _end_job(jobs, inside)
    await _age(engine, inside, 65)  # < 70
    outside = await _bench_job(jobs, "node_b", "bench:run_out")
    await _end_job(jobs, outside)
    await _age(engine, outside, 80)  # > 70
    _l, since = await control.node_slots(cid, {"node_a", "node_b"})
    assert set(since) == {"node_a"} and 64 < since["node_a"] < 90, since
    assert all(v >= 10.0 for v in since.values())


async def test_tester_r2_runner_cooldown_edge_exactly_cooldown_is_free(
    engine: AsyncEngine,  # noqa: F811
) -> None:
    """Runner decision: held while gap < cooldown_s. 20 s ago with cooldown 30
    is held; 31 s ago with cooldown 30 is free - another campaign's filler."""
    store, control, jobs, cid_a = await _setup(engine, cooldown_s=30.0)
    _s, _c, _j, cid_b = await _setup(engine, cooldown_s=30.0)
    # campaign B is not started by anyone; only A is driven
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE bench_campaign SET state = 'paused' WHERE id = :c"),
            {"c": cid_b},
        )
    run_b = await _first_run(store, cid_b, "node_a")
    filler = await _bench_job(jobs, "node_a", filler_key(run_b.id, 1))
    await _end_job(jobs, filler)
    await _age(engine, filler, 20)
    runner, submitter = _runner(store, control)
    await runner.tick()
    assert all("node_a" not in b or b["node_a"] == 0 for b in submitter.budgets), (
        submitter.budgets
    )
    await _age(engine, filler, 31)
    submitter.budgets.clear()
    await runner.tick()
    assert any(b.get("node_a", 0) > 0 for b in submitter.budgets), submitter.budgets
