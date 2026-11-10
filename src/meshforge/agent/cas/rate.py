"""An optional cap on how fast this agent receives blob bytes (V1.0 G9-A, D431).

``[agent.fetch] max_rate_mbit`` (0 = off). On a shared Wi-Fi a staging that
pulls a 5 GB dataset as fast as the link allows leaves no air time for the
other laptops' traffic - their heartbeats included. With a cap, every blob byte
the agent receives (chunk ``Range`` GETs and whole-blob GETs, from the
controller and from peers, and whole-bundle fetches) is charged to **one
process-wide token bucket**; a request that has received more than its share
simply waits before it reads on. Not reading lets the socket's receive buffer
fill, TCP flow control closes the window and the **sender** slows down - the
link really carries less, nothing is dropped or re-sent. Tree manifests and
uploads are not charged (small / the other direction).

**Algorithm.** A token bucket of ``rate`` bytes/s holding at most ``burst``
bytes (``max(64 KiB, rate x 0.25 s)``), kept as "tokens may go negative": a
piece of ``n`` bytes is charged at once (:meth:`FetchRateLimiter.reserve`, no
``await`` between reading and updating the state, so it is atomic on the
agent's one event loop - no lock) and the caller sleeps until the bucket is
back at zero. Concurrent requests queue behind each other's debt, so the
aggregate rate is the cap however many workers and sources a staging uses. One
``asyncio.sleep`` per charged piece, never a busy loop. A request cancelled
while it waits keeps its charge (its bytes did arrive).

Interaction worth knowing: a peer server disconnects a reader slower than its
``[agent.peer] min_send_rate_bytes_s`` (64 KiB/s); with ``N`` requests in
flight each gets about ``cap / N`` - keep the cap above roughly
``0.6 Mbit/s x N`` (``N`` = sources x ``per_source``).

Time is the injected monotonic clock (``loop.time`` / ``time.monotonic``);
tests inject the clock and the sleep.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Final

__all__ = [
    "BURST_S",
    "MAX_RATE_MBIT",
    "MIN_BURST_BYTES",
    "MIN_RATE_MBIT",
    "FetchRateLimiter",
]

MIN_RATE_MBIT: Final = 1.0
"""Smallest cap accepted (below it peers disconnect a throttled reader)."""
MAX_RATE_MBIT: Final = 100_000.0
"""Largest cap accepted (100 Gbit/s: anything above is no cap at all)."""
BURST_S: Final = 0.25
MIN_BURST_BYTES: Final = 64 * 1024


class FetchRateLimiter:
    """See module docstring. One per agent process (the executor's)."""

    def __init__(
        self,
        bytes_per_s: float,
        *,
        burst_bytes: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not bytes_per_s > 0:
            raise ValueError("the rate must be > 0 bytes/s")
        self.rate = float(bytes_per_s)
        self.burst = float(
            burst_bytes
            if burst_bytes is not None
            else max(MIN_BURST_BYTES, self.rate * BURST_S)
        )
        if not self.burst > 0:
            raise ValueError("the burst must be > 0 bytes")
        self._clock = clock
        self._sleep = sleep
        self._tokens = self.burst
        self._at = clock()
        self.charged_bytes = 0
        """Every byte charged since start (telemetry, tests)."""
        self.waited_s = 0.0
        """Total time callers were told to wait (overlapping waits add up)."""

    @classmethod
    def from_mbit(
        cls,
        mbit_per_s: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> FetchRateLimiter:
        """``[agent.fetch] max_rate_mbit`` (megabits, 10^6 bits, per second)."""
        return cls(mbit_per_s * 1_000_000 / 8, clock=clock, sleep=sleep)

    def reserve(self, n: int) -> float:
        """Charge ``n`` received bytes now; the seconds to wait before reading
        on (0 = within the bucket). Synchronous: atomic on the event loop."""
        if n <= 0:
            return 0.0
        now = self._clock()
        elapsed = max(0.0, now - self._at)
        self._at = now
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._tokens -= n
        self.charged_bytes += n
        if self._tokens >= 0:
            return 0.0
        wait = -self._tokens / self.rate
        self.waited_s += wait
        return wait

    async def take(self, n: int) -> None:
        """Charge ``n`` bytes and wait out any debt (one sleep, no polling)."""
        wait = self.reserve(n)
        if wait > 0:
            await self._sleep(wait)
