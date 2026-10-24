"""V0.15 G6b tester round 2: fix-round-1 attacks (T1 orphan rows, N1 thread
cancel through the service, stale-read memory bound, deterministic until)."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import threading
import time
from typing import Any

import pytest
from test_v015_g6b_tester_service import REPORT, FakeStore, Svc, _gated, _row

from meshforge.controller.planner_eval import service as svc
from meshforge.controller.planner_eval.ml_inputs import dataset_until
from meshforge.controller.planner_eval.service import EvalService

pytestmark = [pytest.mark.unit]

EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)


@pytest.fixture(autouse=True)
def _campaigns_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    async def none_missing(_e: Any, ids: Any) -> list[str]:
        return []

    monkeypatch.setattr(svc, "missing_campaigns", none_missing)


# ---- T1: orphans stay failed whatever the start state ------------------------


@pytest.mark.asyncio
async def test_orphans_of_any_id_are_failed_while_a_start_is_in_flight() -> None:
    store = FakeStore()
    store.start_gate = asyncio.Event()
    _release, work = _gated()
    s = Svc(store, work)
    t = asyncio.create_task(s.start(["c"]))
    await asyncio.sleep(0.02)
    assert s._starting
    for orphan in (0, 1, 5, 999, 10**12):
        assert s.status_of(_row(orphan))[:2] == ("failed", "controller_restart")
    store.start_gate.set()
    row = await t
    assert s.status_of(row)[:2] == ("running", None)
    # an orphan older/newer than the live row, while the eval truly runs
    for orphan in (row.id + 1, row.id + 100):
        assert s.status_of(_row(orphan))[0] == "failed"
    _release.set()
    await s.wait()


@pytest.mark.asyncio
async def test_a_finished_row_is_answered_as_stored_even_if_starting() -> None:
    s = Svc(FakeStore(), lambda: None)
    s._starting = True
    done = _row(3, dict(REPORT))
    assert s.status_of(done)[0] == "done"


# ---- N1: a stopped service ends the worker thread, no stuck rows -------------


@pytest.mark.asyncio
async def test_stop_mid_eval_ends_the_real_thread_and_leaves_no_running_row() -> None:
    store = FakeStore()
    entered = threading.Event()
    ended = threading.Event()
    seen: list[str] = []

    def blocking(cancel: threading.Event) -> dict[str, Any]:
        entered.set()
        deadline = time.monotonic() + 20
        while not cancel.is_set():
            if time.monotonic() > deadline:
                seen.append("never cancelled")
                break
            time.sleep(0.002)
        ended.set()
        return dict(REPORT)

    class Stoppable(Svc):
        async def report(self, campaign_ids: Any, **kw: Any) -> dict[str, Any]:
            return await svc._stoppable(blocking)

    s = Stoppable(store, lambda: None)
    row = await s.start(["c"])
    assert await asyncio.to_thread(entered.wait, 5)
    task = s.request_stop()
    assert task is not None
    await asyncio.gather(task, return_exceptions=True)
    assert await asyncio.to_thread(ended.wait, 5), "worker thread leaked"
    assert seen == []
    assert store.rows[row.id].status == "failed"
    assert store.rows[row.id].reason == "controller_restart"
    assert not s.busy
    # repeated stop is a no-op, and a new start works after
    assert s.request_stop() is None
    # no row anywhere is left running
    assert all(r.status != "running" for r in store.rows.values())


@pytest.mark.asyncio
async def test_stop_many_times_leaks_no_threads() -> None:
    ends: list[threading.Event] = []
    for _ in range(15):
        store = FakeStore()
        entered = threading.Event()

        ended = threading.Event()
        ends.append(ended)

        def blocking(
            cancel: threading.Event,
            e: threading.Event = entered,
            d: threading.Event = ended,
        ) -> int:
            e.set()
            stop = time.monotonic() + 3  # bounded, so a missing flag fails, not hangs
            while not cancel.is_set() and time.monotonic() < stop:
                time.sleep(0.001)
            if cancel.is_set():
                d.set()
            return 0

        class S(Svc):
            async def report(self, campaign_ids: Any, **kw: Any) -> dict[str, Any]:
                await svc._stoppable(blocking)
                return dict(REPORT)

        s = S(store, lambda: None)
        await s.start(["c"])
        assert await asyncio.to_thread(entered.wait, 5)
        task = s.request_stop()
        assert task is not None
        await asyncio.gather(task, return_exceptions=True)
    for ended in ends:  # every worker saw its flag (pool threads stay idle)
        assert await asyncio.to_thread(ended.wait, 1), "worker never saw its flag"


@pytest.mark.asyncio
async def test_thread_error_other_than_cancel_still_ends_failed_not_running() -> None:
    store = FakeStore()

    def boom(cancel: threading.Event) -> int:
        raise ValueError("bug")

    class S(Svc):
        async def report(self, campaign_ids: Any, **kw: Any) -> dict[str, Any]:
            await svc._stoppable(boom)
            return {}

    s = S(store, lambda: None)
    row = await s.start(["c"])
    await s.wait()
    assert store.rows[row.id].status == "failed"
    assert store.rows[row.id].reason == "crashed"


# ---- stale-read memory bound over a long life --------------------------------


@pytest.mark.asyncio
async def test_memories_stay_bounded_over_many_runs_and_failures() -> None:
    store = FakeStore()
    release, work = _gated()
    release.set()
    s: EvalService = Svc(store, work)
    for i in range(300):
        store.fail_writes = i % 2 == 1  # alternate stored / unwritten outcomes
        await s.start(["c"])
        await s.wait()
    assert len(s._written) <= 64
    assert len(s._unwritten) <= 64
    assert s._running is not None  # only the latest task is referenced


# ---- deterministic until ------------------------------------------------------


def _t(hour: int, tz: dt.tzinfo = dt.UTC, day: int = 1) -> dt.datetime:
    return dt.datetime(2026, 10, day, hour, tzinfo=tz)


def test_until_is_order_independent_and_tie_stable() -> None:
    stamps = [_t(h) for h in range(1, 20)] + [_t(19)] * 5  # ties at the newest
    rows = [{"finished_at": s, "x": i} for i, s in enumerate(stamps)]
    expect = _t(19)
    rng = random.Random(7)  # noqa: S311 - a test shuffle
    for _ in range(50):
        rng.shuffle(rows)
        assert dataset_until(rows) == expect
        assert dataset_until(iter(rows)) == expect  # a one-shot iterable too


def test_until_compares_instants_across_time_zones_not_wall_clock() -> None:
    plus9 = dt.timezone(dt.timedelta(hours=9))
    minus8 = dt.timezone(dt.timedelta(hours=-8))
    # 23:00+09 is 14:00Z; 08:00-08 is 16:00Z (newer though the number is smaller)
    rows = [{"finished_at": _t(23, plus9)}, {"finished_at": _t(8, minus8)}]
    assert dataset_until(rows) == _t(16)
    assert dataset_until(rows).utcoffset() == dt.timedelta(0)
    # DST-less fixed offsets across a day boundary
    rows = [{"finished_at": _t(1, plus9, day=2)}, {"finished_at": _t(23, minus8)}]
    assert dataset_until(rows) == _t(23, minus8).astimezone(dt.UTC)


def test_until_ignores_missing_and_junk_stamps_and_empty_is_epoch() -> None:
    assert dataset_until([]) == EPOCH
    assert dataset_until([{}, {"finished_at": None}, {"finished_at": 5}]) == EPOCH
    rows = [{"finished_at": None}, {"finished_at": _t(3)}, {"x": 1}]
    assert dataset_until(rows) == _t(3)


def test_until_with_extreme_stamps_does_not_overflow() -> None:
    rows = [
        {"finished_at": dt.datetime.max.replace(tzinfo=dt.UTC)},
        {"finished_at": dt.datetime.min.replace(tzinfo=dt.UTC)},
    ]
    assert dataset_until(rows) == dt.datetime.max.replace(tzinfo=dt.UTC)


def test_until_pre_epoch_data_is_not_replaced_by_the_epoch() -> None:
    old = dt.datetime(1960, 1, 1, tzinfo=dt.UTC)
    assert dataset_until([{"finished_at": old}]) == old  # real data wins


def test_a_newer_run_moves_until_a_unchanged_set_does_not() -> None:
    rows = [{"finished_at": _t(h)} for h in (1, 2, 3)]
    first = dataset_until(rows)
    assert dataset_until(list(rows)) == first
    assert dataset_until([*rows, {"finished_at": _t(2)}]) == first  # older run
    assert dataset_until([*rows, {"finished_at": _t(9)}]) == _t(9) > first


# ---- CLI Retry-After parsing (N2), hostile headers ---------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5", 5.0),
        (" 7 ", 7.0),
        ("0", 0.0),
        ("-3", 0.0),
        ("1e9", 60.0),
        ("99999999999999999999", 60.0),
        ("nan", None),
        ("inf", None),
        ("-inf", None),
        ("", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),  # HTTP-date: not honoured
        ("5 seconds", None),
    ],
)
def test_retry_after_parsing_is_clamped_and_never_nan(
    raw: str, expected: float | None
) -> None:
    import httpx

    from meshforge.cli.client import _retry_after

    response = httpx.Response(503, headers={"Retry-After": raw})
    assert _retry_after(response) == expected


def test_retry_after_absent_is_none() -> None:
    import httpx

    from meshforge.cli.client import _retry_after

    assert _retry_after(httpx.Response(503)) is None
