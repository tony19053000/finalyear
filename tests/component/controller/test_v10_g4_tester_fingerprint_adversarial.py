"""V1.0 G4 (D412, F-2) tester: try to make the window fingerprint answer
``no_new_runs`` where the full (pre-F-2) path would not.

Method: seed a window, refresh (memo store), mutate the database behind the
store's back, then refresh the memo store **and** a fresh store (no memo = the
full path). The memo's answer must agree with the full path's: a skip must be a
skip on the full path too; a stored row must be a skip on the full path.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")

from test_v014_g2_calibration_pg import (
    CFG,
    Manager,
    _node,
    _planner,
    _seed,
    _sql,
    _statuses,
    calibrations,
    store,
)

from meshforge.controller.calibration_store import (
    CalibrationStore,
    RefreshResult,
    window_fingerprint,
)
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]
__all__ = ["calibrations", "store"]


@pytest_asyncio.fixture
async def fresh(pg_settings: DatabaseSettings) -> AsyncIterator[CalibrationStore]:
    engine = create_engine(pg_settings, pool_size=3)
    try:
        yield CalibrationStore(engine)
    finally:
        await engine.dispose()


async def _fp(pg: DatabaseSettings, limit: int = 4000) -> tuple[int, str | None]:
    engine = create_engine(pg)
    try:
        async with create_session_factory(engine)() as db:
            return await window_fingerprint(db, limit)
    finally:
        await engine.dispose()


async def _agree(memo: RefreshResult, fresh: CalibrationStore, **kw: int) -> None:
    """The memo answer must be the full path's answer (see module docstring)."""
    from test_v014_g2_calibration_pg import CFG

    full = await fresh.refresh(CFG, **kw)
    if memo.row is None:
        assert memo.skipped == full.skipped and full.row is None, (memo, full)
        assert memo.matched_runs == full.matched_runs
    else:
        assert full.skipped == "no_new_runs", (memo, full)
        assert full.matched_runs == memo.matched_runs


