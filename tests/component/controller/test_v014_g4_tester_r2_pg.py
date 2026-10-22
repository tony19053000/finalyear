"""V0.14 G4 tester round 2 - N2 on a real PostgreSQL: the refresh's D319 check
outside the lock, and the one-active invariant when refreshes race.

* many simultaneous refreshes from separate engines (also the very first
  activation, and the rejected case) leave at most one ``active`` row, unique
  versions, no IntegrityError;
* a refresh whose unlocked ``_prepare`` finished BEFORE another refresh
  activated a version is judged again under the lock (never stores a result
  computed against a stale active version) and the invariant holds;
* an exception in the unlocked part writes nothing and leaves no lock held.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from alembic import command
from mf_eval_pg import (
    Dispatched,
    dispatch_runs,
    finish_runs,
    make_campaign,
    planner,
    register_nodes,
)
from sqlalchemy import text

from meshforge.controller import calibration_store
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner.replay import shadow_from_replay
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

CFG = CalibrationConfig()
T0 = dt.datetime(2026, 10, 20, 9, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _preferred(engine: Any, item: Dispatched) -> str:
    """The node uncalibrated cost_model picks for this run (its shadow)."""
    async with engine.connect() as conn:
        replay = (
            await conn.execute(
                text("SELECT replay FROM plan_decision WHERE attempt_id = :a"),
                {"a": item.attempt_id},
            )
        ).scalar_one()
    shadow = shadow_from_replay(replay)
    assert shadow is not None
    decision = plan(shadow[0], shadow[1], "cost_model", shadow[2])
    assert decision.chosen is not None
    return decision.chosen.node_id


async def _seed(engine: Any, groups: int) -> str:
    nodes = await register_nodes(engine, 2)
    _cid, runs = await make_campaign(engine, nodes, list(range(20, 20 + groups)))
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
    picked = await _preferred(engine, dispatched[0])
    other = next(n for n in nodes if n != picked)

    def total(run: Any) -> int:  # the planner's pick really is the best
        return (1_000 if run.node_id == picked else 4_000) + run.repetition

    def run_ms(run: Any) -> int:  # ... but its run phase is 10x the 60 s guess
        return 600_000 if run.node_id == picked else 60_000

    # newest = the last repetition of each group (one per group in the
    # held-out fifth), so the held-out slice covers every group
    ordered = sorted(
        dispatched,
        key=lambda d: (d.run.repetition == 3, d.run.node_id == picked, d.run.group_id),
    )
    await finish_runs(
        engine,
        ordered,
        total,
        run_ms=run_ms,
        ended_at=lambda _d, i: T0 + dt.timedelta(seconds=i),
    )
    assert other != picked
    return picked


async def _rows(engine: Any) -> list[Any]:
    async with engine.connect() as conn:
        return list(
            (
                await conn.execute(
                    text(
                        "SELECT version, status, reason, fit_inputs FROM "
                        "planner_calibration ORDER BY version"
                    )
                )
            ).all()
        )


async def _extra_store(pg: DatabaseSettings) -> tuple[Any, CalibrationStore]:
    eng = create_engine(pg, pool_size=3)
    return eng, CalibrationStore(eng)


async def _active_count(engine: Any) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM planner_calibration WHERE status='active'"
                    )
                )
            ).scalar_one()
        )


@pytest.mark.parametrize("groups", [4, 10])
async def test_concurrent_refreshes_keep_one_active_and_unique_versions(
    engine: Any, pg_settings: DatabaseSettings, groups: int
) -> None:
    await _seed(engine, groups)
    pairs = [await _extra_store(pg_settings) for _ in range(6)]
    try:
        results = await asyncio.gather(
            *[s.refresh(CFG) for _e, s in pairs], return_exceptions=True
        )
    finally:
        for eng, _s in pairs:
            await eng.dispose()
    errors = [r for r in results if isinstance(r, BaseException)]
    assert not errors, errors
    rows = await _rows(engine)
    versions = [r.version for r in rows]
    assert len(versions) == len(set(versions))
    assert await _active_count(engine) <= 1
    if groups == 10:  # every fit is rejected by the regret check: nothing live
        assert await _active_count(engine) == 0
        assert all(r.status == "rejected" for r in rows)
    else:
        assert await _active_count(engine) == 1
    # identical inputs: refreshes after the first add no duplicate version
    assert len(rows) <= 6


async def test_prepare_finished_before_another_refresh_activated(
    engine: Any, pg_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A's unlocked prepare ran with NO active version; B then activates one;
    A must not store its (now stale) prepared result blindly."""
    await _seed(engine, 4)
    eng_a, a = await _extra_store(pg_settings)
    eng_b, b = await _extra_store(pg_settings)
    prepared_done = asyncio.Event()
    release = asyncio.Event()
    original = a._prepare

    async def slow(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        prepared_done.set()
        await release.wait()
        return result

    monkeypatch.setattr(a, "_prepare", slow)
    try:
        task = asyncio.create_task(a.refresh(CFG))
        await asyncio.wait_for(prepared_done.wait(), 120)
        first = await b.refresh(CFG)  # activates a version while A is parked
        assert first.row is not None and first.row.status == "active"
        release.set()
        second = await asyncio.wait_for(task, 120)
    finally:
        release.set()
        await eng_a.dispose()
        await eng_b.dispose()
    assert await _active_count(engine) == 1
    rows = await _rows(engine)
    assert [r.version for r in rows] == sorted({r.version for r in rows})
    if second.row is not None:
        # a new row was stored: it was judged against B's version, not "none"
        assert second.row.version > first.row.version
        assert second.row.holdout_error_active is not None
    # B's version is still the only one active unless A's replaced it cleanly
    assert sum(1 for r in rows if r.status == "active") == 1


async def test_failure_in_the_unlocked_part_writes_nothing_and_holds_no_lock(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed(engine, 10)
    store = CalibrationStore(engine)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise calibration_store.CalibrationStoreError("db down")

    monkeypatch.setattr(calibration_store, "load_for_jobs", boom)
    with pytest.raises(calibration_store.CalibrationStoreError):
        await store.refresh(CFG)
    assert await _rows(engine) == []
    assert await _advisory_locks(engine) == 0
    monkeypatch.undo()
    ok = await CalibrationStore(engine).refresh(CFG)  # the lock is not stuck
    assert ok.row is not None
