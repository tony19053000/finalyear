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
  bound, so it is the estimate (NTP's minimum-delay filter) - **if the
  exchanges agree**: each one confines the true offset to an interval, and a
  clock that held still lies in all of them. If they do not overlap (a clock
  stepped between pings), the result is marked ``consistent=False`` and its
  bound widened to cover every interval: a step never yields a tight bound.

A remote timestamp ``r`` then corresponds to local time ``r - offset``.
Everything is in milliseconds. Pure functions: no I/O, no clock reads.

**Feed it a recent window only** (V0.9 G1, D213/D226): over a long series the
minimum-RTT exchange may be minutes old, and a clock that drifted since then
would hide behind that one tight, stale bound (newer, wider intervals still
overlap it). A continuous consumer - the controller's per-node offset from
heartbeats - keeps a :class:`ClockWindow` (the last N exchanges, none older
than T on the local clock) and estimates from it alone.

**Stamp resolution.** A clock that truncates (a browser's ``Date.now()``: whole
ms; Windows ``time.time()`` on Python 3.12: ~15.6 ms) stamps up to one tick
early. ``resolution_ms`` is the local tick, ``remote_resolution_ms`` the
remote one; both widen the bound. ``0`` (the default) assumes a stamp far finer
than the round trip - true for a Linux controller's ``time.time()`` (µs)
answering ``/v1/clock``; an agent reports its own resolution instead.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass

__all__ = [
    "ClockExchange",
    "ClockOffset",
    "ClockOffsetError",
    "ClockWindow",
    "estimate_offset",
    "round_up",
]


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
    consistent: bool = True
    """``False``: the exchanges contradict each other (their intervals do not
    overlap - a clock moved between pings); the bound then covers all of them
    instead of the shortest round trip's."""

    def to_local_ms(self, remote_ms: float) -> float:
        """A remote timestamp on the local clock."""
        return remote_ms - self.offset_ms

    def as_dict(self) -> dict[str, float | int | bool]:
        """Rounded to 0.001 ms - the bound **up** (D226: a reported bound is
        never tighter than the measured one). A reader that judges the bound
        must judge THIS value (the one it reports), never a differently
        rounded copy."""
        return {
            "clock_offset_ms": round(self.offset_ms, 3),
            "clock_offset_bound_ms": round_up(self.bound_ms, 3),
            "rtt_ms": round(self.rtt_ms, 3),
            "exchanges": self.exchanges,
            "consistent": self.consistent,
        }


def round_up(value: float, digits: int) -> float:
    """``value`` rounded to ``digits`` decimals, never below it (a non-finite
    value is returned unchanged)."""
    if not math.isfinite(value):
        return value
    shown = round(value, digits)
    if shown >= value:
        return shown
    return round(shown + 10.0**-digits, digits)


def estimate_offset(
    exchanges: Iterable[ClockExchange],
    *,
    resolution_ms: float = 0.0,
    remote_resolution_ms: float = 0.0,
) -> ClockOffset:
    """The minimum-round-trip estimate of ``exchanges``.

    ``resolution_ms`` is the local clock's tick when it truncates (a browser's
    ``Date.now()`` counts whole milliseconds): each local stamp is up to one
    tick EARLY, so the true offset lies in ``(remote - received - tick,
    remote - sent]`` - its centre is the midpoint estimate minus half a tick,
    its half-width ``(rtt + tick) / 2``. ``remote_resolution_ms`` is the same
    for the remote stamp (up to one remote tick early): it moves the centre up
    by half that tick and widens the half-width by half of it. Both default to
    0 = a stamp much finer than the round trip (see the module docstring).

    Raises :class:`ClockOffsetError` when there is none, or when any exchange
    is not finite or went backwards (``received < sent``: the local clock
    stepped mid-exchange - the measurement itself is unusable, never clamped).
    """
    for tick in (resolution_ms, remote_resolution_ms):
        if not (math.isfinite(tick) and tick >= 0):
            raise ClockOffsetError("a clock resolution must be a finite value >= 0")
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
    # Each exchange alone pins the true offset to an interval; a constant
    # offset lies in ALL of them. Disjoint intervals mean the clocks did not
    # hold still (a step, an NTP slew, a stamp taken elsewhere): never report
    # the tight min-RTT bound then - widen it to cover every interval.
    intervals = [_interval(e, resolution_ms, remote_resolution_ms) for e in seen]
    best = min(range(len(seen)), key=lambda i: (seen[i].rtt_ms, i))
    lowest = max(lo for lo, _ in intervals)
    highest = min(hi for _, hi in intervals)
    if lowest <= highest + _AGREE_EPS_MS:
        lo, hi = intervals[best]
        return ClockOffset(
            offset_ms=(lo + hi) / 2.0,
            bound_ms=(hi - lo) / 2.0,
            rtt_ms=seen[best].rtt_ms,
            exchanges=len(seen),
        )
    lo = min(lo for lo, _ in intervals)
    hi = max(hi for _, hi in intervals)
    return ClockOffset(
        offset_ms=(lo + hi) / 2.0,
        bound_ms=(hi - lo) / 2.0,
        rtt_ms=seen[best].rtt_ms,
        exchanges=len(seen),
        consistent=False,
    )


#: Float slack when intersecting intervals (ms): far below any clock tick.
_AGREE_EPS_MS = 1e-6


def _interval(
    exchange: ClockExchange, resolution_ms: float, remote_resolution_ms: float
) -> tuple[float, float]:
    """Where the true offset must lie given this one exchange (see
    :func:`estimate_offset` for the truncation ticks)."""
    centre = exchange.offset_ms + (remote_resolution_ms - resolution_ms) / 2.0
    half = (exchange.rtt_ms + resolution_ms + remote_resolution_ms) / 2.0
    return centre - half, centre + half


class ClockWindow:
    """The recent exchanges one estimate may use (D213/D226): at most
    ``max_count``, none whose ``received_ms`` is more than ``max_age_ms``
    older than the newest one's. Ages are judged on the exchanges' own LOCAL
    stamps (no clock read here). A local clock that went back (a new exchange
    received before the newest kept one) clears the window: the old exchanges
    are no longer on the same timeline. One owner; not thread-safe."""

    __slots__ = ("_items", "max_age_ms", "max_count")

    def __init__(self, *, max_count: int, max_age_ms: float) -> None:
        if max_count < 1:
            raise ValueError("max_count must be >= 1")
        if not (math.isfinite(max_age_ms) and max_age_ms > 0):
            raise ValueError("max_age_ms must be a finite value > 0")
        self.max_count = max_count
        self.max_age_ms = max_age_ms
        self._items: deque[ClockExchange] = deque(maxlen=max_count)

    def add(self, exchange: ClockExchange) -> None:
        """Keep ``exchange`` and drop what the window no longer allows."""
        if self._items and exchange.received_ms < self._items[-1].received_ms:
            self._items.clear()
        self._items.append(exchange)
        horizon = exchange.received_ms - self.max_age_ms
        while self._items and self._items[0].received_ms < horizon:
            self._items.popleft()

    def clear(self) -> None:
        self._items.clear()

    def exchanges(self) -> tuple[ClockExchange, ...]:
        return tuple(self._items)

    def __len__(self) -> int:
        return len(self._items)
