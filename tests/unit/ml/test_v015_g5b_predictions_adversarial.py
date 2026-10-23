"""V0.15 G5b tester round 1 (D374): ``points_of`` on hostile stored decisions.

Stored decision JSON is data written by older versions and by the planner;
the Models page must never get a 500, a NaN or an invented number from it.
"""

from __future__ import annotations

import copy
import json
import math
from typing import Any

import pytest
from mf_ml_fixtures import chosen, stored, whole_run

from meshforge.controller.ml.predictions import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    points_of,
)
from meshforge.controller.planner.predictor import MAX_Y_HAT

pytestmark = pytest.mark.unit

PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}
SET = "sha256:" + "ab" * 32


def _run(block: Any, *, set_version: Any = SET, decision_id: int = 5) -> Any:
    s = copy.deepcopy(stored())
    for row in s.candidates:
        row["ml_model_version"] = set_version
    chosen(s)["estimate"]["terms"]["compute"]["detail"]["ml"] = block
    return whole_run(s, phases=PHASES, decision_id=decision_id)


def _compute(points: list[Any]) -> Any:
    found = [p for p in points if p.term == "compute"]
    assert len(found) == 1
    return found[0]


def test_bounds_are_the_documented_ones() -> None:
    assert (DEFAULT_LIMIT, MAX_LIMIT) == (500, 2000)


@pytest.mark.parametrize(
    "block",
    [
        None,
        "x",
        7,
        [],
        {},
        {"used": "true"},
        {"used": 1},
        {"used": True},  # used without y_hat
        {"used": True, "y_hat": "0.1"},
        {"used": True, "y_hat": float("nan")},
        {"used": True, "y_hat": float("inf")},
        {"used": True, "y_hat": True},
        {"used": False, "reason": ""},
        {"used": False, "reason": 5},
    ],
)
def test_junk_ml_blocks_never_invent_an_ml_value(block: Any) -> None:
    p = _compute(points_of([_run(block)], versions={}, calibrations={}))
    assert p.ml_s is None
    if isinstance(block, dict) and block.get("used") is True:
        assert p.used is True
    else:
        assert p.used is False
    assert p.reason is None or (isinstance(p.reason, str) and p.reason)
    for v in (p.analytical_s, p.actual_s):
        assert math.isfinite(v)


def test_absurd_y_hat_gives_no_ml_value() -> None:
    p = _compute(
        points_of(
            [_run({"used": True, "y_hat": MAX_Y_HAT + 1.0})],
            versions={},
            calibrations={},
        )
    )
    assert p.ml_s is None


@pytest.mark.parametrize("version", [None, 5, ["x"], "", {}])
def test_junk_model_version_is_null_and_never_matches_a_set_filter(
    version: Any,
) -> None:
    run = _run({"used": False, "reason": "ood"}, set_version=version)
    points = points_of([run], versions={}, calibrations={})
    assert points and all(p.ml_model_version is None for p in points)
    assert points_of([run], versions={}, calibrations={}, set_version=SET) == []


@pytest.mark.parametrize("factor", ["2", None, float("nan"), -3.0, True])
def test_junk_stored_calibration_factor_is_not_trusted(factor: Any) -> None:
    s = copy.deepcopy(stored())
    terms = chosen(s)["estimate"]["terms"]
    terms["compute"]["detail"]["calibration"] = {"factor": factor, "version": 1}
    run = whole_run(s, phases=PHASES, decision_id=2)
    p = _compute(points_of([run], versions={2: 1}, calibrations={1: None}))
    assert p.calibrated_s is None or math.isfinite(p.calibrated_s)
    assert p.calibrated_s != float("nan")


def test_points_are_json_safe_for_a_thousand_decisions() -> None:
    runs = [
        _run({"used": True, "y_hat": -0.1 * (i % 5)}, decision_id=i + 1)
        for i in range(300)
    ]
    points = points_of(runs, versions={}, calibrations={})
    assert len(points) >= 300
    text = json.dumps(
        [(p.analytical_s, p.calibrated_s, p.ml_s, p.actual_s) for p in points],
        allow_nan=False,
    )
    assert text
