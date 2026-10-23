"""V0.15 G5a tester: failure injection around the retrain task's D371 regret
check on a real PostgreSQL + CAS: concurrent retrains, a database failure and
a crashing check store nothing and leave the serving set alone."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from alembic import command
from mf_ml_synthetic import synthetic_rows
from sqlalchemy import text

from meshforge.common.cas import CasStore
from meshforge.controller.ml import export, regret
from meshforge.controller.ml.dataset import Dataset, parquet_bytes, sort_key
from meshforge.controller.ml.registry import ModelRegistry
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.ml_retrain_task import MlRetrainer
from meshforge.controller.planner_eval.loader import EvalLoadError
from meshforge.controller.settings import PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=6)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def cas(tmp_path: Path) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    return store


def _built() -> export.BuiltDataset:
    rows = sorted(
        synthetic_rows("signal", families=6, groups_per_family=5, runs_per_group=10),
        key=sort_key,
    )
    data = parquet_bytes(rows)
    ds = Dataset(
        rows=tuple(rows),
        counts={"rows": len(rows)},
        parquet=data,
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
    )
    return export.BuiltDataset(dataset=ds, max_runs=1000, truncated=False)


def _task(
    engine: Any, cas: CasStore, **kw: Any
) -> tuple[MlRetrainer, ModelSource, Any]:
    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")
    return (
        MlRetrainer(engine, registry, source, PlannerSettings(ml_min_rows=100), **kw),
        source,
        registry,
    )


async def _count(engine: Any) -> int:
    async with engine.connect() as conn:
        return int(
            (await conn.execute(text("SELECT count(*) FROM ml_model_set"))).scalar()
        )


async def test_concurrent_retrains_store_exactly_one_set(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _built()

    async def fake_build(_e: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    task, _source, _reg = _task(engine, cas)
    results = await asyncio.gather(*(task.retrain_now("manual") for _ in range(4)))
    skipped = [r.skipped for r in results]
    assert skipped.count("already_running") == 3, skipped
    assert sum(1 for r in results if r.row is not None) == 1
    assert await _count(engine) == 1


async def test_two_retrainers_on_one_database_do_not_double_store(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two controller processes (two MlRetrainer objects): the advisory lock
    or the idempotent store means one set, no error."""
    built = _built()

    async def fake_build(_e: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    a, _s1, _r1 = _task(engine, cas)
    b, _s2, _r2 = _task(engine, cas)
    ra, rb = await asyncio.gather(a.retrain_now("manual"), b.retrain_now("manual"))
    assert ra.error is None and rb.error is None, (ra, rb)
    assert await _count(engine) == 1


async def test_a_database_failure_in_the_regret_check_stores_nothing(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _built()

    async def fake_build(_e: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    async def down(*_a: Any, **_k: Any) -> Any:
        raise EvalLoadError("db down", details={"cause": "OSError"})

    monkeypatch.setattr(export, "build_dataset", fake_build)
    monkeypatch.setattr(regret, "check_candidate", down)
    task, source, _reg = _task(engine, cas)
    result = await task.retrain_now("manual")
    assert result.error == "database" and result.row is None
    assert await _count(engine) == 0 and source.current is None
    # the database comes back: the next retrain is not blocked by the failure
    monkeypatch.undo()
    monkeypatch.setattr(export, "build_dataset", fake_build)
    again = await task.retrain_now("manual")
    assert again.error is None and again.row is not None
    assert again.row.status == "active"
    assert again.row.gate["regret"]["status"] in {"skipped", "passed"}


async def test_a_crashing_regret_check_is_reported_and_stores_nothing(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _built()

    async def fake_build(_e: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    async def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("planner bug")

    monkeypatch.setattr(export, "build_dataset", fake_build)
    monkeypatch.setattr(regret, "check_candidate", boom)
    task, source, _reg = _task(engine, cas)
    result = await task.retrain_now("manual")
    assert result.error == "crashed" and result.row is None
    assert await _count(engine) == 0 and source.current is None
    assert not task.running  # the lock was released


async def test_a_failing_live_calibration_read_is_not_a_silent_pass(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _built()

    async def fake_build(_e: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    async def bad_calibration() -> Any:
        raise OSError("calibration store down")

    monkeypatch.setattr(export, "build_dataset", fake_build)
    task, source, _reg = _task(engine, cas, calibration=bad_calibration)
    result = await task.retrain_now("manual")
    assert result.row is None, "a set must not activate when its check could not run"
    assert result.error is not None
    assert await _count(engine) == 0 and source.current is None
