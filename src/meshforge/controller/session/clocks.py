"""Per-node clock offset from Ping/Pong round trips (V0.9 G1, D213/D226).

Benchmark records compare work done on different laptops. Durations are
measured where the work runs (monotonic, checklist 28), but a reader still
wants to know how far each node's wall clock is from the controller's - to
order events, and to know when a timestamp from that node means little.

Each Ping/Pong is one NTP-style exchange (:mod:`meshforge.common.clock_offset`),
**controller = local, agent = remote**:

* ``sent`` = the controller's wall clock when it queued the Ping;
* ``remote`` = the Pong envelope's ``sent_at`` (the agent's wall clock when it
  answered - the agent stamps it as the very first thing it does on a Ping);
* ``received`` = ``sent`` + the **monotonic** RTT (so a controller wall-clock
  step mid-exchange cannot fake a round trip).

The estimate uses only a recent :class:`~meshforge.common.clock_offset.ClockWindow`
(last :data:`WINDOW_EXCHANGES` exchanges, none older than :data:`WINDOW_AGE_S`):
an old minimum-RTT exchange cannot hide drift (D226). The agent reports its
wall clock's resolution (``Pong.clock_resolution_us``); an agent that does not
is assumed to tick in :data:`UNKNOWN_RESOLUTION_MS` steps (Windows on Python
3.12), so its bound is honest rather than tight. The bound uses the **coarsest**
resolution reported over the window, not only the latest one: every exchange in
the window feeds the estimate, so each one's tick limits it - and a resolution
that alternates between reports cannot flap the alert on every Ping.

A node's clock is **uncertain** when the bound exceeds :data:`ALERT_BOUND_MS`
or the window's exchanges contradict each other (``consistent=False``), once at
least :data:`MIN_EXCHANGES` exchanges are in the window. Entering that state
raises one alert per episode (an ``audit_event`` ``clock_uncertain`` row and a
live ``alert`` event); leaving it is logged.

Pure state; no I/O, no clock reads (the caller passes the times).
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from typing import Final

from meshforge.common.clock_offset import (
    ClockExchange,
    ClockOffset,
    ClockOffsetError,
    ClockWindow,
    estimate_offset,
)

__all__ = [
    "ALERT_BOUND_MS",
    "MAX_RESOLUTION_MS",
    "MIN_EXCHANGES",
    "UNKNOWN_RESOLUTION_MS",
    "WINDOW_AGE_S",
    "WINDOW_EXCHANGES",
    "ClockChange",
    "NodeClock",
    "resolution_ms",
    "uncertain_reasons",
]

WINDOW_EXCHANGES: Final = 32
"""Exchanges one estimate may use (one Ping per lease interval, 6 s by default:
about 3 minutes)."""
WINDOW_AGE_S: Final = 300.0
"""No exchange older than this (controller wall clock of its receipt) counts."""
ALERT_BOUND_MS: Final = 250.0
"""A bound above this makes the node's clock uncertain (brief §5b item 1)."""
MIN_EXCHANGES: Final = 4
"""Below this the estimate is reported but never alerted on (one slow Wi-Fi
round trip right after connecting is not a clock problem)."""
UNKNOWN_RESOLUTION_MS: Final = 16.0
"""Assumed tick of an agent that does not report one (Windows Python 3.12:
15.625 ms)."""
MAX_RESOLUTION_MS: Final = 1000.0
"""A reported resolution is clamped to this (a peer value is not trusted to
make the bound absurd; anything coarser is uncertain anyway)."""


@dataclass(frozen=True, slots=True)
class ClockChange:
    """The uncertain/certain state changed with this estimate."""

    uncertain: bool
    clock: ClockOffset
    reasons: tuple[str, ...]


def resolution_ms(clock_resolution_us: int) -> float:
    """The agent's reported tick in ms (0 = unknown -> the conservative guess)."""
    if clock_resolution_us <= 0:
        return UNKNOWN_RESOLUTION_MS
    return min(MAX_RESOLUTION_MS, clock_resolution_us / 1000.0)


@dataclass(slots=True)
class NodeClock:
    """One session's clock-offset state (see module docstring)."""

    window: ClockWindow = field(
        default_factory=lambda: ClockWindow(
            max_count=WINDOW_EXCHANGES, max_age_ms=WINDOW_AGE_S * 1000.0
        )
    )
    resolutions: deque[float] = field(default_factory=deque)
    """The agent's reported resolution of each exchange still in ``window``
    (same order; the window keeps a suffix of what was added)."""
    latest: ClockOffset | None = None
    uncertain: bool = False
    exchanges_rejected: int = 0
    """Exchanges refused as unusable (non-finite, negative RTT)."""

    def observe(
        self,
        *,
        sent_wall_ms: float,
        rtt_ms: float,
        agent_ms: float,
        agent_resolution_ms: float,
        local_resolution_ms: float = 0.0,
    ) -> ClockChange | None:
        """Add one exchange and re-estimate; a :class:`ClockChange` when the
        uncertain state flipped, else ``None``."""
        exchange = ClockExchange(
            sent_ms=sent_wall_ms, remote_ms=agent_ms, received_ms=sent_wall_ms + rtt_ms
        )
        values = (exchange.sent_ms, exchange.remote_ms, exchange.received_ms)
        if not all(math.isfinite(v) for v in values) or rtt_ms < 0:
            self.exchanges_rejected += 1
            return None
        self.window.add(exchange)
        self.resolutions.append(agent_resolution_ms)
        while len(self.resolutions) > len(self.window):
            self.resolutions.popleft()
        try:
            clock = estimate_offset(
                self.window.exchanges(),
                resolution_ms=local_resolution_ms,
                remote_resolution_ms=max(self.resolutions),
            )
        except ClockOffsetError:
            self.exchanges_rejected += 1
            return None
        self.latest = clock
        reasons = uncertain_reasons(clock)
        if clock.exchanges < MIN_EXCHANGES:
            return None
        now_uncertain = bool(reasons)
        if now_uncertain == self.uncertain:
            return None
        self.uncertain = now_uncertain
        return ClockChange(uncertain=now_uncertain, clock=clock, reasons=reasons)


def uncertain_reasons(clock: ClockOffset) -> tuple[str, ...]:
    """Why ``clock`` is not trustworthy (empty = it is)."""
    reasons: list[str] = []
    if not clock.consistent:
        reasons.append("inconsistent")
    if clock.bound_ms > ALERT_BOUND_MS:
        reasons.append("bound_over_limit")
    return tuple(reasons)
