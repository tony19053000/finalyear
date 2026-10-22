"""V0.15 G2 item 0a (D355): the dataset route's cache - single flight, reuse
while the watermark is unchanged, rebuild when it changes, nothing cached on
a failed build. The database side (real watermark, real build) is covered by
``tests/component/controller/test_v015_g2_ml_compare_pg.py``."""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sqlalchemy")

from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.ml import dataset as ds
from meshforge.controller.ml import export, loader
from meshforge.controller.ml.cache import DatasetCache

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

ENGINE = cast(AsyncEngine, object())  # never used: the reads are faked


class _Fakes:
    def __init__(self) -> None:
        self.mark: object = "w1"
        self.builds = 0
        self.watermarks = 0
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()
        self.fail = False

    async def watermark(self, engine: Any, *, max_runs: int) -> object:
        self.watermarks += 1
        return (self.mark, max_runs)

    async def build(self, engine: Any, *, max_runs: int) -> export.BuiltDataset:
        self.builds += 1
        self.started.set()
        if self.gate is not None:
            await self.gate.wait()
        if self.fail:
            raise loader.MlLoadError("db down")
        return export.BuiltDataset(
            dataset=ds.build([]), max_runs=max_runs, truncated=False
        )


@pytest.fixture
def fakes(monkeypatch: pytest.MonkeyPatch) -> _Fakes:
    f = _Fakes()
    monkeypatch.setattr(loader, "read_watermark", f.watermark)
    monkeypatch.setattr(export, "build_dataset", f.build)
    return f


async def test_two_concurrent_requests_build_once(fakes: _Fakes) -> None:
    cache = DatasetCache()
    fakes.gate = asyncio.Event()
    first = asyncio.create_task(cache.get(ENGINE, max_runs=5))
    await fakes.started.wait()
    second = asyncio.create_task(cache.get(ENGINE, max_runs=5))
    await asyncio.sleep(0.01)
    assert fakes.builds == 1  # the second waits on the lock, it does not build
    fakes.gate.set()
    a, b = await asyncio.gather(first, second)
    assert a is b
    assert fakes.builds == cache.builds == 1
    assert fakes.watermarks == 2  # the waiter re-reads the watermark, then reuses


async def test_unchanged_watermark_reuses_changed_rebuilds(fakes: _Fakes) -> None:
    cache = DatasetCache()
    a = await cache.get(ENGINE, max_runs=5)
    b = await cache.get(ENGINE, max_runs=5)
    assert a is b and fakes.builds == 1
    fakes.mark = "w2"  # a new matched run
    c = await cache.get(ENGINE, max_runs=5)
    assert c is not a and fakes.builds == 2
    d = await cache.get(ENGINE, max_runs=6)  # another read cap is another key
    assert d is not c and fakes.builds == 3


async def test_a_failed_build_caches_nothing(fakes: _Fakes) -> None:
    cache = DatasetCache()
    fakes.fail = True
    with pytest.raises(loader.MlLoadError):
        await cache.get(ENGINE, max_runs=5)
    fakes.fail = False
    await cache.get(ENGINE, max_runs=5)
    assert fakes.builds == 2 and cache.builds == 1


async def test_a_failed_watermark_read_propagates_and_releases_the_lock(
    fakes: _Fakes, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache = DatasetCache()

    async def down(engine: Any, *, max_runs: int) -> object:
        raise loader.MlLoadError("db down")

    monkeypatch.setattr(loader, "read_watermark", down)
    with pytest.raises(loader.MlLoadError):
        await cache.get(ENGINE, max_runs=5)
    monkeypatch.setattr(loader, "read_watermark", fakes.watermark)
    await asyncio.wait_for(cache.get(ENGINE, max_runs=5), 1.0)
    assert fakes.builds == 1


async def test_counts_carry_node_failures() -> None:
    data = ds.build([])
    assert data.counts["node_failures"] == 0
    builder = ds.Builder()
    builder.node_failures = 7
    assert builder.finish().counts["node_failures"] == 7
