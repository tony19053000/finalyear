"""V0.9 G5 lane A item 2: ``meshforge.bench.stats`` - pure robust statistics.

Known values, the median-CI rule (n <= 5 -> [min, max]; n >= 6 -> the tightest
order-statistic interval with coverage >= 90 %, exact binomial coverage), the
MAD outlier rule, and property checks (hypothesis is not a dev dependency, so
they are parametrised over seeded random samples): permutation invariance
(bit-identical), scale equivariance, the CI contains the median.
"""

from __future__ import annotations

import math
import random
from fractions import Fraction

import pytest

from meshforge.bench import stats

pytestmark = pytest.mark.unit


def test_median_mean_std_cv_known_values() -> None:
    assert stats.median([3, 1, 2]) == 2.0
    assert stats.median([4, 1, 3, 2]) == 2.5
    assert stats.mean([1, 2, 3, 4]) == 2.5
    assert stats.sample_std([2, 4, 4, 4, 5, 5, 7, 9]) == pytest.approx(
        math.sqrt(32 / 7)
    )
    assert stats.cv([10, 10, 10]) == 0.0
    assert stats.cv([9, 11]) == pytest.approx(math.sqrt(2) / 10)


@pytest.mark.parametrize("values", [[], [5]])
def test_cv_and_std_are_none_below_two_values(values: list[int]) -> None:
    assert stats.cv(values) is None
    assert stats.sample_std(values) is None


def test_cv_is_none_when_the_mean_is_zero() -> None:
    assert stats.cv([-1, 1]) is None
    assert stats.cv([0, 0, 0]) is None


def test_empty_input_is_an_error_not_a_number() -> None:
    for fn in (stats.median, stats.mean, stats.median_ci):
        with pytest.raises(ValueError):
            fn([])
    assert stats.summarize([]) is None
    assert stats.mad_outliers([]) == []


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_non_finite_values_are_refused(bad: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        stats.median([1.0, bad])


@pytest.mark.parametrize(
    ("n", "coverage"),
    [(1, 0.0), (2, 0.5), (3, 0.75), (4, 0.875), (5, 0.9375)],
)
def test_small_n_interval_is_min_max_with_its_true_coverage(
    n: int, coverage: float
) -> None:
    values = [float(10 * i + 7) for i in range(n)]
    ci = stats.median_ci(list(reversed(values)))
    assert (ci.low, ci.high, ci.rank) == (min(values), max(values), 1)
    assert ci.coverage == coverage


def _exact(n: int, k: int) -> Fraction:
    return 1 - Fraction(2 * sum(math.comb(n, i) for i in range(k)), 2**n)


@pytest.mark.parametrize("n", [6, 7, 8, 9, 10, 11, 15, 20, 30, 50, 101, 400])
def test_large_n_interval_is_the_tightest_with_coverage_at_least_90(n: int) -> None:
    values = list(range(1, n + 1))
    ci = stats.median_ci(values)
    k = ci.rank
    assert (ci.low, ci.high) == (float(k), float(n + 1 - k))
    assert _exact(n, k) >= Fraction(9, 10)
    assert ci.coverage == float(_exact(n, k))
    # one step tighter would drop below 90 %
    assert k + 1 > (n + 1) // 2 or _exact(n, k + 1) < Fraction(9, 10)


def test_known_ranks() -> None:
    # binomial tables: n = 10 -> k = 2 (97.9 %), n = 20 -> k = 6 (95.9 %)
    assert stats.median_ci(list(range(10))).rank == 2
    assert stats.median_ci(list(range(20))).rank == 6
    assert stats.median_ci(list(range(20))).coverage == pytest.approx(0.9586, abs=1e-4)


def test_mad_outliers_rule() -> None:
    values = [10, 11, 10, 12, 11, 10, 100]
    # median 11, MAD 1 -> limit 3.5 * 1.4826 = 5.19
    assert stats.mad(values) == 1.0
    assert stats.mad_outliers(values) == [100.0]
    assert stats.mad_outliers([10, 11, 10, 12, 11, 10, 17]) == [17.0]
    assert stats.mad_outliers([10, 11, 10, 12, 11, 10, 16]) == []


def test_mad_zero_means_no_outliers() -> None:
    assert stats.mad_outliers([5, 5, 5, 5, 1000]) == []


def test_summarize_fields() -> None:
    summary = stats.summarize([12, 10, 11])
    assert summary == stats.Summary(
        n=3,
        median=11.0,
        mean=11.0,
        std=1.0,
        cv=pytest.approx(1 / 11),  # type: ignore[arg-type]
        ci_low=10.0,
        ci_high=12.0,
        ci_coverage=0.75,
        outliers=0,
    )


def _samples() -> list[list[float]]:
    rng = random.Random(20261019)  # noqa: S311 - seeded test data
    out: list[list[float]] = [
        [rng.lognormvariate(3.0, 0.6) for _ in range(n)]
        for n in (1, 2, 3, 4, 5, 6, 7, 9, 12, 25, 60)
        for _ in range(4)
    ]
    out.append([5.0] * 8)
    out.append([1.0, 1.0, 1.0, 50.0, 1.0, 1.0])
    return out


SAMPLES = _samples()


@pytest.mark.parametrize("values", SAMPLES, ids=[f"n{len(s)}" for s in SAMPLES])
def test_permutation_invariance_is_bit_identical(values: list[float]) -> None:
    rng = random.Random(len(values))  # noqa: S311 - seeded shuffle
    shuffled = values[:]
    rng.shuffle(shuffled)
    assert stats.summarize(shuffled) == stats.summarize(values)
    assert stats.summarize(list(reversed(values))) == stats.summarize(values)


@pytest.mark.parametrize("values", SAMPLES, ids=[f"n{len(s)}" for s in SAMPLES])
@pytest.mark.parametrize("scale", [2.0, 0.001, 1e6, 3.7])
def test_scale_equivariance(values: list[float], scale: float) -> None:
    a = stats.summarize(values)
    b = stats.summarize([v * scale for v in values])
    assert a is not None and b is not None
    assert b.n == a.n and b.outliers == a.outliers
    assert b.ci_coverage == a.ci_coverage
    for field in ("median", "mean", "ci_low", "ci_high"):
        assert getattr(b, field) == pytest.approx(getattr(a, field) * scale)
    if a.std is None:
        assert b.std is None
    else:
        assert b.std == pytest.approx(a.std * scale)
    if a.cv is None:
        assert b.cv is None
    else:  # CV is scale-free
        assert b.cv == pytest.approx(a.cv)


@pytest.mark.parametrize("values", SAMPLES, ids=[f"n{len(s)}" for s in SAMPLES])
def test_ci_contains_the_median_and_is_made_of_sample_values(
    values: list[float],
) -> None:
    ci = stats.median_ci(values)
    med = stats.median(values)
    assert ci.low <= med <= ci.high
    assert ci.low in values and ci.high in values
    assert 0.0 <= ci.coverage <= 1.0
    if len(values) >= 5:
        assert ci.coverage >= 0.9
