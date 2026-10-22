"""V0.15 G3 step 0 (G2 tech debt) on a real PostgreSQL: a run record that lands
**after** its attempt ended (the recorder is asynchronous) changes the row's
family - a "relabel" - and must move the D355 watermark, so the dataset
route and the retrain task rebuild."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text

from meshforge.common.ids import new_job_id, new_node_id
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(120)]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=3)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _record(engine: Any, family: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run_record (schema_version, attempt_id, job_id, "
                "attempt_no, node_id, strategy, placement_mode, attempt_state, "
                "failure_class, attempt_created_at, workload_family) VALUES (3, :a, "
                ":j, 1, :n, 'mode1', 'auto', 'done', 'ok', now(), :f)"
            ),
            {
                "a": f"att_{new_job_id()}",
                "j": str(new_job_id()),
                "n": str(new_node_id()),
                "f": family,
            },
        )


async def test_a_late_run_record_moves_the_watermark(engine: Any) -> None:
    from meshforge.controller.ml import loader

    before = await loader.read_watermark(engine, max_runs=100)
    assert before.run_records == 0 and before.newest_record is None
    await _record(engine, "matmul")
    after = await loader.read_watermark(engine, max_runs=100)
    assert after != before
    assert after.run_records == 1
    assert isinstance(after.newest_record, dt.datetime)
    # nothing else moved: only the record count / time tell the change
    assert (after.matched_runs, after.ended_attempts) == (
        before.matched_runs,
        before.ended_attempts,
    )
    assert after == await loader.read_watermark(engine, max_runs=100)  # stable
