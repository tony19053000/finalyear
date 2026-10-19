"""Pure robust statistics for benchmark labels (V0.9 G5, roadmap 0.9.6, D218/D246).

No I/O, no numpy, no clock: plain Python on a sequence of finite numbers. Every
function first **sorts** its input and sums with :func:`math.fsum` (exactly
rounded), so the result does not depend on the order of the values - the same
measurements give the same numbers, bit for bit (D222). Used by the controller's
labels (:mod:`meshforge.controller.telemetry.labels`) and by the quality report
(0.9.8, G5c).

* :func:`median`, :func:`mean`, :func:`sample_std` (``n - 1``), :func:`cv`
  (``std / |mean|``; ``None`` when ``n < 2`` or ``mean == 0``);
* :func:`mad_outliers` - the values with ``|x - median| > 3.5 * 1.4826 * MAD``
  (MAD = median absolute deviation; ``MAD == 0`` -> none);
* :func:`median_ci` - a **distribution-free** confidence interval of the median
  from order statistics: the number of values below the true median is
  Binomial(n, 1/2), so ``[x_(k), x_(n+1-k)]`` (1-based order statistics)
  covers it with probability ``1 - 2 * P(B <= k - 1)``, whatever the
  distribution. ``n <= 5`` -> ``[min, max]`` (``k = 1``; coverage 0 %, 50 %,
  75 %, 87.5 %, 93.75 %); ``n >= 6`` -> the **tightest** such interval (the
  largest ``k``) whose coverage is still >= :data:`CI_COVERAGE` (90 %). The
  achieved coverage is reported, never assumed.
* :func:`summarize` - all of the above for one metric.

Why order statistics and not mean +- t * s / sqrt(n): benchmark timings are
skewed (a cold cache, a background job) and three to ten repetitions say
nothing about normality; the order-statistic interval needs no such assumption
(D218: "a noisy best teaches the ML planner noise").
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Final

__all__ = [
    "CI_COVERAGE",
    "MAD_SCALE",
    "MAD_THRESHOLD",
    "SMALL_N",
    "MedianCI",
    "Summary",
    "cv",
    "mad",
    "mad_outliers",
    "mean",
    "median",
    "median_ci",
    "sample_std",
    "summarize",
]

CI_COVERAGE: Final = Fraction(9, 10)
"""Minimum coverage of the median interval for ``n >= 6`` (exact fraction)."""
SMALL_N: Final = 5
"""Up to this many values the interval is ``[min, max]``."""
MAD_SCALE: Final = 1.4826
"""MAD -> standard deviation of a normal distribution (consistency constant)."""
MAD_THRESHOLD: Final = 3.5
"""Outlier rule: more than this many scaled MADs from the median."""

type Number = int | float


def _sorted(values: Sequence[Number]) -> list[float]:
    out = sorted(float(v) for v in values)
    if any(not math.isfinite(v) for v in out):
        raise ValueError("statistics need finite values")
    return out


def _median_sorted(xs: Sequence[float]) -> float:
    n = len(xs)
    if n == 0:
        raise ValueError("the median of no values")
    mid = n // 2
    if n % 2:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0


def median(values: Sequence[Number]) -> float:
    """The median (mean of the two middle values when ``n`` is even)."""
    return _median_sorted(_sorted(values))


def mean(values: Sequence[Number]) -> float:
    xs = _sorted(values)
    if not xs:
        raise ValueError("the mean of no values")
    return math.fsum(xs) / len(xs)


def sample_std(values: Sequence[Number]) -> float | None:
    """Sample standard deviation (``n - 1``); ``None`` for fewer than 2 values."""
    xs = _sorted(values)
    if len(xs) < 2:
        return None
    m = math.fsum(xs) / len(xs)
    return math.sqrt(math.fsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def cv(values: Sequence[Number]) -> float | None:
    """Coefficient of variation ``std / |mean|``; ``None`` when ``n < 2`` or the
    mean is 0 (undefined, not infinite)."""
    xs = _sorted(values)
    if len(xs) < 2:
        return None
    m = math.fsum(xs) / len(xs)
    std = sample_std(xs)
    if m == 0 or std is None:
        return None
    return std / abs(m)


def mad(values: Sequence[Number]) -> float:
    """Median absolute deviation from the median (unscaled)."""
    xs = _sorted(values)
    centre = _median_sorted(xs)
    return _median_sorted(sorted(abs(x - centre) for x in xs))


def mad_outliers(values: Sequence[Number]) -> list[float]:
    """The values farther than ``3.5 * 1.4826 * MAD`` from the median, in
    ascending order; none when ``MAD == 0`` (half the values are equal: no
    scale to judge by) or there are no values."""
    xs = _sorted(values)
    if not xs:
        return []
    centre = _median_sorted(xs)
    spread = _median_sorted(sorted(abs(x - centre) for x in xs))
    if spread == 0:
        return []
    limit = MAD_THRESHOLD * MAD_SCALE * spread
    return [x for x in xs if abs(x - centre) > limit]


@dataclass(frozen=True, slots=True)
class MedianCI:
    low: float
    high: float
    coverage: float
    """Achieved coverage probability of ``[low, high]`` for the median."""
    rank: int
    """``k``: the interval is the k-th smallest .. k-th largest value."""


def _coverage(n: int, k: int) -> Fraction:
    """P(x_(k) <= true median <= x_(n+1-k)) = 1 - 2 P(Bin(n, 1/2) <= k - 1)."""
    tail = sum(math.comb(n, i) for i in range(k))
    return 1 - Fraction(2 * tail, 2**n)


def median_ci(values: Sequence[Number]) -> MedianCI:
    """Distribution-free interval for the median (module docstring)."""
    xs = _sorted(values)
    n = len(xs)
    if n == 0:
        raise ValueError("the interval of no values")
    k = 1
    if n > SMALL_N:
        # the largest k (tightest interval) whose coverage is still >= 90 %;
        # coverage falls as k grows, and k = 1 always qualifies for n >= 5
        while k + 1 <= (n + 1) // 2 and _coverage(n, k + 1) >= CI_COVERAGE:
            k += 1
    return MedianCI(
        low=xs[k - 1],
        high=xs[n - k],
        coverage=float(_coverage(n, k)),
        rank=k,
    )


@dataclass(frozen=True, slots=True)
class Summary:
    """One metric over one configuration's repetitions."""

    n: int
    median: float
    mean: float
    std: float | None
    cv: float | None
    ci_low: float
    ci_high: float
    ci_coverage: float
    outliers: int


def summarize(values: Sequence[Number]) -> Summary | None:
    """Every statistic of ``values``; ``None`` for no values."""
    xs = _sorted(values)
    if not xs:
        return None
    ci = median_ci(xs)
    return Summary(
        n=len(xs),
        median=_median_sorted(xs),
        mean=math.fsum(xs) / len(xs),
        std=sample_std(xs),
        cv=cv(xs),
        ci_low=ci.low,
        ci_high=ci.high,
        ci_coverage=ci.coverage,
        outliers=len(mad_outliers(xs)),
    )
