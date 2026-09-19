"""Summary statistics for repeated measurements.

``stdev`` is the *sample* standard deviation (n - 1), ``None`` for n < 2.
``p95`` uses linear interpolation between closest ranks (NumPy's default
"linear" method), so it is defined for any n >= 1. Non-finite samples are
rejected: a NaN in a result file would silently poison every later mean.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass

from spikes._harness import MIN_VALID_REPS


@dataclass(frozen=True, slots=True)
class Summary:
    """Statistics of one metric over ``n`` measured repetitions."""

    n: int
    mean: float | None
    stdev: float | None
    min: float | None
    max: float | None
    median: float | None
    p95: float | None

    @property
    def valid(self) -> bool:
        """At least :data:`MIN_VALID_REPS` repetitions (the lab rule)."""
        return self.n >= MIN_VALID_REPS

    def to_json(self) -> dict[str, float | int | None]:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Mapping[str, object]) -> Summary:
        def num(key: str) -> float | None:
            value = data.get(key)
            if value is None:
                return None
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise TypeError(f"summary field {key!r} is not a number")
            return float(value)

        n = data.get("n")
        if not isinstance(n, int) or isinstance(n, bool):
            raise TypeError("summary field 'n' must be an integer")
        if n < 0:
            raise ValueError("summary field 'n' must be non-negative")
        return cls(
            n=n,
            mean=num("mean"),
            stdev=num("stdev"),
            min=num("min"),
            max=num("max"),
            median=num("median"),
            p95=num("p95"),
        )


def quantile(sorted_values: Sequence[float], q: float) -> float:
    """Linear-interpolated quantile of already sorted, non-empty values."""
    if not sorted_values:
        raise ValueError("quantile of no values")
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    pos = (len(sorted_values) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_values) - 1)
    frac = pos - lo
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * frac


def summarize(values: Iterable[float]) -> Summary:
    """Summary of ``values``; an empty input gives ``n=0`` and ``None`` fields."""
    data = [float(v) for v in values]
    for v in data:
        if not math.isfinite(v):
            raise ValueError(f"non-finite sample: {v!r}")
    if not data:
        return Summary(0, None, None, None, None, None, None)
    ordered = sorted(data)
    return Summary(
        n=len(data),
        mean=statistics.fmean(data),
        stdev=statistics.stdev(data) if len(data) >= 2 else None,
        min=ordered[0],
        max=ordered[-1],
        median=statistics.median(ordered),
        p95=quantile(ordered, 0.95),
    )


def mean_abs_delta(values: Sequence[float]) -> float | None:
    """Mean ``|x[i] - x[i-1]|`` over consecutive values; ``None`` for n < 2.

    For RTT samples this is RFC 3550's ``|D(i-1, i)|`` averaged without the 1/16
    smoothing, i.e. a stable estimate of the same quantity.
    """
    if len(values) < 2:
        return None
    return statistics.fmean(abs(b - a) for a, b in itertools.pairwise(values))


def rfc3550_jitter(values: Sequence[float]) -> float | None:
    """RFC 3550 §6.4.1 running jitter ``J += (|D| - J) / 16`` over consecutive
    values (its final value); ``None`` for n < 2."""
    if len(values) < 2:
        return None
    jitter = 0.0
    for a, b in itertools.pairwise(values):
        jitter += (abs(b - a) - jitter) / 16.0
    return jitter
