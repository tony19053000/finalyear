"""V0.9 G5c item 0.7 (D252.4): the orphan backstop scan is throttled.

``BenchStore._cancel_orphans`` scans (``orphan_jobs``) only when due: the first
time, while the last whole scan still found orphans, after
:meth:`~meshforge.controller.bench.store.BenchStore.expect_orphans` (a cancel,
a skipped run, a failed link), or ``orphan_scan_s`` after the last whole scan.
``orphan_jobs`` is faked; the SQL is tested on real PostgreSQL elsewhere.
"""

from __future__ import annotations

import types
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy.ext.asyncio import create_async_engine

from meshforge.controller.bench import store as store_module
from meshforge.controller.bench.store import DEFAULT_ORPHAN_SCAN_S, BenchStore
from meshforge.controller.settings import BenchSettings

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _Rig:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/x")
        self.store = BenchStore(engine)
        self.now = 1000.0
        monkeypatch.setattr(
            store_module, "time", types.SimpleNamespace(monotonic=lambda: self.now)
        )
        self.scans: list[str | None] = []
        self.found: list[str] = []
        self.cancelled: list[str] = []

        async def orphan_jobs(campaign_id: str | None = None) -> list[str]:
            self.scans.append(campaign_id)
            return list(self.found)

        async def cancel(job_id: str) -> None:
            self.cancelled.append(job_id)

        self.store.orphan_jobs = orphan_jobs  # type: ignore[method-assign]
        self.store.cancel_job = cancel

    async def scan(self, campaign_id: str | None = None) -> int:
        before = len(self.scans)
        await self.store._cancel_orphans(campaign_id)
        return len(self.scans) - before


async def test_the_default_is_thirty_seconds() -> None:
    assert DEFAULT_ORPHAN_SCAN_S == 30.0 == BenchSettings().orphan_scan_s


async def test_first_scan_then_throttled_until_the_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(monkeypatch)
    assert await rig.scan() == 1  # never scanned
    for _ in range(10):
        rig.now += 2.0  # runner ticks
        if rig.now - 1000.0 < 30.0:
            assert await rig.scan() == 0
    rig.now = 1030.0
    assert await rig.scan() == 1  # orphan_scan_s passed


async def test_expect_orphans_forces_the_next_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(monkeypatch)
    await rig.scan()
    rig.now += 1.0
    assert await rig.scan() == 0
    rig.store.expect_orphans()
    assert await rig.scan() == 1
    assert await rig.scan() == 0  # consumed


async def test_found_orphans_keep_the_scan_running_until_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(monkeypatch)
    rig.found = ["job_1"]
    await rig.scan()
    assert rig.cancelled == ["job_1"]
    rig.now += 1.0
    assert await rig.scan() == 1  # still there: scan again (re-ask throttled)
    rig.found = []
    rig.now += 1.0
    assert await rig.scan() == 1  # clean now
    rig.now += 1.0
    assert await rig.scan() == 0  # throttled again


async def test_a_scoped_scan_never_says_all_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = _Rig(monkeypatch)
    rig.found = ["job_1"]
    await rig.scan()  # whole: found one
    rig.found = []
    rig.now += 1.0
    assert await rig.scan("camp_x") == 1  # scoped: clean for camp_x only ...
    rig.now += 1.0
    assert await rig.scan() == 1  # ... so the whole scan still runs


async def test_mark_skipped_raises_the_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _Rig(monkeypatch)
    await rig.scan()
    calls: list[Any] = []

    class _Session:
        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, *_a: Any) -> None:
            return None

        def begin(self) -> Any:
            return self

        async def execute(self, *_a: Any, **_k: Any) -> Any:
            calls.append(1)
            return types.SimpleNamespace(rowcount=1)

    rig.store._sessions = lambda: _Session()  # type: ignore[assignment]
    assert await rig.store.mark_skipped("brun_x", "gone")
    assert calls and await rig.scan() == 1


async def test_zero_scans_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = _Rig(monkeypatch)
    rig.store.orphan_scan_s = 0.0
    assert [await rig.scan() for _ in range(3)] == [1, 1, 1]
