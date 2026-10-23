"""V0.15 G6b: the G5 debt items and the D378 fold-reason fix.

* ``TrainingGuard.busy()`` outside an event loop never raises: it says
  whether a child holds the guard on any loop (a worker thread asking while
  the controller's loop trains says True), and it never creates a lock;
* the fold child's limit is the evaluation budget left + epsilon (or the
  retrain timeout when that ends first), so when both would expire together
  the fold reads ``eval_budget_exhausted``, never ``timeout``;
* ``_point`` with ``|y_hat| > MAX_Y_HAT``: ``used`` stays true, ``ml_s`` is
  null (the value is not a prediction); exactly ``MAX_Y_HAT`` still counts;
* calibration versions beyond the per-request cap are counted, with the
  points whose ``calibrated_s`` is null because of it.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import math
import threading
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_ml_fixtures import chosen, stored, whole_run

from meshforge.controller.ml.child import TrainingGuard
from meshforge.controller.ml.export import BuiltDataset
from meshforge.controller.ml.predictions import (
    MAX_CALIBRATION_VERSIONS,
    PredictionPoint,
    calibration_skips,
    points_of,
)
from meshforge.controller.planner.predictor import MAX_Y_HAT
from meshforge.controller.planner_eval.ml_inputs import (
    FOLD_BUDGET_EPSILON_S,
    FoldMlInputs,
    fold_child_timeout,
)
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)
PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}


# ------------------------------------------------------------ TrainingGuard


def test_busy_outside_a_loop_never_raises_and_creates_no_lock() -> None:
    guard = TrainingGuard()
    assert guard.busy() is False
    assert len(guard._locks) == 0  # noqa: SLF001

    async def inside() -> bool:
        return guard.busy()

    assert asyncio.run(inside()) is False
    assert len(guard._locks) == 0  # noqa: SLF001 - a read creates nothing


def test_busy_from_another_thread_sees_a_child_held_on_the_loop() -> None:
    guard = TrainingGuard()
    holding = threading.Event()
    release = threading.Event()

    async def hold() -> None:
        async with guard.held():
            holding.set()
            await asyncio.to_thread(release.wait, 10)

    thread = threading.Thread(target=lambda: asyncio.run(hold()))
    thread.start()
    try:
        assert holding.wait(10)
        assert guard.busy() is True  # no running loop in this thread
    finally:
        release.set()
        thread.join(10)
    assert guard.busy() is False


# ------------------------------------------------------- fold budget (D378)


@pytest.mark.parametrize(
    ("retrain_s", "remaining_s", "want"),
    [
        (3600.0, 100.0, 100.0 + FOLD_BUDGET_EPSILON_S),
        (10.0, 100.0, 10.0),
        (100.0, 100.0, 100.0 + FOLD_BUDGET_EPSILON_S),
        (3600.0, 0.5, 0.5 + FOLD_BUDGET_EPSILON_S),
    ],
)
def test_fold_child_timeout(retrain_s: float, remaining_s: float, want: float) -> None:
    assert fold_child_timeout(retrain_s, remaining_s) == pytest.approx(want)
    assert FOLD_BUDGET_EPSILON_S > 0


class _Models:
    mode = "auto"


def _built() -> BuiltDataset:
    class _D:
        rows: tuple[Any, ...] = ()
        parquet = b""

    return BuiltDataset(_D(), 0, False)  # type: ignore[arg-type]


def test_both_expiring_together_reads_budget_exhausted_not_timeout() -> None:
    """A child that enforces its own limit (raising ``TimeoutError`` just
    before it): with the budget left as the limit it would win the race and
    the fold would read ``timeout``; with + epsilon the evaluation's deadline
    fires first."""
    limits: list[float] = []

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        limits.append(timeout_s)
        await asyncio.sleep(max(0.0, timeout_s - 0.05))
        raise TimeoutError

    async def build() -> BuiltDataset:
        return _built()

    inputs = FoldMlInputs(
        PlannerSettings(ml_eval_timeout_s=1.0),
        _Models(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        build,
        child=child,
        missing=list,
        clock=lambda: NOW,
    )
    ml = asyncio.run(inputs.prepare({"fam_a": frozenset()}, allow_leak=False))
    assert limits and limits[0] > 1.0
    (fold,) = ml.folds
    assert fold["reason"] == "eval_budget_exhausted: stopped, ml_eval_timeout_s = 1 s"


def test_a_short_retrain_timeout_is_still_a_genuine_timeout() -> None:
    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        assert timeout_s == 10.0
        raise TimeoutError

    async def build() -> BuiltDataset:
        return _built()

    inputs = FoldMlInputs(
        PlannerSettings(ml_eval_timeout_s=1000.0, ml_retrain_timeout_s=10.0),
        _Models(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        build,
        child=child,
        missing=list,
        clock=lambda: NOW,
    )
    ml = asyncio.run(inputs.prepare({"fam_a": frozenset()}, allow_leak=False))
    assert ml.folds[0]["reason"] == "timeout: killed after 10 s"


# ----------------------------------------------------------- _point y_hat


def _run_with_y_hat(y_hat: float) -> Any:
    s = copy.deepcopy(stored())
    terms = chosen(s)["estimate"]["terms"]
    raw = terms["compute"]["seconds"]
    terms["compute"]["detail"]["ml"] = {
        "model_version": "m1",
        "used": True,
        "y_hat": y_hat,
        "lo": y_hat - 0.1,
        "hi": y_hat + 0.1,
        "raw_s": raw,
    }
    return whole_run(s, phases=PHASES, decision_id=5)


@pytest.mark.parametrize(
    "y_hat", [MAX_Y_HAT + 1e-9, MAX_Y_HAT + 1.0, -MAX_Y_HAT - 1.0, 1e308]
)
def test_point_beyond_max_y_hat_is_used_but_has_no_ml_value(y_hat: float) -> None:
    points = points_of([_run_with_y_hat(y_hat)], versions={5: None}, calibrations={})
    (compute,) = [p for p in points if p.term == "compute"]
    assert compute.used is True
    assert compute.ml_s is None
    assert compute.reason is None
    assert compute.calibrated_s == compute.analytical_s  # still plotted


@pytest.mark.parametrize("y_hat", [MAX_Y_HAT, -MAX_Y_HAT, 0.0])
def test_point_at_max_y_hat_still_counts(y_hat: float) -> None:
    points = points_of([_run_with_y_hat(y_hat)], versions={5: None}, calibrations={})
    (compute,) = [p for p in points if p.term == "compute"]
    assert compute.ml_s == pytest.approx(compute.analytical_s * math.exp(y_hat))
    assert math.isfinite(compute.ml_s)


# ------------------------------------------------------ calibration skips


def _p(decision_id: int, calibrated: float | None) -> PredictionPoint:
    return PredictionPoint(
        decision_id=decision_id,
        term="compute",
        node_id="n",
        family=None,
        analytical_s=1.0,
        calibrated_s=calibrated,
        ml_s=None,
        used=True,
        reason=None,
        actual_s=1.0,
        ml_model_version=None,
    )


def test_calibration_versions_beyond_the_cap_are_counted() -> None:
    n = MAX_CALIBRATION_VERSIONS + 3
    versions: dict[int, int | None] = {i: i for i in range(1, n + 1)}  # v1..v19
    versions[100] = None
    versions[101] = n  # newest version, read
    points = [
        _p(1, None),  # v1: skipped (older than the newest 16)
        _p(2, None),  # v2: skipped
        _p(3, 4.0),  # v3: skipped but not null (a stored factor)
        _p(n, None),  # newest: read, unreadable -> not a skip
        _p(100, None),  # no version
    ]
    assert calibration_skips(points, versions) == (3, 2)
    assert calibration_skips(points, {1: 1}) == (0, 0)
    assert calibration_skips([], {}) == (0, 0)
