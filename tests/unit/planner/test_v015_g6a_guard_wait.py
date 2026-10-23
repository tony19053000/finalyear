"""V0.15 G6a (G5 debt): the retrain logs how long it waited for the
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


def test_a_queued_retrain_logs_its_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    retrainer = _retrainer(clock, monkeypatch)

    async def main() -> None:
        held = asyncio.Event()
        release = asyncio.Event()

        async def other_child() -> None:
            async with TRAINING_GUARD.held():
                held.set()
                await release.wait()
                clock.now += 42.5  # the other child trained this long

        other = asyncio.create_task(other_child())
        await held.wait()
        retrain = asyncio.create_task(retrainer._retrain("scheduled", NOW))
        await asyncio.sleep(0.05)  # the retrain is now queued on the guard
        release.set()
        await asyncio.gather(other, retrain)

    with capture_logs() as logs:
        asyncio.run(main())
    (wait,) = _waits(logs)
    assert wait["waited_s"] == pytest.approx(42.5)
    assert wait["trigger"] == "scheduled"
    assert any(
        e["event"] == "controller.ml.retrain_waits_for_training_child" for e in logs
    )


def test_a_free_guard_logs_no_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    retrainer = _retrainer(_Clock(), monkeypatch)
    with capture_logs() as logs:
        result = asyncio.run(retrainer._retrain("manual", NOW))
    assert result.error == "child_failed"
    assert _waits(logs) == []
