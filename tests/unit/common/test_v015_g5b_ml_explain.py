"""V0.15 G5b (D373): ``common/ml_explain`` - the ML text the CLI prints and
the Plan tab renders (its TypeScript copy ``mlText.ts`` is held equal on the
explain-contract fixture and on :data:`FIXED_CASES`, which the frontend test
repeats).
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from meshforge.common.ml_explain import (
    ML_REASON_TEXT,
    fixed,
    ml_decision_text,
    ml_line,
    reason_text,
    short_version,
)

pytestmark = pytest.mark.unit

FIXED_CASES: list[tuple[float, int, str]] = [
    (2.125, 2, "2.13"),  # an exact binary tie: half away from zero (toFixed)
    (-2.125, 2, "-2.13"),
    (0.5, 0, "1"),
    (1.5, 0, "2"),
    (2.5, 0, "3"),  # Python's round() would say 2
    (1.005, 2, "1.00"),  # 1.005 is 1.00499999... in binary
    (-0.001, 2, "0.00"),  # never "-0.00"
    (-0.0, 2, "0.00"),
    (33.155, 2, "33.16"),
    (123456.789, 1, "123456.8"),
    (0.0, 0, "0"),
]
"""``frontend/src/components/jobs/mlText.test.ts`` asserts the same table."""


@pytest.mark.parametrize(("value", "digits", "text"), FIXED_CASES)
def test_fixed_rounds_like_javascript_tofixed(
    value: float, digits: int, text: str
) -> None:
    assert fixed(value, digits) == text


@pytest.mark.parametrize("value", [None, math.nan, math.inf, -math.inf, 1e15, -2e16])
def test_fixed_refuses_what_it_cannot_print(value: float | None) -> None:
    assert fixed(value, 2) == "-"


def test_short_version_and_reason_words() -> None:
    assert short_version("sha256:" + "ab" * 32) == "abababababab"
    assert short_version("mem-3a1f00aa-extra") == "mem-3a1f00aa"
    assert short_version(None) == short_version("") == short_version(5) == "?"
    assert reason_text("ood") == "out of distribution"
    assert reason_text("brand_new") == "brand_new"
    assert reason_text(None) == "?"
    assert set(ML_REASON_TEXT) >= {
        "no_model",
        "ood",
        "interval_too_wide",
        "exact_by_construction",
        "below_min_predicted",
        "predictor_error",
    }


def _used(**extra: Any) -> dict[str, Any]:
    return {
        "model_version": "m1",
        "used": True,
        "y_hat": -0.235722,
        "lo": -0.4,
        "hi": -0.07,
        "raw_s": 2.531,
        "corrected_s": 2.0,
        "rate": None,
        **extra,
    }


def test_used_line() -> None:
    assert ml_line(_used()) == "ML 2.53 s -> 2.00 s (m1, x0.79, +-18 %)"


def test_used_is_read_exactly() -> None:
    """``used`` must be the boolean true: a truthy string is "not used"."""
    assert ml_line(_used(used="true")).startswith("ML not used: ")


def test_not_used_lines() -> None:
    assert ml_line({"used": False, "reason": "no_model", "model_version": "v"}) == (
        "ML not used: no model for this term (v)"
    )
    ood = {
        "used": False,
        "reason": "ood",
        "ood_reason": "knn:distance",
        "model_version": "v",
        "y_hat": 0.4,
        "lo": 0.1,
        "hi": 0.7,
    }
    assert ml_line(ood) == (
        "ML not used: out of distribution (knn:distance) (v, x1.49, +-35 %)"
    )


def test_rate_segment_is_per_item_in_ms() -> None:
    rate = {
        "used": True,
        "model_version": "m1",
        "y_hat": -0.1,
        "lo": -0.25,
        "hi": 0.05,
        "raw_s": 0.110517,
        "corrected_s": 0.1,
    }
    line = ml_line(_used(rate=rate))
    assert line is not None
    assert line.endswith(
        "; rate: ML 110.52 ms/item -> 100.00 ms/item (m1, x0.90, +-16 %)"
    )


@pytest.mark.parametrize("ml", [None, "x", 5, []])
def test_no_line_without_a_block(ml: Any) -> None:
    assert ml_line(ml) is None


def test_odd_numbers_never_raise() -> None:
    odd = _used(y_hat=1e308, lo="a", hi=None, raw_s=math.nan, corrected_s=-math.inf)
    assert ml_line(odd) == "ML - s -> - s (m1)"
    wide = _used(lo=-1e300, hi=1e300)  # the spread is capped, then refused
    assert ml_line(wide) == "ML 2.53 s -> 2.00 s (m1, x0.79, +-- %)"


def test_decision_text() -> None:
    assert ml_decision_text(None, None) is None
    assert ml_decision_text("sha256:" + "ab" * 32, None) == "ML set abababababab"
    assert ml_decision_text("v", "predictor_error") == (
        "ML set v - fell back to calibration: predictor error"
    )


def test_text_is_ascii() -> None:
    lines = [ml_line(_used())]
    lines += [ml_line({"used": False, "reason": r}) for r in ML_REASON_TEXT]
    for line in lines:
        assert line is not None and line.isascii()
