"""V0.14 G4 (D317, D303): ``meshforge.bench.stats.bootstrap_mean_ci``.

Seeded, order-independent, bit-for-bit repeatable; the interval holds the
sample mean for well-behaved data and shrinks with n; ``n == 1`` is a point;
bad arguments are refused. A pinned value guards against an accidental change
of the resampling stream (it would silently change every stored report's
numbers for the same inputs).
"""

from __future__ import annotations

import math
import random

import pytest

from meshforge.bench.stats import (
    BOOTSTRAP_LEVEL,
    BOOTSTRAP_RESAMPLES,
    bootstrap_mean_ci,
)

pytestmark = pytest.mark.unit

VALUES = [0.0, 0.0, 3.5, 12.0, 0.0, 41.0, 7.25, 0.0, 15.0, 2.0, 0.0, 9.0]


def test_defaults_are_the_d317_numbers() -> None:
    assert BOOTSTRAP_RESAMPLES == 10_000 and BOOTSTRAP_LEVEL == 0.95
    ci = bootstrap_mean_ci(VALUES, seed=3)
    assert (ci.resamples, ci.level, ci.seed, ci.n) == (10_000, 0.95, 3, 12)


def test_same_inputs_same_interval_whatever_the_order() -> None:
    first = bootstrap_mean_ci(VALUES, seed=11, resamples=2000)
    shuffled = list(VALUES)
    random.Random(5).shuffle(shuffled)  # noqa: S311 - test data
    assert bootstrap_mean_ci(shuffled, seed=11, resamples=2000) == first
    assert bootstrap_mean_ci(VALUES, seed=12, resamples=2000) != first


def test_pinned_value_of_the_resampling_stream() -> None:
    ci = bootstrap_mean_ci([1.0, 2.0, 3.0, 4.0], seed=42, resamples=1000)
    assert ci.mean == 2.5
    assert (ci.low, ci.high) == (1.5, 3.5)
    skewed = bootstrap_mean_ci(VALUES, seed=42, resamples=1000)
    assert (skewed.low, skewed.high) == (2.2083333333333335, 15.208333333333334)


def test_interval_holds_the_mean_and_narrows_with_more_data() -> None:
    rng = random.Random(1)  # noqa: S311 - test data
    small = [rng.gauss(10, 3) for _ in range(10)]
    large = [rng.gauss(10, 3) for _ in range(400)]
    a = bootstrap_mean_ci(small, seed=1, resamples=3000)
    b = bootstrap_mean_ci(large, seed=1, resamples=3000)
    assert a.low <= a.mean <= a.high and b.low <= b.mean <= b.high
    assert (b.high - b.low) < (a.high - a.low)
    assert math.isclose(b.mean, math.fsum(large) / len(large))


def test_one_value_is_a_point_and_constant_data_has_no_width() -> None:
    one = bootstrap_mean_ci([7.0], seed=0)
    assert (one.low, one.mean, one.high) == (7.0, 7.0, 7.0)
    flat = bootstrap_mean_ci([0.0] * 9, seed=0, resamples=100)
    assert (flat.low, flat.high) == (0.0, 0.0)


@pytest.mark.parametrize(
    ("values", "kwargs"),
    [
        ([], {"seed": 0}),
        ([1.0], {"seed": -1}),
        ([1.0], {"seed": True}),
        ([1.0], {"seed": 0, "resamples": 0}),
        ([1.0], {"seed": 0, "level": 1.0}),
        ([1.0], {"seed": 0, "level": 0.0}),
        ([float("nan")], {"seed": 0}),
    ],
)
def test_bad_arguments_are_refused(values: list[float], kwargs: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ValueError):
        bootstrap_mean_ci(values, **kwargs)
