"""Clock offset between two hosts from round trips, NTP style (V0.9 G0, D213).

Laptops on Wi-Fi do not share a clock: subtracting a timestamp taken on one
host from one taken on another adds the skew between them to the result. When
two clocks must be compared anyway (the dashboard's event latency, V0.9's
per-node clock offset), measure the offset first:

* the local side notes ``sent`` (its clock), asks the remote for its time,
  gets ``remote``, and notes ``received`` (its clock again);
* if the remote stamped half-way through the round trip, then
  ``offset = remote - (sent + received) / 2`` is "remote clock minus local
  clock", and the true offset is within ``± (received - sent) / 2`` of it
  whatever the path asymmetry (the remote stamped *somewhere* inside the round
  trip) - the **bound**;
* of several exchanges the one with the shortest round trip has the tightest
  bound, so it is the estimate (NTP's minimum-delay filter).

A remote timestamp ``r`` then corresponds to local time ``r - offset``.
Everything is in milliseconds. Pure functions: no I/O, no clock reads.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = ["ClockExchange", "ClockOffset", "ClockOffsetError", "estimate_offset"]


class ClockOffsetError(ValueError):
    """No usable exchange (none given, a negative round trip, a non-finite value)."""


@dataclass(frozen=True, slots=True)
class ClockExchange:
    """One round trip, all in milliseconds."""

    sent_ms: float
    """Local clock just before the request left."""
    remote_ms: float
    """The remote's clock when it answered."""
    received_ms: float
    """Local clock just after the answer arrived."""

    @property
    def rtt_ms(self) -> float:
        return self.received_ms - self.sent_ms

    @property
    def offset_ms(self) -> float:
        """Remote clock minus local clock (assuming a symmetric path)."""
        return self.remote_ms - (self.sent_ms + self.received_ms) / 2.0


@dataclass(frozen=True, slots=True)
class ClockOffset:
    """The best estimate of ``remote - local`` and how far off it can be."""

    offset_ms: float
    bound_ms: float
    """The true offset is within ``offset_ms ± bound_ms`` (half the round trip)."""
    rtt_ms: float
    exchanges: int
    """How many round trips were considered (the shortest one won)."""

    def to_local_ms(self, remote_ms: float) -> float:
        """A remote timestamp on the local clock."""
        return remote_ms - self.offset_ms

    def as_dict(self) -> dict[str, float | int]:
        return {
            "clock_offset_ms": round(self.offset_ms, 1),
            "clock_offset_bound_ms": round(self.bound_ms, 1),
            "rtt_ms": round(self.rtt_ms, 1),
            "exchanges": self.exchanges,
        }


def estimate_offset(exchanges: Iterable[ClockExchange]) -> ClockOffset:
    """The minimum-round-trip estimate of ``exchanges``.

    Raises :class:`ClockOffsetError` when there is none, or when any exchange
    is not finite or went backwards (``received < sent``: the local clock
    stepped mid-exchange - the measurement itself is unusable, never clamped).
    """
    seen = list(exchanges)
    if not seen:
        raise ClockOffsetError("no clock exchange to estimate an offset from")
    for exchange in seen:
        values = (exchange.sent_ms, exchange.remote_ms, exchange.received_ms)
        if not all(math.isfinite(v) for v in values):
            raise ClockOffsetError("a clock exchange has a non-finite timestamp")
        if exchange.rtt_ms < 0:
            raise ClockOffsetError(
                "a clock exchange went backwards (the local clock stepped)"
            )
    best = min(seen, key=lambda e: e.rtt_ms)
    return ClockOffset(
        offset_ms=best.offset_ms,
        bound_ms=best.rtt_ms / 2.0,
        rtt_ms=best.rtt_ms,
        exchanges=len(seen),
    )
