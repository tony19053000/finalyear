"""V0.14 G2 tester round 2 - verify fix round 1 on a real PostgreSQL.

* restored strength: concurrent refreshes that *each* must write (different
  settings -> different fits) still leave exactly one ``active`` row;
* NB1: a late-finishing old decision changes the matched set without changing
  the newest decision id -> refit; a changed window/clamp forces a refit;
  pre-fix rows (no fingerprint keys) never match and then skipping resumes;
* NB2: cancelling a refresh while the fit runs in the worker thread writes
  nothing and releases the advisory lock.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from meshforge.controller import calibration_store
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

from test_v014_g2_tester_r1_pg import (  # noqa: F401  (fixtures)
    CFG,
    Manager,
    calibrations,
    dispatch_one,
    finish,
    new_node,
    planner_for,
    seed,
    sql,
    statuses,
    store,
)

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


async def test_concurrent_refreshes_that_each_write_leave_one_active(
    pg_settings: DatabaseSettings, store: Any, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [90.0] * 10)
    engines = [create_engine(pg_settings, pool_size=2) for _ in range(6)]
    try:
        stores = [CalibrationStore(e) for e in engines]
        for round_ in range(3):
            # distinct settings per caller: none is a "no_new_runs" skip, all
            # six must write, racing for the single active slot
            cfgs = [
                CalibrationConfig(shrink_k=CFG.shrink_k + round_ * 10 + i)
                for i in range(len(stores))
            ]
            results = await asyncio.gather(
                *(s.refresh(c) for s, c in zip(stores, cfgs, strict=True)),
                return_exceptions=True,
            )
            assert [r for r in results if isinstance(r, BaseException)] == []
            written = [r for r in results if r.row is not None]  # type: ignore[union-attr]
            # the lock serialises; same settings can't repeat, so every one writes
            assert len(written) == 6
            n = await sql(
                pg_settings,
                "SELECT count(*) AS n FROM planner_calibration WHERE status='active'",
            )
            assert n[0].n == 1
    finally:
        for e in engines:
            await e.dispose()
    rows = await statuses(pg_settings)
    assert len(rows) == 18
    assert [s for _v, s in rows].count("active") == 1
    versions = [v for v, _s in rows]
    assert versions == sorted(set(versions))


async def test_a_late_finishing_old_decision_forces_a_refit(
    pg_settings: DatabaseSettings, store: Any, calibrations: CalibrationStore
) -> None:
    """The set changes while count... and the max decision id stay different
    only by the digest; ordering is by ended_at so an old decision finishing
    last is read last."""
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    # dispatch 12 attempts first (ids ascending), finish only the later ones
    ids = []
    for _ in range(8):
        ids.append(await dispatch_one(store, planner, fingerprint=f"{next_fp():064x}"))
    for i, a in enumerate(ids[2:]):
        await finish(pg_settings, a, run_s=90.0, at=100 + i)
    first = await calibrations.refresh(CFG)
    assert first.row is not None
    n0 = first.row.fit_inputs["matched_runs"]
    newest0 = first.row.fit_inputs["newest_decision_id"]
    assert (await calibrations.refresh(CFG)).skipped == "no_new_runs"
    # the OLDEST decision finishes now, with the OLDEST ended_at: the set
    # grows, the newest decision id is unchanged
    await finish(pg_settings, ids[0], run_s=95.0, at=1)
    second = await calibrations.refresh(CFG)
    assert second.row is not None, "late-finishing old decision was missed"
    assert second.row.fit_inputs["matched_runs"] == n0 + 1
    assert second.row.fit_inputs["newest_decision_id"] == newest0
    assert second.row.fit_inputs["runs_digest"] != first.row.fit_inputs["runs_digest"]
    # swap: same count, same max id, different member (digest only)
    assert (await calibrations.refresh(CFG)).skipped == "no_new_runs"


async def test_same_count_same_max_different_member_is_refit(
    pg_settings: DatabaseSettings, store: Any, calibrations: CalibrationStore
) -> None:
    """Window-limited read: a run finishing late with an old ended_at pushes
    another out of a ``max_runs`` window; count and newest id stay equal."""
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    ids = [
        await dispatch_one(store, planner, fingerprint=f"{next_fp():064x}")
        for _ in range(8)
    ]
    for i, a in enumerate(ids[1:]):
        await finish(pg_settings, a, run_s=90.0, at=100 + i)  # ids[1..7]
    first = await calibrations.refresh(CFG, max_runs=5)
    assert first.row is not None and first.row.fit_inputs["matched_runs"] == 5
    # ids[0] finishes now with the newest ended_at: it enters the newest-5
    # window and the oldest of the five leaves; count 5, max id still ids[7]?
    await finish(pg_settings, ids[0], run_s=95.0, at=1000)
    second = await calibrations.refresh(CFG, max_runs=5)
    assert second.row is not None, "membership changed but refresh skipped"
    assert second.row.fit_inputs["matched_runs"] == 5
    assert second.row.fit_inputs["runs_digest"] != first.row.fit_inputs["runs_digest"]


@pytest.mark.parametrize(
    "other",
    [
        CalibrationConfig(window_runs=CFG.window_runs + 1),
        CalibrationConfig(clamp=(0.5, 2.0)),
    ],
    ids=["window", "clamp"],
)
async def test_a_changed_window_or_clamp_forces_a_refit(
    pg_settings: DatabaseSettings,
    store: Any,
    calibrations: CalibrationStore,
    other: CalibrationConfig,
) -> None:
    assert other.window_runs != CFG.window_runs or other.clamp != CFG.clamp
    node = await new_node(pg_settings)
    await seed(pg_settings, store, planner_for(Manager([node])), [90.0] * 10)
    assert (await calibrations.refresh(CFG)).row is not None
    assert (await calibrations.refresh(CFG)).skipped == "no_new_runs"
    changed = await calibrations.refresh(other)
    assert changed.row is not None
    assert (await calibrations.refresh(other)).skipped == "no_new_runs"
    # back to the first settings: newest row is now "other" -> refit again
    assert (await calibrations.refresh(CFG)).row is not None


async def test_rows_without_a_fingerprint_are_refit_once_then_skipped(
    pg_settings: DatabaseSettings, store: Any, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    await seed(pg_settings, store, planner_for(Manager([node])), [90.0] * 10)
    assert (await calibrations.refresh(CFG)).row is not None
    # simulate a pre-fix row: strip the fingerprint keys
    await sql(
        pg_settings,
        "UPDATE planner_calibration SET fit_inputs = fit_inputs::jsonb "
        "- 'matched_runs' - 'newest_decision_id' - 'runs_digest'",
    )
    again = await calibrations.refresh(CFG)
    assert again.row is not None and again.skipped is None
    assert again.row.fit_inputs["runs_digest"]
    assert (await calibrations.refresh(CFG)).skipped == "no_new_runs"
    rows = await statuses(pg_settings)
    assert [s for _v, s in rows].count("active") == 1


async def test_cancelling_during_the_threaded_fit_writes_nothing(
    pg_settings: DatabaseSettings,
    store: Any,
    calibrations: CalibrationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = await new_node(pg_settings)
    await seed(pg_settings, store, planner_for(Manager([node])), [90.0] * 10)
    entered = threading.Event()
    release = threading.Event()
    real = calibration_store.refit

    def slow(*a: Any, **k: Any) -> Any:
        entered.set()
        release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(calibration_store, "refit", slow)
    task = asyncio.create_task(calibrations.refresh(CFG))
    for _ in range(200):
        if entered.is_set():
            break
        await asyncio.sleep(0.02)
    assert entered.is_set()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.2)  # let the orphan thread finish
    assert await statuses(pg_settings) == []
    # the advisory lock and the connection are free again
    monkeypatch.setattr(calibration_store, "refit", real)
    result = await asyncio.wait_for(calibrations.refresh(CFG), 20)
    assert result.row is not None and result.row.status == "active"
    assert len(await statuses(pg_settings)) == 1


async def test_concurrent_refreshes_with_new_runs_between_rounds(
    pg_settings: DatabaseSettings, store: Any, calibrations: CalibrationStore
) -> None:
    """Same settings, runs arriving while refreshes race: never two active,
    never an error, never a duplicate for an unchanged set."""
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [90.0] * 4)
    engines = [create_engine(pg_settings, pool_size=2) for _ in range(4)]
    try:
        stores = [CalibrationStore(e) for e in engines]

        async def feeder() -> None:
            for j in range(4):
                await seed(pg_settings, store, planner, [90.0], start=500 + j)
                await asyncio.sleep(0.05)

        async def hammer(s: CalibrationStore) -> None:
            for _ in range(6):
                await s.refresh(CFG)
                await asyncio.sleep(0.01)

        await asyncio.gather(feeder(), *(hammer(s) for s in stores))
    finally:
        for e in engines:
            await e.dispose()
    rows = await statuses(pg_settings)
    assert [s for _v, s in rows].count("active") == 1
    fp = await sql(pg_settings, "SELECT fit_inputs->>'runs_digest' AS d FROM planner_calibration")
    digests = [r.d for r in fp]
    assert len(digests) == len(set(digests)), "duplicate row for one run set"
    assert (await calibrations.refresh(CFG)).skipped in ("no_new_runs", None)


_FPC = iter(range(10**9, 2 * 10**9))


def next_fp() -> int:
    return next(_FPC)
