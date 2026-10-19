"""V0.9 G5c item 0.5 (D252.1/D254): two runners on one database never overfill
a node slot - real PostgreSQL, two engines (two "controllers").

Runner A's tick is held inside its submission (the gap where a second runner
would see the slot still free); runner B ticks meanwhile. With ``exclusive``
(the controller's runner) B does not get the advisory lock and skips; without
it - the contrast that shows the test can see the bug - B fills the same
slots and each node ends up with two benchmark jobs.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("testcontainers", reason="needs the dev dependency group")

from alembic import command
from sqlalchemy.ext.asyncio import AsyncEngine
from test_v09_g5c_runner_cooldown import (  # type: ignore[import-not-found]
    _setup,
    _submit,
)

from meshforge.controller.bench.control import CampaignControl
from meshforge.controller.bench.runner import (
    RUNNER_LOCK_KEY,
    BenchRunner,
    RunnerSettings,
)
from meshforge.controller.bench.store import BenchStore
from meshforge.controller.bench.submit import SubmitBatch
from meshforge.controller.jobs.store import JobStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def engines(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[tuple[AsyncEngine, AsyncEngine]]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    one, two = create_engine(pg_settings), create_engine(pg_settings)
    yield one, two
    await one.dispose()
    await two.dispose()


class _RealSubmitter:
    """Submits the first planned run of every budgeted node as a real
    benchmark job (what ``BenchSubmitter`` would leave in the database);
    ``hold`` keeps the submission open until the test releases it."""

    def __init__(self, store: BenchStore, jobs: JobStore) -> None:
        self.store, self.jobs = store, jobs
        self.entered = asyncio.Event()
        self.hold: asyncio.Event | None = None

    async def submit_next(
        self, record: Any, _count: int, *, node_budget: Any, **_kw: Any
    ) -> SubmitBatch:
        self.entered.set()
        if self.hold is not None:
            await self.hold.wait()
        planned = [
            r
            for r in await self.store.runs(record.id, limit=100)
            if r.state == "planned"
        ]
        for node in sorted(node_budget):
            run = next((r for r in planned if r.node_id == node), None)
            if run is not None:
                await _submit(self.jobs, self.store, run)
        return SubmitBatch()


def _runner(engine: AsyncEngine, *, exclusive: bool) -> tuple[BenchRunner, Any]:
    store, jobs = BenchStore(engine), JobStore(engine)
    submitter = _RealSubmitter(store, jobs)
    runner = BenchRunner(
        store=store,
        control=CampaignControl(engine),
        submitter=submitter,  # type: ignore[arg-type]
        settings=RunnerSettings(),
        exclusive=exclusive,
    )
    return runner, submitter


async def _race(
    engines: tuple[AsyncEngine, AsyncEngine], *, exclusive: bool
) -> tuple[dict[str, int], bool]:
    one, two = engines
    _store, control, _jobs, _cid = await _setup(one, cooldown_s=0.0)
    a, a_sub = _runner(one, exclusive=exclusive)
    b, _b_sub = _runner(two, exclusive=exclusive)
    a_sub.hold = asyncio.Event()
    a_tick = asyncio.create_task(a.tick())
    await asyncio.wait_for(a_sub.entered.wait(), 10)  # A is mid-submission
    b_report = await asyncio.wait_for(b.tick(), 10)
    a_sub.hold.set()
    await asyncio.wait_for(a_tick, 10)
    load = await control.node_bench_load({"node_a", "node_b"})
    return load, b_report.locked_out


async def test_two_exclusive_runners_never_overfill_a_slot(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    load, locked_out = await _race(engines, exclusive=True)
    assert locked_out  # B skipped its tick
    assert load == {"node_a": 1, "node_b": 1}


async def test_without_the_lock_the_same_race_overfills(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    load, locked_out = await _race(engines, exclusive=False)
    assert not locked_out
    assert load == {"node_a": 2, "node_b": 2}  # the bug the lock prevents


async def test_the_lock_is_released_when_the_tick_ends(
    engines: tuple[AsyncEngine, AsyncEngine],
) -> None:
    one, two = engines
    first, second = CampaignControl(one), CampaignControl(two)
    async with first.tick_lock(RUNNER_LOCK_KEY) as got:
        assert got
        async with second.tick_lock(RUNNER_LOCK_KEY) as also:
            assert not also
    async with second.tick_lock(RUNNER_LOCK_KEY) as got_now:
        assert got_now
    with pytest.raises(RuntimeError):  # a crashing tick releases it too
        async with first.tick_lock(RUNNER_LOCK_KEY):
            raise RuntimeError("tick crashed")
    async with second.tick_lock(RUNNER_LOCK_KEY) as after_crash:
        assert after_crash
