"""V0.15 G6b tester: edge cases of the G5 debt fixes (brief 5h debt list)."""

from __future__ import annotations

import asyncio
import copy
import math
import threading
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_ml_fixtures import chosen, stored, whole_run

from meshforge.common import ml_explain
from meshforge.controller.ml.child import TrainingGuard
from meshforge.controller.ml.predictions import points_of
from meshforge.controller.planner import predictor
from meshforge.controller.planner_eval.ml_inputs import (
    FOLD_BUDGET_EPSILON_S,
    fold_child_timeout,
)

pytestmark = pytest.mark.unit

PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}


def test_max_y_hat_is_one_number_in_planner_and_common() -> None:
    assert predictor.MAX_Y_HAT == ml_explain.MAX_Y_HAT


@pytest.mark.parametrize(
    ("retrain", "left"),
    [(0.0, 0.0), (1e-9, 1e-9), (1.0, 1e-12), (3600.0, 1e9), (1e18, 1e-3)],
)
def test_fold_child_timeout_is_positive_and_never_below_the_budget(
    retrain: float, left: float
) -> None:
    got = fold_child_timeout(retrain, left)
    assert got > 0.0 or retrain == 0.0
    if retrain >= left:
        # the evaluation's own deadline must fire first, always
        assert got > left
        assert got - left == pytest.approx(FOLD_BUDGET_EPSILON_S, rel=1e-6, abs=1e-6)
    else:
        assert got == retrain < left


def test_fold_child_timeout_exactly_at_the_boundary_prefers_the_budget() -> None:
    assert fold_child_timeout(50.0, 50.0) == 50.0 + FOLD_BUDGET_EPSILON_S
    assert fold_child_timeout(math.nextafter(50.0, 0.0), 50.0) < 50.0


@pytest.mark.asyncio
async def test_busy_from_a_thread_while_two_loops_hold_and_release() -> None:
    guard = TrainingGuard()
    seen: list[bool] = []
    holding = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with guard.held():
            holding.set()
            await release.wait()

    t = asyncio.create_task(holder())
    await holding.wait()
    th = threading.Thread(target=lambda: seen.append(guard.busy()))
    th.start()
    await asyncio.to_thread(th.join)
    assert seen == [True] and guard.busy() is True
    release.set()
    await t
    seen.clear()
    th = threading.Thread(target=lambda: seen.append(guard.busy()))
    th.start()
    await asyncio.to_thread(th.join)
    assert seen == [False] and guard.busy() is False


def test_busy_in_a_thread_with_no_lock_ever_made_creates_none() -> None:
    guard = TrainingGuard()
    out: list[bool] = []
    th = threading.Thread(target=lambda: out.append(guard.busy()))
    th.start()
    th.join(10)
    assert out == [False] and len(guard._locks) == 0


def _compute_seconds() -> float:
    return float(
        chosen(copy.deepcopy(stored()))["estimate"]["terms"]["compute"]["seconds"]
    )


def _run(y_hat: float, raw_scale: float = 1.0) -> Any:
    s = copy.deepcopy(stored())
    terms = chosen(s)["estimate"]["terms"]
    raw = terms["compute"]["seconds"] * raw_scale
    terms["compute"]["seconds"] = raw
    terms["compute"]["detail"]["ml"] = {
        "model_version": "m1",
        "used": True,
        "y_hat": y_hat,
        "lo": y_hat,
        "hi": y_hat,
        "raw_s": raw,
    }
    return whole_run(s, phases=PHASES, decision_id=5)


@pytest.mark.parametrize("y_hat", [math.nan, math.inf, -math.inf])
def test_non_finite_y_hat_gives_no_ml_value(y_hat: float) -> None:
    points = points_of([_run(y_hat)], versions={5: None}, calibrations={})
    (c,) = [p for p in points if p.term == "compute"]
    assert c.ml_s is None


def test_ml_value_is_finite_at_the_largest_analytical_term() -> None:
    """y_hat at the cap times the planner's largest term (MAX_TERM_S) must
    stay finite: a JSON response cannot carry inf."""
    points = points_of(
        [_run(predictor.MAX_Y_HAT, raw_scale=1e9 / _compute_seconds())],
        versions={5: None},
        calibrations={},
    )
    (c,) = [p for p in points if p.term == "compute"]
    assert c.ml_s is not None and math.isfinite(c.ml_s)


@pytest.mark.parametrize(
    "y_hat",
    [
        math.nan,
        math.inf,
        -math.inf,
    ],
)
def test_explain_factor_for_non_finite_y_hat_is_none(y_hat: float) -> None:
    assert ml_explain._factor(y_hat) is None
