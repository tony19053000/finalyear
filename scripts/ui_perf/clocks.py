"""The browser<->controller clock offset (V0.9 G0, D210 item 1, D213).

Live latency compares a CONTROLLER timestamp (an event's ``recv_at``, a
transition's ``at``) with the moment the page shows it (the browser's
``Date.now()``). On a LAN the two hosts' clocks differ, so the offset is
measured: sequential round trips from the signed-in page to ``GET /v1/clock``,
estimated by :mod:`meshforge.common.clock_offset` (min-RTT exchange; the true
offset is within the bound; pings that contradict each other widen it).

**Never a silent pass on a bad clock** (fix round 1, Lead ruling): the offset
is measured before AND after each events phase (:func:`phase_clock`); the
phase is ``uncertain`` when either measurement failed or is inconsistent,
either bound exceeds :data:`MAX_CLOCK_BOUND_MS`, the two disagree beyond their
combined bounds (the clock moved during the phase), or any latency is below
``-bound`` (shown before it happened: impossible on a correct offset).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Final

from meshforge.common.clock_offset import (
    ClockExchange,
    ClockOffset,
    ClockOffsetError,
    estimate_offset,
)

if TYPE_CHECKING:
    from playwright.sync_api import Page

#: The browser<->controller offset must be known within this (ms; the bound is
#: (RTT + 1 ms tick) / 2 of the best ping) for the live-latency numbers to
#: count: above it the run is ``skew_uncertain`` - re-run with the browser
#: nearer the controller (wired, or on its host) rather than pass or fail on an
#: unknown clock.
MAX_CLOCK_BOUND_MS: Final = 50.0
CLOCK_PINGS: Final = 16
#: More than this share of one measurement's pings dropped makes the phase
#: uncertain (Lead, fix round 2): the usable rest may be a biased sample.
MAX_DROPPED_SHARE: Final = 0.25
#: ``Date.now()`` truncates to whole milliseconds (the bound widens by it).
DATE_NOW_TICK_MS: Final = 1.0
#: ``n`` round trips to ``GET /v1/clock`` from the page (the page's cookie, its
#: ``Date.now()`` - the clock the DOM probes stamp with), sequential. A failed
#: request is a ``null`` row (dropped and counted), never a page exception.
CLOCK_PING: Final = """
async (n) => {
  const out = [];
  for (let i = 0; i < n; i++) {
    const sent = Date.now();
    try {
      const r = await fetch("/v1/clock", {cache: "no-store", credentials: "same-origin"});
      if (!r.ok) { out.push(null); continue; }
      const body = await r.json();
      out.push([sent, body.server_time * 1000, Date.now()]);
    } catch (e) {
      out.push(null);
    }
  }
  return out;
}
"""


def _usable(row: Any) -> ClockExchange | None:
    """A ping row as an exchange, or ``None`` (failed, malformed, non-finite,
    or backwards: the browser's clock stepped during that one ping)."""
    try:
        sent, remote, received = (float(v) for v in row)
    except (TypeError, ValueError):
        return None
    exchange = ClockExchange(sent, remote, received)
    values = (sent, remote, received)
    if not all(math.isfinite(v) for v in values) or exchange.rtt_ms < 0:
        return None
    return exchange


def ping_clock(page: Page, pings: int = CLOCK_PINGS) -> tuple[ClockOffset, int]:
    """(offset estimate, pings dropped). A bad ping is dropped and counted,
    not fatal; fewer than half of ``pings`` usable (at least one) is
    :class:`ClockOffsetError`."""
    rows = page.evaluate(CLOCK_PING, pings)
    good = [e for e in (_usable(r) for r in rows or []) if e is not None]
    dropped = len(rows or []) - len(good)
    needed = max(1, pings // 2)
    if len(good) < needed:
        raise ClockOffsetError(
            f"only {len(good)} of {pings} clock pings usable (need {needed})"
        )
    return estimate_offset(good, resolution_ms=DATE_NOW_TICK_MS), dropped


def measure_clock(page: Page, pings: int = CLOCK_PINGS) -> ClockOffset:
    """The controller's clock minus the page's (the signed-in ``page`` must be
    on the controller's origin), estimated from ``pings`` round trips."""
    return ping_clock(page, pings)[0]


def bound_uncertain(bound_ms: float | None) -> bool:
    """THE rule for a (reported) bound: missing, not finite or over the
    limit. The report's flag and the verdict both call this on the same
    number."""
    return (
        bound_ms is None or not math.isfinite(bound_ms) or bound_ms > MAX_CLOCK_BOUND_MS
    )


def _json_number(value: Any) -> Any:
    """A non-finite float as ``None``: the report is strict JSON (no
    ``Infinity``/``NaN`` tokens, fix round 2 DEFECT-R1)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def clock_report(clock: ClockOffset) -> dict[str, Any]:
    """One measurement as the results carry it (with the verdict's limit)."""
    shown = {k: _json_number(v) for k, v in clock.as_dict().items()}
    return {
        **shown,
        "max_bound_ms": MAX_CLOCK_BOUND_MS,
        "uncertain": bound_uncertain(shown["clock_offset_bound_ms"])
        or not clock.consistent,
    }


def phase_clock(
    before: ClockOffset | str,
    after: ClockOffset | str,
    lags_ms: Sequence[float],
    *,
    dropped: int | Sequence[int] = 0,
    pings: int = CLOCK_PINGS,
) -> dict[str, Any]:
    """The ``clock`` block of an events phase: both measurements (an ``str``
    is the error of a failed one), the reasons it is uncertain (empty = it
    is not), and the top-level fields the verdict and the lab read - the
    offset used (``before``) and the WORSE bound of the two (``None`` when a
    measurement failed: the report never holds ``Infinity``).

    ``dropped``: pings dropped per measurement ``(before, after)``, or one
    total over both; more than :data:`MAX_DROPPED_SHARE` of a measurement's
    ``pings`` dropped is a reason (the survivors may be a biased sample)."""
    reasons: list[str] = []
    reports: dict[str, dict[str, Any]] = {}
    for name, clock in (("before", before), ("after", after)):
        if isinstance(clock, str):
            reasons.append(f"{name}: clock measurement failed ({clock})")
            reports[name] = {"error": clock}
            continue
        reports[name] = clock_report(clock)
        if not clock.consistent:
            reasons.append(f"{name}: pings disagree (a clock moved while pinging)")
        bound = reports[name]["clock_offset_bound_ms"]
        if bound_uncertain(bound):
            reasons.append(f"{name}: bound {bound} ms > {MAX_CLOCK_BOUND_MS} ms")
    per = [dropped] if isinstance(dropped, int) else list(dropped)
    allowed = MAX_DROPPED_SHARE * pings * (2 if isinstance(dropped, int) else 1)
    reasons.extend(
        f"{count} clock pings dropped (> {MAX_DROPPED_SHARE:.0%} of {pings})"
        for count in per
        if count > allowed
    )
    bounds = [r["clock_offset_bound_ms"] for r in reports.values() if "error" not in r]
    worst: float | None = None
    if len(bounds) == 2 and all(b is not None for b in bounds):
        worst = float(max(bounds))
    if isinstance(before, ClockOffset) and isinstance(after, ClockOffset):
        moved = abs(after.offset_ms - before.offset_ms)
        if not moved <= before.bound_ms + after.bound_ms:  # NaN-safe
            reasons.append(
                f"offset moved {moved:.1f} ms during the phase "
                f"(> {before.bound_ms + after.bound_ms:.1f} ms combined bound)"
            )
    finite = [lag for lag in lags_ms if math.isfinite(lag)]
    if len(finite) != len(lags_ms):
        # fix round 2 (DEFECT-R3): a NaN must never hide a negative lag; a
        # non-finite lag itself means the timing broke.
        reasons.append(f"{len(lags_ms) - len(finite)} latencies are not finite")
    if finite and worst is not None and min(finite) < -worst:
        reasons.append(
            f"a latency of {min(finite):.1f} ms is below -bound ({-worst} ms)"
        )
    used = reports["before"]
    return {
        "clock_offset_ms": used.get("clock_offset_ms"),
        "clock_offset_bound_ms": worst,
        "max_bound_ms": MAX_CLOCK_BOUND_MS,
        "pings_dropped": sum(per),
        "before": reports["before"],
        "after": reports["after"],
        "reasons": reasons,
        "uncertain": bool(reasons) or bound_uncertain(worst),
    }