async def _setup(
    pg: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> tuple[str, list[str]]:
    node = await _node(pg)
    await _seed(pg, store, _planner(Manager([node])), [90.0] * 10)
    first = await calibrations.refresh(CFG)
    assert first.row is not None
    assert (await calibrations.refresh(CFG)).skipped == "no_new_runs"
    ids = [
        str(r.id)
        for r in await _sql(pg, "SELECT id FROM attempt ORDER BY ended_at, id")
    ]
    return node, ids


# (``plan_decision.outcome`` cannot be changed away from dispatched: a CHECK
# constraint ties it to the attempt - not a mutation the database allows.)
_MUTATIONS = {
    "phase_duration_changed": (
        "UPDATE attempt_phase SET duration_ms = duration_ms + 1 "
        "WHERE phase = 'run' AND attempt_id = (SELECT id FROM attempt "
        "ORDER BY ended_at LIMIT 1)"
    ),
    "phase_row_deleted_run_drops_out": (
        "DELETE FROM attempt_phase WHERE phase = 'run' AND attempt_id = "
        "(SELECT id FROM attempt ORDER BY ended_at LIMIT 1)"
    ),
    "phase_skipped_flag_flipped": (
        "UPDATE attempt_phase SET skipped = true WHERE phase = 'run' AND "
        "attempt_id = (SELECT id FROM attempt ORDER BY ended_at LIMIT 1)"
    ),
    "candidates_changed_same_count": (
        "UPDATE plan_decision SET candidates = '[]'::jsonb WHERE id = "
        "(SELECT min(id) FROM plan_decision)"
    ),
    "chosen_node_changed": (
        "UPDATE plan_decision SET chosen_node_id = NULL WHERE id = "
        "(SELECT min(id) FROM plan_decision)"
    ),
    "attempt_role_changed": (
        "UPDATE attempt SET role = 'lane' WHERE id = "
        "(SELECT id FROM attempt ORDER BY ended_at LIMIT 1)"
    ),
    "attempt_no_longer_done": (
        "UPDATE attempt SET state = 'failed' WHERE id = "
        "(SELECT id FROM attempt ORDER BY ended_at LIMIT 1)"
    ),
    "attempt_ended_at_cleared": (
        "UPDATE attempt SET ended_at = NULL WHERE id = "
        "(SELECT id FROM attempt ORDER BY ended_at LIMIT 1)"
    ),
    "job_fingerprint_changed": (
        "UPDATE job SET fingerprint = repeat('e', 64) WHERE id = "
        "(SELECT job_id FROM plan_decision ORDER BY id LIMIT 1)"
    ),
}


@pytest.mark.parametrize("name", sorted(_MUTATIONS))
async def test_memo_agrees_with_the_full_path_after_a_mutation(
    name: str,
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    fresh: CalibrationStore,
) -> None:
    await _setup(pg_settings, store, calibrations)
    await _sql(pg_settings, _MUTATIONS[name])
    memo = await calibrations.refresh(CFG)
    await _agree(memo, fresh)
    # and it stays consistent on the next round
    again = await calibrations.refresh(CFG)
    assert again.skipped in {"no_new_runs", "too_few_runs", "nothing_to_fit"}


@pytest.mark.parametrize(
    "name", ["phase_duration_changed", "candidates_changed_same_count"]
)
async def test_same_count_different_content_changes_the_fingerprint(
    name: str,
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    await _setup(pg_settings, store, calibrations)
    before = await _fp(pg_settings)
    await _sql(pg_settings, _MUTATIONS[name])
    after = await _fp(pg_settings)
    assert after[0] == before[0] and after[1] != before[1]


async def test_a_row_deleted_and_another_inserted_keeps_count_but_not_digest(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    fresh: CalibrationStore,
) -> None:
    """Swap a run for a newer one: count unchanged (window == 10), run set
    different. The memo must refit."""
    node, _ = await _setup(pg_settings, store, calibrations)
    before = await _fp(pg_settings, 10)
    await _seed(pg_settings, store, _planner(Manager([node])), [90.0], start=500)
    after = await _fp(pg_settings, 10)
    assert before[0] == after[0] == 10 and before[1] != after[1]
    # a bounded window: the memo keyed on limit 10 must not answer for 4000
    small = await calibrations.refresh(CFG, max_runs=10)
    assert small.row is not None or small.skipped == "no_new_runs"
    memo = await calibrations.refresh(CFG)
    await _agree(memo, fresh)


async def test_a_different_read_bound_never_uses_the_memo(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
) -> None:
    await _setup(pg_settings, store, calibrations)
    reads = calibrations.full_reads
    smaller = await calibrations.refresh(CFG, max_runs=5)
    assert calibrations.full_reads > reads  # limit differs: full path
    assert smaller.matched_runs in (5, 0) or smaller.skipped is not None


async def test_active_version_change_is_judged_like_the_full_path(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    fresh: CalibrationStore,
) -> None:
    """An operator demotes the active row behind the store's back; the newest
    row still holds the same run set, so the pre-F-2 rule says no_new_runs and
    the memo must say the same (D306 gate semantics identical)."""
    await _setup(pg_settings, store, calibrations)
    await _sql(pg_settings, "UPDATE planner_calibration SET status = 'superseded'")
    memo = await calibrations.refresh(CFG)
    await _agree(memo, fresh)


async def test_deleted_newest_calibration_row_refits(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    fresh: CalibrationStore,
) -> None:
    await _setup(pg_settings, store, calibrations)
    await _sql(pg_settings, "DELETE FROM planner_calibration")
    memo = await calibrations.refresh(CFG)
    assert memo.row is not None, memo  # nothing stored -> must fit again
    await _agree(memo, fresh)
    assert len(await _statuses(pg_settings)) == 1


async def test_whole_history_deleted_is_not_answered_from_the_memo(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    fresh: CalibrationStore,
) -> None:
    await _setup(pg_settings, store, calibrations)
    await _sql(pg_settings, "DELETE FROM attempt_phase")
    memo = await calibrations.refresh(CFG)
    await _agree(memo, fresh)
    assert memo.matched_runs == 0


async def test_run_landing_between_fingerprint_and_read_is_never_lost(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    fresh: CalibrationStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A run commits right after the (locked) fingerprint, before the read.
    The stored row may include it, but the memo's fingerprint is older than the
    window, so the next refresh must not return a wrong answer."""
    import meshforge.controller.calibration_store as mod

    node = await _node(pg_settings)
    planner = _planner(Manager([node]))
    await _seed(pg_settings, store, planner, [90.0] * 10)
    real = mod.window_fingerprint
    state = {"calls": 0}

    async def racing(db, limit):  # type: ignore[no-untyped-def]
        out = await real(db, limit)
        state["calls"] += 1
        if state["calls"] == 2:  # the locked one (calls: _still_unchanged skipped)
            await _seed(pg_settings, store, planner, [90.0], start=900)
        return out

    monkeypatch.setattr(mod, "window_fingerprint", racing)
    first = await calibrations.refresh(CFG)
    monkeypatch.undo()
    assert first.row is not None
    nxt = await calibrations.refresh(CFG)
    await _agree(nxt, fresh)
    # whatever happened, the stored run sets cover all 11 runs eventually
    assert (
        max(
            int(r.n)
            for r in await _sql(
                pg_settings,
                "SELECT (fit_inputs->>'matched_runs')::int AS n FROM planner_calibration",
            )
        )
        == 11
    )
