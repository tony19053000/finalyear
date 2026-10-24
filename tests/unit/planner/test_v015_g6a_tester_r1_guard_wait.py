"""V0.15 G6a tester r1 (guard-wait edge cases): the retrain logs
controller-wide training-child guard.

* a retrain that queued behind another training child logs
  ``controller.ml.retrain_guard_acquired`` with ``waited_s`` = the time it
  waited (here: the other child's hold);
* a retrain that found the guard free and got it at once logs nothing extra.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")

from structlog.testing import capture_logs

from meshforge.controller import ml_retrain_task
from meshforge.controller.ml.child import TRAINING_GUARD
from meshforge.controller.ml.export import BuiltDataset
from meshforge.controller.ml_retrain_task import MlRetrainer
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)


class _Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _retrainer(clock: _Clock, monkeypatch: pytest.MonkeyPatch) -> MlRetrainer:
    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        return 1, "fake child"

    retrainer = MlRetrainer(
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        PlannerSettings(),
        child=child,
        monotonic=clock,
        set_loader=object(),  # type: ignore[arg-type]
    )

    async def key() -> dict[str, Any]:
        return {}

    async def unchanged(_key: Any) -> bool:
        return False

    async def reference(_dir: Path) -> None:
        return None

    async def build(_engine: Any, *, max_runs: int) -> BuiltDataset:
        class _D:
            rows: tuple[Any, ...] = ()
            parquet = b""

        return BuiltDataset(_D(), 0, False)  # type: ignore[arg-type]

    monkeypatch.setattr(retrainer, "_key", key)
    monkeypatch.setattr(retrainer, "_unchanged", unchanged)
    monkeypatch.setattr(retrainer, "_write_reference", reference)
    monkeypatch.setattr(ml_retrain_task.export, "build_dataset", build)
    return retrainer


def _waits(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in logs if e["event"] == "controller.ml.retrain_guard_acquired"]


def test_queued_but_instant_still_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    retrainer = _retrainer(_Clock(), monkeypatch)

    async def main() -> None:
        held, release = asyncio.Event(), asyncio.Event()

        async def other() -> None:
            async with TRAINING_GUARD.held():
                held.set()
                await release.wait()

        o = asyncio.create_task(other())
        await held.wait()
        r = asyncio.create_task(retrainer._retrain("manual", NOW))
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(o, r)

    with capture_logs() as logs:
        asyncio.run(main())
    (w,) = _waits(logs)
    assert w["waited_s"] == 0.0 and w["trigger"] == "manual"


def test_a_free_guard_but_a_slow_acquire_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    retrainer = _retrainer(clock, monkeypatch)
    ticks = iter([100.0, 101.5])  # start, after acquire
    monkeypatch.setattr(retrainer, "_monotonic", lambda: next(ticks))
    with capture_logs() as logs:
        asyncio.run(retrainer._retrain("scheduled", NOW))
    (w,) = _waits(logs)
    assert w["waited_s"] == pytest.approx(1.5)


def test_a_cancelled_waiter_logs_nothing_and_frees_the_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retrainer = _retrainer(_Clock(), monkeypatch)

    async def main() -> bool:
        held, release = asyncio.Event(), asyncio.Event()

        async def other() -> None:
            async with TRAINING_GUARD.held():
                held.set()
                await release.wait()

        o = asyncio.create_task(other())
        await held.wait()
        r = asyncio.create_task(retrainer._retrain("scheduled", NOW))
        await asyncio.sleep(0.02)
        r.cancel()
        with pytest.raises(asyncio.CancelledError):
            await r
        release.set()
        await o
        return TRAINING_GUARD.busy()

    with capture_logs() as logs:
        assert asyncio.run(main()) is False
    assert _waits(logs) == []


def test_two_queued_retrains_each_log_their_own_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    retrainer = _retrainer(clock, monkeypatch)

    async def main() -> None:
        held, release = asyncio.Event(), asyncio.Event()

        async def other() -> None:
            async with TRAINING_GUARD.held():
                held.set()
                await release.wait()
                clock.now += 10

        o = asyncio.create_task(other())
        await held.wait()
        a = asyncio.create_task(retrainer._retrain("scheduled", NOW))
        b = asyncio.create_task(retrainer._retrain("manual", NOW))
        await asyncio.sleep(0.02)
        release.set()
        await asyncio.gather(o, a, b)

    with capture_logs() as logs:
        asyncio.run(main())
    ws = _waits(logs)
    assert sorted(w["trigger"] for w in ws) == ["manual", "scheduled"]
    assert all(w["waited_s"] >= 10 for w in ws)
