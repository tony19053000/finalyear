"""V1.0 G4 (D412, F-2) on a real PostgreSQL: the calibration refresh's
run-set fingerprint.

* an unchanged window answers ``no_new_runs`` without reading the outcomes
  (``full_reads`` does not move) - the same answer a fresh store (no memo,
  the full pre-F-2 path) gives;
* a new run, a phase row that makes an old run count, a changed setting and
  an unreadable newest row all still refit (the D306 / ``no_new_runs`` rules
  are unchanged);
* a refresh whose window did not change between the unlocked prepare and the
  lock reads the outcomes once; a run landing in between is re-read under the
  lock and is in the stored row.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from test_v014_g2_calibration_pg import (
    CFG,
    Manager,
    _dispatch_one,
    _node,
    _planner,
    _seed,
    _sql,
    _statuses,
    calibrations,
    store,
)

from meshforge.controller.calibration_store import CalibrationStore, window_fingerprint
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

__all__ = ["calibrations", "store"]  # fixtures re-used from the V0.14 G2 module


@pytest_asyncio.fixture
async def fresh(pg_settings: DatabaseSettings) -> AsyncIterator[CalibrationStore]:
    """A second store per check: no memo, i.e. the full (pre-F-2) path."""
    engine = create_engine(pg_settings, pool_size=3)
    try:
        yield CalibrationStore(engine)
    finally:
        await engine.dispose()


async def _fingerprint(pg: DatabaseSettings, limit: int) -> tuple[int, str | None]:
    engine = create_engine(pg)
    try:
        async with create_session_factory(engine)() as db:
            return await window_fingerprint(db, limit)
    finally:
        await engine.dispose()


async def test_an_unchanged_window_is_no_new_runs_without_a_read(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    node = await _node(pg_settings)
    await _seed(pg_settings, store, _planner(Manager([node])), [90.0] * 10)
    first = await calibrations.refresh(CFG)
    assert first.row is not None and first.matched_runs == 10
    assert calibrations.full_reads == 1  # F-2 (b): no second read under the lock
    for _ in range(3):
        again = await calibrations.refresh(CFG)
        assert (again.row, again.skipped, again.matched_runs) == (
            None,
            "no_new_runs",
            10,
        )
    assert calibrations.full_reads == 1  # F-2 (a): answered from the fingerprint
    assert len(await _statuses(pg_settings)) == 1


async def test_the_early_out_gives_the_full_paths_answer(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    fresh: CalibrationStore,
) -> None:
    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    await _seed(pg_settings, store, planner, [90.0] * 10)
    assert (await calibrations.refresh(CFG)).row is not None
    memo = await calibrations.refresh(CFG)
    full = await fresh.refresh(CFG)
    assert fresh.full_reads == 1  # it did read
    assert (memo.row, memo.skipped, memo.matched_runs) == (
        full.row,
        full.skipped,
        full.matched_runs,
    )


async def test_a_new_run_refits(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    await _seed(pg_settings, store, planner, [90.0] * 10)
    assert (await calibrations.refresh(CFG)).row is not None
    before = await _fingerprint(pg_settings, 4000)
    await _seed(pg_settings, store, planner, [90.0], start=50)
    assert await _fingerprint(pg_settings, 4000) != before
    newer = await calibrations.refresh(CFG)
    assert newer.row is not None and newer.row.fit_inputs["matched_runs"] == 11


async def test_a_phase_row_that_makes_an_old_run_count_refits(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    """The run set depends on the phases, not only on which attempts ended:
    an attempt already ``done`` whose run phase lands later joins the set."""
    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    await _seed(pg_settings, store, planner, [90.0] * 10)
    late = await _dispatch_one(store, planner, fingerprint="f" * 64)
    await _sql(
        pg_settings,
        "UPDATE attempt SET state = 'done', "
        "ended_at = timestamptz '2026-10-01 00:00:00+00' + make_interval(secs => 60) "
        "WHERE id = :a",
        a=late,
    )
    first = await calibrations.refresh(CFG)
    assert first.row is not None and first.matched_runs == 10  # no phase: no match
    assert (await calibrations.refresh(CFG)).skipped == "no_new_runs"
    await _sql(
        pg_settings,
        "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
        "skipped) VALUES (:a, 'run', now(), 90000, false)",
        a=late,
    )
    later = await calibrations.refresh(CFG)
    assert later.row is not None and later.matched_runs == 11


async def test_a_changed_setting_refits(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    node = await _node(pg_settings)
    await _seed(pg_settings, store, _planner(Manager([node])), [90.0] * 10)
    assert (await calibrations.refresh(CFG)).row is not None
    assert (await calibrations.refresh(CFG)).skipped == "no_new_runs"
    other_k = CalibrationConfig(
        window_runs=CFG.window_runs, shrink_k=CFG.shrink_k + 1, clamp=CFG.clamp
    )
    changed = await calibrations.refresh(other_k)
    assert changed.row is not None  # same window, new settings: a new fit
    assert (await calibrations.refresh(other_k)).skipped == "no_new_runs"
    back = await calibrations.refresh(CFG)  # newest row is other_k's: refit
    assert back.row is not None
    assert len(await _statuses(pg_settings)) == 3


async def test_an_unreadable_newest_row_refits_even_with_the_memo(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    node = await _node(pg_settings)
    await _seed(pg_settings, store, _planner(Manager([node])), [90.0] * 10)
    first = await calibrations.refresh(CFG)
    assert first.row is not None
    await _sql(
        pg_settings,
        "UPDATE planner_calibration SET factors = '{\"format\": 9}'::jsonb "
        "WHERE version = :v",
        v=first.row.version,
    )
    again = await calibrations.refresh(CFG)
    assert again.row is not None and again.row.calibration is not None


async def test_a_run_landing_between_prepare_and_lock_is_re_read(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    await _seed(pg_settings, store, planner, [90.0] * 10)
    real_prepare = calibrations._prepare

    async def prepare_then_a_run_lands(*args: object, **kwargs: object) -> object:
        prepared = await real_prepare(*args, **kwargs)  # type: ignore[arg-type]
        await _seed(pg_settings, store, planner, [90.0], start=70)
        return prepared

    monkeypatch.setattr(calibrations, "_prepare", prepare_then_a_run_lands)
    result = await calibrations.refresh(CFG)
    assert calibrations.full_reads == 2  # the window moved: read again under the lock
    assert result.row is not None and result.row.fit_inputs["matched_runs"] == 11
    monkeypatch.undo()
    again = await calibrations.refresh(CFG)
    assert again.skipped == "no_new_runs" and again.matched_runs == 11
    assert calibrations.full_reads == 2
