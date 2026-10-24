# ruff: noqa: ASYNC240
"""V0.15 G6b tester: EvalService start-then-poll, adversarial (fake store, no DB).

Derived from brief section 5h D378: one evaluation per controller (concurrent
starts -> exactly one wins), never stuck ``running``, stop kills the fold
child, bounded memories (64) of stored / unwritten outcomes.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from meshforge.controller.planner_eval import service as svc
from meshforge.controller.planner_eval.service import (
    EvalBusyError,
    EvalService,
)
from meshforge.controller.planner_eval.store import (
    EvalReportRow,
    EvalStoreError,
    failed_state,
)

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

NOW = dt.datetime(2026, 10, 24, tzinfo=dt.UTC)
RUNNING = {"run_state": {"status": "running"}}


def _row(report_id: int, report: dict[str, Any] | None = None) -> EvalReportRow:
    return EvalReportRow(
        id=report_id,
        created_at=NOW,
        campaign_ids=["c"],
        holdout=None,
        leaked=False,
        report=RUNNING if report is None else report,
    )


class FakeStore:
    """In-memory EvalReportStore with the same running-only replace rule."""

    def __init__(self) -> None:
        self.rows: dict[int, EvalReportRow] = {}
        self.next_id = 1
        self.start_gate: asyncio.Event | None = None
        self.starts = 0
        self.fail_writes = False
        self.fail_delay_s = 0.0

    async def start(self, ids: list[str], *, holdout: Any, leaked: bool) -> Any:
        self.starts += 1
        if self.start_gate is not None:
            await self.start_gate.wait()
        row = _row(self.next_id)
        self.next_id += 1
        self.rows[row.id] = row
        return row

    async def finish(self, report_id: int, report: dict[str, Any]) -> Any:
        if self.fail_writes:
            raise EvalStoreError("down", details={"cause": "OperationalError"})
        cur = self.rows[report_id]
        if cur.status != "running":
            return None
        self.rows[report_id] = _row(report_id, dict(report))
        return self.rows[report_id]

    async def fail(self, report_id: int, reason: str, detail: str | None = None) -> Any:
        if self.fail_delay_s:
            await asyncio.sleep(self.fail_delay_s)
        if self.fail_writes:
            raise EvalStoreError("down", details={"cause": "OperationalError"})
        cur = self.rows[report_id]
        if cur.status != "running":
            return None
        self.rows[report_id] = _row(report_id, failed_state(reason, detail))
        return self.rows[report_id]


REPORT = {
    "campaign_ids": ["c"],
    "holdout": None,
    "leaked": False,
    "counts": {
        "groups": 0,
        "evaluated_groups": 0,
        "records_used": 0,
    },
    "verdicts": [],
    "ml": {"status": "ml_unavailable", "verdicts": []},
    "latency": {"decisions": 0},
}


class Svc(EvalService):
    """EvalService whose evaluation is a controllable coroutine."""

    def __init__(self, store: FakeStore, work: Any) -> None:
        super().__init__(
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            max_rows=1,
        )
        self.work = work

    async def report(self, campaign_ids: Any, **kw: Any) -> dict[str, Any]:
        return await self.work()


@pytest.fixture(autouse=True)
def _campaigns_exist(monkeypatch: pytest.MonkeyPatch) -> None:
    async def none_missing(_e: Any, ids: Any) -> list[str]:
        return []

    monkeypatch.setattr(svc, "missing_campaigns", none_missing)


def _gated() -> tuple[asyncio.Event, Any]:
    release = asyncio.Event()

    async def work() -> dict[str, Any]:
        await release.wait()
        return dict(REPORT)

    return release, work


async def test_many_concurrent_starts_exactly_one_wins() -> None:
    store = FakeStore()
    store.start_gate = asyncio.Event()
    release, work = _gated()
    s = Svc(store, work)
    tasks = [asyncio.create_task(s.start(["c"])) for _ in range(25)]
    await asyncio.sleep(0.05)
    store.start_gate.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    ok = [r for r in results if isinstance(r, EvalReportRow)]
    busy = [r for r in results if isinstance(r, EvalBusyError)]
    assert len(ok) == 1 and len(busy) == 24, results
    assert store.starts == 1
    release.set()
    await s.wait()
    assert store.rows[ok[0].id].status == "done"
    assert not s.busy


async def test_a_failing_store_start_releases_the_busy_flag() -> None:
    store = FakeStore()
    _release, work = _gated()
    s = Svc(store, work)

    async def boom(*a: Any, **k: Any) -> Any:
        raise EvalStoreError("down", details={"cause": "OperationalError"})

    store.start = boom  # type: ignore[method-assign]
    with pytest.raises(EvalStoreError):
        await s.start(["c"])
    assert not s.busy and s.running_id is None


async def test_start_cancelled_while_storing_releases_the_busy_flag() -> None:
    store = FakeStore()
    store.start_gate = asyncio.Event()
    _release, work = _gated()
    s = Svc(store, work)
    t = asyncio.create_task(s.start(["c"]))
    await asyncio.sleep(0.02)
    assert s.busy
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    assert not s.busy


async def test_written_memory_is_bounded_and_newest_stay_remembered() -> None:
    s = Svc(FakeStore(), lambda: None)
    for rid in range(1, 201):
        s._stored(rid)
    assert len(s._written) == 64
    assert list(s._written) == list(range(137, 201))
    # newest: a stale read is answered running; oldest evicted: failed
    assert s.status_of(_row(200))[:2] == ("running", None)
    assert s.status_of(_row(1))[:2] == ("failed", "controller_restart")


async def test_unwritten_memory_is_bounded_and_keeps_the_reason() -> None:
    store = FakeStore()
    store.fail_writes = True

    async def crash() -> dict[str, Any]:
        raise RuntimeError("bug")

    s = Svc(store, crash)
    for _ in range(80):
        row = await s.start(["c"])
        await s.wait()
        assert not s.busy
        # DB still says running; this process answers the real outcome
        assert s.status_of(row)[:2] == ("failed", "crashed")
    assert len(s._unwritten) == 64
    assert s.status_of(store.rows[1])[:2] == ("failed", "controller_restart")
    assert s.status_of(store.rows[80])[:2] == ("failed", "crashed")


async def test_a_swept_row_drops_a_late_result_and_stays_failed() -> None:
    store = FakeStore()
    release, work = _gated()
    s = Svc(store, work)
    row = await s.start(["c"])
    # another process's sweep fails the row while the evaluation runs
    store.rows[row.id] = _row(row.id, failed_state("controller_restart", "x"))
    release.set()
    await s.wait()
    assert store.rows[row.id].status == "failed"  # not overwritten with a report
    assert not s.busy
    assert s.status_of(store.rows[row.id])[:2] == ("failed", "controller_restart")


async def test_a_second_start_after_the_first_ends_is_accepted() -> None:
    store = FakeStore()
    release, work = _gated()
    release.set()
    s = Svc(store, work)
    a = await s.start(["c"])
    await s.wait()
    b = await s.start(["c"])
    await s.wait()
    assert a.id != b.id
    assert [store.rows[i].status for i in (a.id, b.id)] == ["done", "done"]


async def test_request_stop_idle_returns_none_and_is_idempotent() -> None:
    s = Svc(FakeStore(), lambda: None)
    assert s.request_stop() is None
    assert s.request_stop() is None


async def test_request_stop_marks_controller_restart_and_frees_busy() -> None:
    store = FakeStore()
    _release, work = _gated()
    s = Svc(store, work)
    row = await s.start(["c"])
    await asyncio.sleep(0.01)
    task = s.request_stop()
    assert task is not None
    await asyncio.gather(task, return_exceptions=True)
    assert store.rows[row.id].status == "failed"
    assert store.rows[row.id].reason == "controller_restart"
    assert not s.busy
    assert s.request_stop() is None  # second stop: nothing runs


async def test_request_stop_is_bounded_when_the_database_hangs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(svc, "_FAIL_WRITE_S", 0.2)
    store = FakeStore()
    store.fail_delay_s = 30.0
    _release, work = _gated()
    s = Svc(store, work)
    await s.start(["c"])
    await asyncio.sleep(0.01)
    task = s.request_stop()
    assert task is not None
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)
    assert loop.time() - t0 < 2.0


async def test_request_stop_with_a_dead_database_does_not_raise() -> None:
    store = FakeStore()
    store.fail_writes = True
    _release, work = _gated()
    s = Svc(store, work)
    await s.start(["c"])
    await asyncio.sleep(0.01)
    task = s.request_stop()
    assert task is not None
    results = await asyncio.gather(task, return_exceptions=True)
    assert isinstance(results[0], asyncio.CancelledError)


@pytest.mark.xfail(
    strict=True,
    reason="defect T1: while ANY start is in flight (_starting), status_of "
    "answers every orphaned running row 'running' (a row left by a dead "
    "controller is briefly 'running' again)",
)
async def test_an_orphan_row_is_never_answered_running_during_another_start() -> None:
    store = FakeStore()
    store.start_gate = asyncio.Event()
    _release, work = _gated()
    s = Svc(store, work)
    orphan = _row(999)  # left running by a killed controller
    t = asyncio.create_task(s.start(["c"]))
    await asyncio.sleep(0.02)
    assert s._starting
    answer = s.status_of(orphan)[:2]
    store.start_gate.set()
    await t
    assert answer == ("failed", "controller_restart")


@pytest.mark.skipif(os.name != "posix", reason="pid liveness via /proc or kill(0)")
async def test_stop_kills_the_real_fold_child_process() -> None:
    """request_stop while a fold child runs: the OS process is gone and reaped."""
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    from mf_ml_synthetic import synthetic_rows

    from meshforge.controller.ml.child import run_child
    from meshforge.controller.ml.dataset import parquet_bytes, sort_key
    from meshforge.controller.ml.export import BuiltDataset
    from meshforge.controller.planner_eval.ml_inputs import FoldMlInputs
    from meshforge.controller.settings import PlannerSettings

    pids: list[int] = []
    started = asyncio.Event()

    async def sleeper(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        code = (
            "import os,sys,time;"
            "open(sys.argv[1],'w').write(str(os.getpid()));time.sleep(600)"
        )
        pidfile = f"{os.environ.get('TMPDIR', '/tmp')}/mf-g6b-{os.getpid()}-{id(argv)}"
        task = asyncio.create_task(
            run_child([sys.executable, "-c", code, pidfile], timeout_s)
        )
        for _ in range(200):
            await asyncio.sleep(0.05)
            if os.path.exists(pidfile) and os.path.getsize(pidfile) > 0:
                pids.append(int(Path(pidfile).read_text()))
                os.unlink(pidfile)
                started.set()
                break
        return await task

    class _M:
        mode = "auto"

    class _L:
        pass

    rows = sorted(
        synthetic_rows("signal", families=2, groups_per_family=2, runs_per_group=4),
        key=sort_key,
    )

    class _D:
        pass

    d = _D()
    d.rows = tuple(rows)  # type: ignore[attr-defined]
    d.parquet = parquet_bytes(rows)  # type: ignore[attr-defined]

    async def build() -> BuiltDataset:
        return BuiltDataset(d, 10, False)  # type: ignore[arg-type]

    fold = FoldMlInputs(
        PlannerSettings(),
        _M(),  # type: ignore[arg-type]
        _L(),  # type: ignore[arg-type]
        build,
        child=sleeper,
        missing=list,
    )

    async def work() -> dict[str, Any]:
        await fold.prepare({"fam00": frozenset()}, allow_leak=False)
        return dict(REPORT)

    store = FakeStore()
    s = Svc(store, work)
    row = await s.start(["c"])
    await asyncio.wait_for(started.wait(), 30)
    pid = pids[0]
    os.kill(pid, 0)  # alive
    task = s.request_stop()
    assert task is not None
    await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 30)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # gone AND reaped (no zombie: kill(0) on a zombie succeeds)
    assert store.rows[row.id].reason == "controller_restart"
