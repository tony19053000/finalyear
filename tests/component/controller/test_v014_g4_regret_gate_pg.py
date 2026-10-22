"""V0.14 G4 (D319) on a real PostgreSQL: the calibration refresh's regret check.

A campaign (2 nodes, ``local`` on the origin + ``mode1``, 3 repetitions, one
group per ``repeats`` value) runs through the real dispatcher; every attempt
ends ``done`` with a measured ``run`` phase, so the runs are both **labelled
groups** and **matched runs** for the calibration. The node uncalibrated
``cost_model`` picks (``P``) is also the measured best of every group (regret
0), but its ``run`` phase is 10x the planner's guess - so a new calibration
slows ``P``, beats "no calibration" on the D306 prediction-error check, and
makes calibrated ``cost_model`` pick the other node: worse picks.

* >= 10 labelled groups in the held-out slice -> the version is ``rejected``
  with the regret reason, ``fit_inputs`` holds the D319 numbers, no version
  becomes active;
* < 10 groups -> the regret check is ``skipped (too_few_groups)``, written as
  the active row's ``reason``; the error check alone decided;
* ``regret_check=False`` (the pre-D319 behaviour) activates the same fit.
"""

from __future__ import annotations

import asyncio
import dataclasses
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
    preferred_node,
    register_nodes,
)
from sqlalchemy import text

from meshforge.controller import calibration_store
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner.calibration import CalibrationConfig
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
    return await preferred_node(engine, item)


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


async def test_ten_groups_worse_picks_reject_the_version(engine: Any) -> None:
    await _seed(engine, 10)
    result = await CalibrationStore(engine).refresh(CFG)
    assert result.row is not None, result
    row = result.row
    # the D306 error check alone would have accepted it ...
    assert row.holdout_error_new is not None and row.holdout_error_active is not None
    assert row.holdout_error_new < row.holdout_error_active
    # ... the D319 regret check rejects it
    assert row.status == "rejected", row
    assert row.reason is not None and row.reason.startswith("holdout_regret")
    assert row.fit_inputs["regret_check"] == "rejected"
    assert row.fit_inputs["regret_groups"] >= 10
    assert row.fit_inputs["holdout_regret_active"] == 0.0
    assert row.fit_inputs["holdout_regret_new"] > 0.0
    statuses = [r.status for r in await _rows(engine)]
    assert statuses == ["rejected"]  # nothing went live


async def test_fewer_groups_skip_the_check_and_say_so(engine: Any) -> None:
    await _seed(engine, 4)
    result = await CalibrationStore(engine).refresh(CFG)
    assert result.row is not None
    row = result.row
    assert row.status == "active"
    assert row.reason is not None
    assert row.reason.startswith("regret check skipped (too_few_groups: ")
    assert row.fit_inputs["regret_check"] == "skipped"
    assert row.fit_inputs["regret_groups"] < 10


async def _advisory_locks(engine: Any) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(
                    text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'")
                )
            ).scalar_one()
        )


async def test_the_regret_check_runs_before_the_refresh_lock(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """G4 fix round 1 (reviewer N2): the expensive D319 work (label reads,
    replays) holds no advisory lock; the stored result is the same."""
    await _seed(engine, 10)
    original = calibration_store.load_for_jobs
    held: list[int] = []

    async def spy(*args: Any, **kwargs: Any) -> Any:
        held.append(await _advisory_locks(engine))
        return await original(*args, **kwargs)

    monkeypatch.setattr(calibration_store, "load_for_jobs", spy)
    result = await CalibrationStore(engine).refresh(CFG)
    assert held == [0], held  # ran once, outside the lock
    assert result.row is not None and result.row.status == "rejected"


async def test_a_change_while_unlocked_is_judged_again_under_the_lock(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The prepared result is stale (here: another version was activated in
    between) -> the refresh refits and re-checks under the lock; the row is
    still judged and stored correctly, and the check then ran twice."""
    await _seed(engine, 10)
    store = CalibrationStore(engine)
    original = store._prepare
    calls: list[int] = []

    async def stale(*args: Any, **kwargs: Any) -> Any:
        prepared = await original(*args, **kwargs)
        return dataclasses.replace(prepared, active_version=-1)

    real_load = calibration_store.load_for_jobs

    async def count(*args: Any, **kwargs: Any) -> Any:
        calls.append(await _advisory_locks(engine))
        return await real_load(*args, **kwargs)

    monkeypatch.setattr(store, "_prepare", stale)
    monkeypatch.setattr(calibration_store, "load_for_jobs", count)
    result = await store.refresh(CFG)
    assert result.row is not None and result.row.status == "rejected"
    assert result.row.fit_inputs["regret_check"] == "rejected"
    assert len(calls) == 2 and calls[0] == 0 and calls[1] >= 1  # 2nd under lock
    statuses = [r.status for r in await _rows(engine)]
    assert statuses == ["rejected"]


async def test_without_the_check_the_same_fit_goes_live(engine: Any) -> None:
    await _seed(engine, 10)
    result = await CalibrationStore(engine, regret_check=False).refresh(CFG)
    assert result.row is not None
    assert result.row.status == "active" and result.row.reason is None
    assert "regret_check" not in result.row.fit_inputs
