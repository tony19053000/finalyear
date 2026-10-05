"""Link arithmetic shared by the probe store, the REST view and the planner's
snapshot builder (V0.6 G4, D111) - pure.

* :func:`ewma` - the stored value after one more measurement
  (``alpha * new + (1 - alpha) * old``; the first sample is taken as is);
* :func:`is_stale` - a link measured longer ago than ``stale_after_s`` (1 h)
  is shown but not trusted: the estimator falls back (D108);
* :class:`LinkRow` - one ``network_link`` row as plain data.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

__all__ = ["LinkRow", "age_s", "ewma", "is_stale"]


def ewma(old: float | None, new: float, alpha: float) -> float:
    """The EWMA after ``new`` (``old`` None = first sample)."""
    if not 0.0 < alpha <= 1.0:
        raise ValueError("alpha must be in (0, 1]")
    if old is None:
        return new
    return alpha * new + (1.0 - alpha) * old


def age_s(measured_at: dt.datetime, now: dt.datetime) -> float:
    """Seconds since ``measured_at`` (never negative: a clock step back is 0)."""
    return max(0.0, (now - measured_at).total_seconds())


def is_stale(measured_at: dt.datetime, now: dt.datetime, stale_after_s: float) -> bool:
    return age_s(measured_at, now) > stale_after_s


@dataclass(frozen=True, slots=True)
class LinkRow:
    """``src`` downloaded from ``dst``'s peer server (data flows dst -> src)."""

    src_node_id: str
    dst_node_id: str
    rtt_ms: float
    bw_bytes_s: float
    samples: int
    measured_at: dt.datetime
