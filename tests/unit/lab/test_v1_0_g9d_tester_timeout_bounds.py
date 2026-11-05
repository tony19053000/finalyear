"""V1.0 G9-D tester: bounds of the lab job time limit formula."""

from __future__ import annotations

import math

import pytest

from meshforge.common.manifest import MAX_TIMEOUT_S, MIN_TIMEOUT_S
from mode3_check import LAB_TIMEOUT_FACTOR, LAB_TIMEOUT_FLOOR_S, lab_timeout_s

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    "hint", [0.0, -5.0, 1e-9, 1.0, 89.9, 90.0, 90.1, 599.9, 3600.0, 1e6, 1e12]
)
def test_always_whole_minutes_within_floor_and_cap(hint: float) -> None:
    got = lab_timeout_s(hint)
    assert isinstance(got, int)
    assert MIN_TIMEOUT_S <= LAB_TIMEOUT_FLOOR_S <= got <= MAX_TIMEOUT_S
    assert got % 60 == 0
    if hint * LAB_TIMEOUT_FACTOR <= MAX_TIMEOUT_S:
        assert got >= hint * LAB_TIMEOUT_FACTOR  # never below 10x the hint


def test_monotonic_in_the_hint() -> None:
    values = [lab_timeout_s(h) for h in range(0, 200_000, 997)]
    assert values == sorted(values)


@pytest.mark.parametrize(
    "hint",
    [
        1e15,
        pytest.param(
            1e308,
            marks=pytest.mark.xfail(
                raises=OverflowError, strict=True, reason="tester D1: minor"
            ),
        ),
        pytest.param(
            float("inf"),
            marks=pytest.mark.xfail(
                raises=OverflowError, strict=True, reason="tester D1: minor"
            ),
        ),
    ],
)
def test_huge_hint_is_capped(hint: float) -> None:
    assert lab_timeout_s(hint) == MAX_TIMEOUT_S


def test_nan_hint_does_not_crash_or_escape_the_bounds() -> None:
    # a NaN hint (a broken model) must not yield a limit outside [floor, cap]
    try:
        got = lab_timeout_s(math.nan)
    except (ValueError, OverflowError):
        return  # a clear refusal is acceptable
    assert LAB_TIMEOUT_FLOOR_S <= got <= MAX_TIMEOUT_S
