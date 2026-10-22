"""V0.15 G2 tester round 1: the dataset route cache under stress (D355) -
many concurrent requests, a holder that is cancelled, data landing *during* a
build, and a non-admin never getting anything out of a warm cache."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import httpx
import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("fastapi")

from fastapi import FastAPI
from mf_ml_fixtures import stored, whole_run
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.api import auth, ml_routes
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.ml import dataset as ds
from meshforge.controller.ml import export, loader
from meshforge.controller.ml.cache import DatasetCache

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ENGINE = cast(AsyncEngine, object())
PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}


class _World:
    def __init__(self) -> None:
        self.mark: Any = "w1"
        self.builds = 0
        self.watermark_reads = 0
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.during_build: Any = None  # the mark to set while a build runs
        self.data = ds.build([whole_run(stored(pinned="node_001"), phases=PHASES)])

    async def watermark(self, engine: Any, *, max_runs: int) -> object:
        self.watermark_reads += 1
        return (self.mark, max_runs)

    async def build(self, engine: Any, *, max_runs: int) -> export.BuiltDataset:
        self.builds += 1
        self.started.set()
        if self.during_build is not None:
            self.mark = self.during_build  # data lands while we build
        if self.gate is not None:
            await self.gate.wait()
        return export.BuiltDataset(
            dataset=self.data, max_runs=max_runs, truncated=False
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    w = _World()
    monkeypatch.setattr(loader, "read_watermark", w.watermark)
    monkeypatch.setattr(export, "build_dataset", w.build)
    return w


async def test_many_concurrent_requests_build_exactly_once(world: _World) -> None:
    cache = DatasetCache()
    world.gate = asyncio.Event()
    tasks = [asyncio.create_task(cache.get(ENGINE, max_runs=5)) for _ in range(40)]
    await world.started.wait()
    await asyncio.sleep(0.02)
    world.gate.set()
    results = await asyncio.gather(*tasks)
    assert world.builds == cache.builds == 1
    assert all(r is results[0] for r in results)


async def test_a_cancelled_holder_releases_the_lock_and_the_next_builds(
    world: _World,
) -> None:
    cache = DatasetCache()
    world.gate = asyncio.Event()
    first = asyncio.create_task(cache.get(ENGINE, max_runs=5))
    await world.started.wait()
    second = asyncio.create_task(cache.get(ENGINE, max_runs=5))
    await asyncio.sleep(0.01)
    first.cancel()  # a client that hung up mid-build
    with pytest.raises(asyncio.CancelledError):
        await first
    world.gate.set()
    got = await asyncio.wait_for(second, 2.0)
    assert got.dataset is world.data
    assert cache.builds == 1  # the cancelled build cached nothing
    again = await asyncio.wait_for(cache.get(ENGINE, max_runs=5), 1.0)
    assert again is got and world.builds == 2


async def test_data_that_lands_during_a_build_is_never_served_stale(
    world: _World,
) -> None:
    cache = DatasetCache()
    world.during_build = "w2"  # the watermark moves while we build for w1
    first = await cache.get(ENGINE, max_runs=5)
    world.during_build = None
    second = await cache.get(ENGINE, max_runs=5)  # key was read BEFORE the build
    assert world.builds == 2 and second is not first
    third = await cache.get(ENGINE, max_runs=5)
    assert third is second and world.builds == 2


class _P:
    user_id = "usr_t"
    name = "t"

    def __init__(self, admin: bool) -> None:
        self.is_admin = admin


async def test_a_non_admin_gets_nothing_from_a_warm_cache(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    principal: list[Any] = [_P(True)]

    async def authenticate(request: Any) -> Any:
        return principal[0]

    monkeypatch.setattr(auth, "authenticate", authenticate)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ml_routes.ml_router)
    setattr(app.state, ml_routes.ML_STATE_KEY, ml_routes.MlDatasetApi(object(), 5))  # type: ignore[arg-type]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        ok = await asyncio.gather(
            client.get("/v1/planner/ml/dataset?format=summary"),
            client.get("/v1/planner/ml/dataset?format=parquet"),
            client.get("/v1/planner/ml/dataset"),
        )
        assert [r.status_code for r in ok] == [200, 200, 200]
        assert world.builds == 1  # one build served every format
        reads, builds = world.watermark_reads, world.builds
        for p, code in ((None, 401), (_P(False), 403)):
            principal[0] = p
            for q in ("", "?format=summary", "?format=parquet"):
                r = await client.get("/v1/planner/ml/dataset" + q)
                assert r.status_code == code, (p, q, r.status_code)
                assert world.data.digest not in r.text
                assert "digest" not in r.text.lower() or r.status_code != 200
        # the rejected calls neither read the watermark nor touched the cache
        assert (world.watermark_reads, world.builds) == (reads, builds)
