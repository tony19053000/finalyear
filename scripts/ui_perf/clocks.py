"""The browser<->controller clock offset (V0.9 G0, D210 item 1, D213).

Live latency compares a CONTROLLER timestamp (an event's ``recv_at``, a
transition's ``at``) with the moment the page shows it (the browser's
``Date.now()``). On a LAN the two hosts' clocks differ, so the offset is
measured first: sequential round trips from the signed-in page to
``GET /v1/clock``, estimated by :mod:`meshforge.common.clock_offset` (the
shortest round trip wins; the true offset is within +- RTT/2). A bound over
:data:`MAX_CLOCK_BOUND_MS` makes the run ``skew_uncertain``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Final

from meshforge.common.clock_offset import ClockExchange, ClockOffset, estimate_offset

if TYPE_CHECKING:
    from playwright.sync_api import Page


#: The browser<->controller offset must be known within this (ms; the bound is
#: (RTT + 1 ms tick) / 2 of the best ping) for the live-latency numbers to
#: count: above it the run is
#: ``skew_uncertain`` - re-run with the browser nearer the controller (wired,
#: or on its host) rather than pass or fail on an unknown clock.
MAX_CLOCK_BOUND_MS: Final = 50.0
CLOCK_PINGS: Final = 16
#: ``Date.now()`` truncates to whole milliseconds (the bound widens by it).
DATE_NOW_TICK_MS: Final = 1.0
#: ``n`` round trips to ``GET /v1/clock`` from the page (the page's cookie, its
#: ``Date.now()`` - the clock the DOM probes stamp with), sequential.
CLOCK_PING: Final = """
async (n) => {
  const out = [];
  for (let i = 0; i < n; i++) {
    const sent = Date.now();
    const r = await fetch("/v1/clock", {cache: "no-store", credentials: "same-origin"});
    if (!r.ok) throw new Error("GET /v1/clock -> " + r.status);
    const body = await r.json();
    out.push([sent, body.server_time * 1000, Date.now()]);
  }
  return out;
}
"""


def measure_clock(page: Page, pings: int = CLOCK_PINGS) -> ClockOffset:
    """The controller's clock minus the page's (the signed-in ``page`` must be
    on the controller's origin), estimated from ``pings`` round trips."""
    rows = page.evaluate(CLOCK_PING, pings)
    return estimate_offset(
        (
            ClockExchange(float(sent), float(remote), float(received))
            for sent, remote, received in rows
        ),
        resolution_ms=DATE_NOW_TICK_MS,
    )


def clock_report(clock: ClockOffset) -> dict[str, Any]:
    """The offset as the results carry it (with the verdict's limit)."""
    return {
        **clock.as_dict(),
        "max_bound_ms": MAX_CLOCK_BOUND_MS,
        "uncertain": clock.bound_ms > MAX_CLOCK_BOUND_MS,
    }
