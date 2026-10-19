"""V0.9 G5 lane A tester r1: the stats module against an independent oracle.

The oracle re-derives the order-statistic interval from the *definition*
(simulated exactly by enumerating all 2**n sign patterns of "sample point below
the median") instead of the closed form the module uses.
"""

from __future__ import annotations

import itertools
import math
import random
from fractions import Fraction

import pytest

from meshforge.bench import stats

pytestmark = pytest.mark.unit


def oracle_coverage(n: int, k: int) -> Fraction:
    """P(x_(k) <= median <= x_(n+1-k)): the count B of values below the true
    median is Bin(n, 1/2); the interval covers iff k <= B + 1 and B <= n - k
    (enumerated exactly, no closed form)."""
    # coverage fails when B <= k-1 (all of x_(1..k) ... ) or B >= n-k+1
    fails = sum(
        1
        for pattern in itertools.product((0, 1), repeat=n)
        if sum(pattern) <= k - 1 or sum(pattern) >= n - k + 1
    )
    return Fraction(2**n - fails, 2**n)


def oracle_rank(n: int) -> int:
    if n <= 5:
        return 1
    best = 1
    for k in range(1, (n + 1) // 2 + 1):
        if oracle_coverage(n, k) >= Fraction(9, 10):
            best = k
    return best


@pytest.mark.parametrize("n", range(3, 17))
def test_ci_rank_and_coverage_match_exact_enumeration(n: int) -> None:
    xs = list(range(10, 10 + n))
    ci = stats.median_ci(xs)
    k = oracle_rank(n)
    assert ci.rank == k
    assert (ci.low, ci.high) == (xs[k - 1], xs[n - k])
    assert ci.coverage == float(oracle_coverage(n, k))


@pytest.mark.parametrize("n", range(6, 21))
def test_ci_is_the_tightest_with_at_least_90_percent(n: int) -> None:
    ci = stats.median_ci(list(range(n)))
    assert ci.coverage >= 0.9
    # one rank tighter would drop below 90 % (or be impossible)
    if ci.rank + 1 <= (n + 1) // 2:
        k = ci.rank + 1
        tail = sum(math.comb(n, i) for i in range(k))
        assert 1 - Fraction(2 * tail, 2**n) < Fraction(9, 10)


@pytest.mark.parametrize("n", range(3, 6))
def test_small_n_is_min_max(n: int) -> None:
    xs = [5.0, 1.0, 9.0, 3.0, 7.0][:n]
    ci = stats.median_ci(xs)
    assert (ci.low, ci.high) == (min(xs), max(xs))


@pytest.mark.parametrize("n", range(1, 21))
def test_ci_contains_median_and_is_order_invariant(n: int) -> None:
    rng = random.Random(n)  # noqa: S311
    xs = [rng.uniform(-50, 500) for _ in range(n)]
    a = stats.median_ci(xs)
    rng.shuffle(xs)
    b = stats.median_ci(xs)
    assert a == b
    assert a.low <= stats.median(xs) <= a.high


def test_all_equal_values() -> None:
    s = stats.summarize([7, 7, 7, 7])
    assert s is not None
    assert (s.median, s.mean, s.std, s.cv) == (7.0, 7.0, 0.0, 0.0)
    assert (s.ci_low, s.ci_high, s.outliers) == (7.0, 7.0, 0)


def test_cv_none_for_mean_zero_and_single_value_and_empty() -> None:
    assert stats.cv([-1, 1]) is None  # mean exactly 0
    assert stats.cv([0, 0, 0]) is None
    assert stats.cv([5]) is None
    assert stats.cv([]) is None
    assert stats.sample_std([5]) is None


def test_cv_value() -> None:
    assert stats.cv([2, 4]) == pytest.approx(math.sqrt(2) / 3)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_values_are_refused_loudly_not_silently_averaged(bad: float) -> None:
    for fn in (stats.median, stats.mean, stats.cv, stats.mad_outliers, stats.summarize):
        with pytest.raises(ValueError):
            fn([1.0, bad, 3.0])


def test_empty_inputs() -> None:
    assert stats.summarize([]) is None
    assert stats.mad_outliers([]) == []
    with pytest.raises(ValueError):
        stats.median([])


def test_mad_zero_means_no_outliers_even_with_a_huge_value() -> None:
    assert stats.mad_outliers([5, 5, 5, 5, 1_000_000]) == []
    assert stats.mad([5, 5, 5, 5, 1_000_000]) == 0


def test_mad_outlier_boundary_is_strict() -> None:
    # [9,9,10,10,11,11,x]: median 10, MAD 1, limit 3.5 * 1.4826 = 5.1891
    limit = 3.5 * 1.4826
    base = [9, 9, 10, 10, 11, 11]
    assert stats.mad_outliers([*base, 10 + limit - 1e-6]) == []
    assert stats.mad_outliers([*base, 10 + limit + 1e-6]) == [10 + limit + 1e-6]
    # the low side counts too
    assert stats.mad_outliers([*base, 10 - limit - 1e-6]) == [10 - limit - 1e-6]


def test_scale_equivariance_and_shift() -> None:
    xs = [3.0, 9.0, 4.0, 8.0, 100.0, 5.0, 7.0]
    a, b = stats.summarize(xs), stats.summarize([x * 4 for x in xs])
    assert a is not None and b is not None
    assert b.median == 4 * a.median and b.ci_low == 4 * a.ci_low
    assert b.cv == pytest.approx(a.cv)  # type: ignore[arg-type]
    assert b.outliers == a.outliers


def test_huge_and_tiny_magnitudes_do_not_overflow_or_lose_order() -> None:
    xs = [1, 2, 9_000_000_000_000_000_000, 3]
    s = stats.summarize(xs)
    assert s is not None and s.median == 2.5 and s.ci_high == 9e18


def test_bool_like_and_int_float_mix() -> None:
    assert stats.median([1, 2.0, 3]) == 2.0
