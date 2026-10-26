"""Small, dependency-free statistics for the soak rules (pure)."""

from __future__ import annotations

import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime


def parse_ts(text: object) -> datetime | None:
    """An ISO-8601 time stamp (any offset) as aware UTC, ``None`` if not one."""
    if not isinstance(text, str):
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC)


def median(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def slope_per_hour(points: Sequence[tuple[float, float]]) -> float | None:
    """Least-squares slope of ``(t_seconds, value)`` in value units per hour;
    ``None`` with fewer than 3 points or no time spread."""
    if len(points) < 3:
        return None
    n = len(points)
    mean_t = sum(t for t, _ in points) / n
    mean_v = sum(v for _, v in points) / n
    var = sum((t - mean_t) ** 2 for t, _ in points)
    if var <= 0:
        return None
    cov = sum((t - mean_t) * (v - mean_v) for t, v in points)
    return cov / var * 3600.0


@dataclass(frozen=True, slots=True)
class Window:
    """A half-open time window ``[start, end)`` in seconds since the run start."""

    name: str
    start_s: float
    end_s: float

    def holds(self, t_s: float) -> bool:
        return self.start_s <= t_s < self.end_s

    def as_dict(self) -> dict[str, float | str]:
        return {
            "name": self.name,
            "start_h": round(self.start_s / 3600.0, 4),
            "end_h": round(self.end_s / 3600.0, 4),
        }


def pct_change(before: float, after: float) -> float | None:
    if before == 0:
        return None
    return (after - before) / before * 100.0
