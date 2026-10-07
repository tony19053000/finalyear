"""Which link to probe next, within every D111 bound (V0.6 G4) - pure.

:class:`ProbeScheduler` keeps no I/O and reads no clock: the caller passes the
controller's monotonic ``now`` and the candidate nodes, and gets back at most one
:class:`PlannedProbe`. The bounds, each enforced here and tested with an
injected clock:

* **interval** - an ordered pair ``(src, dst)`` is probed at most once per
  ``probe_interval_s`` (a failed or timed-out probe counts: a broken pair is not
  retried every gap; a pair measured before a controller restart is
  :meth:`~ProbeScheduler.seed`-ed with that measurement's time, so the bound
  holds across restarts);
* **one per node** - a node that is the source or the target of a probe in
  flight takes part in no other;
* **budget** - the bytes of probes finished in the last 60 s plus the
  worst case of every probe in flight plus the new one's worst case must stay
  within ``probe_budget_bytes_per_min``; a finished probe's reservation is
  replaced by the bytes it really moved;
* **busy nodes** - never a node running a GPU job, or whose latest CPU sample is
  above ``busy_cpu_percent``, or whose latest sample is too old to say
  (``cpu_stale``: a load that is no longer known is not a low load). A node
  that never sent a sample (its resource monitor is off) is not blocked;
* **stagger** - at most one probe starts per ``probe_gap_s`` scaled by a random
  factor in ``[1 - jitter, 1 + jitter]`` (the first one too, so a controller
  restart does not probe at once);
* **timeout** - a probe with no result after ``probe_timeout_s`` is given up and
  its nodes are free again.

Among eligible pairs the one probed longest ago (never = first) wins; ties
break on ``(src, dst)``, so the choice is deterministic for a seeded RNG.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final

__all__ = [
    "BUDGET_WINDOW_S",
    "InFlight",
    "PlannedProbe",
    "ProbeConfig",
    "ProbeNode",
    "ProbeScheduler",
]

BUDGET_WINDOW_S: Final = 60.0


@dataclass(frozen=True, slots=True)
class ProbeConfig:
    interval_s: float = 900.0
    gap_s: float = 15.0
    jitter: float = 0.5
    budget_bytes_per_min: int = 48 * 1024 * 1024
    probe_bytes: int = 4 * 1024 * 1024
    probe_max_bytes: int = 8 * 1024 * 1024
    timeout_s: float = 60.0
    busy_cpu_percent: float = 80.0

    @property
    def worst_case_bytes(self) -> int:
        return self.probe_bytes + self.probe_max_bytes


@dataclass(frozen=True, slots=True)
class ProbeNode:
    """A node as the scheduler sees it."""

    node_id: str
    endpoint: str
    """Its peer server ``host:port`` ("" = it cannot be a probe target)."""
    gpu_job: bool = False
    """A live attempt holds one of its GPUs."""
    cpu_percent: float | None = None
    cpu_stale: bool = False
    """Its latest resource sample is older than ``cpu_sample_max_age_s``: the
    load is unknown, so the node is not probed (tester r1 observation)."""


@dataclass(frozen=True, slots=True)
class PlannedProbe:
    probe_id: str
    src: str
    """Runs the probe (downloads)."""
    dst: str
    """Serves it (its peer server)."""
    endpoint: str


@dataclass(slots=True)
class InFlight:
    src: str
    dst: str
    started_at: float
    reserved: int


class ProbeScheduler:
    """See module docstring. Single owner (the prober task)."""

    def __init__(
        self,
        config: ProbeConfig,
        *,
        rng: random.Random,
        now: float,
    ) -> None:
        self.config = config
        self._rng = rng
        self.in_flight: dict[str, InFlight] = {}
        self._last: dict[tuple[str, str], float] = {}
        self._spent: deque[tuple[float, int]] = deque()
        self._next_at = now + self._gap()
        self._seq = 0
        self.started = 0
        self.timed_out = 0

    # -------------------------------------------------------------- helpers

    def _gap(self) -> float:
        jitter = self.config.jitter
        return self.config.gap_s * self._rng.uniform(1.0 - jitter, 1.0 + jitter)

    def spent(self, now: float) -> int:
        """Bytes counted against the budget at ``now`` (finished in the window
        + in-flight reservations)."""
        while self._spent and self._spent[0][0] <= now - BUDGET_WINDOW_S:
            self._spent.popleft()
        return sum(b for _t, b in self._spent) + sum(
            f.reserved for f in self.in_flight.values()
        )

    def busy(self, node: ProbeNode) -> bool:
        return (
            node.gpu_job
            or node.cpu_stale
            or (
                node.cpu_percent is not None
                and node.cpu_percent > self.config.busy_cpu_percent
            )
        )

    @property
    def next_at(self) -> float:
        return self._next_at

    def defer(self, now: float) -> None:
        """Nothing can be decided at ``now`` (the caller's inputs could not be
        read): the next attempt is one jittered gap away, like after a pick."""
        self._next_at = now + self._gap()

    def last_probed(self, src: str, dst: str) -> float | None:
        return self._last.get((src, dst))

    def seed(self, src: str, dst: str, last: float) -> None:
        """A pair measured before this scheduler existed (a controller
        restart): ``last`` is that measurement on the caller's monotonic clock
        (``now - age``; it may be negative). The interval then counts from it,
        so a restart does not probe every pair again. Never moves a pair
        backwards."""
        known = self._last.get((src, dst))
        if known is None or last > known:
            self._last[(src, dst)] = last

    # ---------------------------------------------------------------- steps

    def expire(self, now: float) -> list[tuple[str, InFlight]]:
        """Give up probes older than ``timeout_s``; their bytes stay counted
        at the reservation (we cannot know what moved)."""
        gone = [
            (pid, f)
            for pid, f in self.in_flight.items()
            if now - f.started_at >= self.config.timeout_s
        ]
        for pid, flight in gone:
            del self.in_flight[pid]
            self._spent.append((flight.started_at, flight.reserved))
            self.timed_out += 1
        return gone

    def pick(self, nodes: Iterable[ProbeNode], now: float) -> PlannedProbe | None:
        """At most one probe to start now (module docstring)."""
        if now < self._next_at:
            return None
        self._next_at = now + self._gap()
        cfg = self.config
        if cfg.budget_bytes_per_min < cfg.worst_case_bytes:
            return None
        if self.spent(now) + cfg.worst_case_bytes > cfg.budget_bytes_per_min:
            return None
        engaged = {f.src for f in self.in_flight.values()} | {
            f.dst for f in self.in_flight.values()
        }
        free = sorted(
            (n for n in nodes if n.node_id not in engaged and not self.busy(n)),
            key=lambda n: n.node_id,
        )
        best: tuple[float, str, str, str] | None = None
        for src in free:
            for dst in free:
                if src.node_id == dst.node_id or not dst.endpoint:
                    continue
                last = self._last.get((src.node_id, dst.node_id))
                if last is not None and now - last < cfg.interval_s:
                    continue
                key = (
                    float("-inf") if last is None else last,
                    src.node_id,
                    dst.node_id,
                    dst.endpoint,
                )
                if best is None or key[:3] < best[:3]:
                    best = key
        if best is None:
            return None
        _last, src_id, dst_id, endpoint = best
        self._seq += 1
        probe_id = f"prb{self._seq:06d}{self._rng.getrandbits(32):08x}"
        self.in_flight[probe_id] = InFlight(
            src_id, dst_id, started_at=now, reserved=cfg.worst_case_bytes
        )
        self._last[(src_id, dst_id)] = now
        self.started += 1
        return PlannedProbe(probe_id, src_id, dst_id, endpoint)

    def finish(self, probe_id: str, moved: int, now: float) -> InFlight | None:
        """A result arrived: free the nodes and count what really moved
        (``None`` = not a probe in flight - unknown, late or a duplicate)."""
        flight = self.in_flight.pop(probe_id, None)
        if flight is None:
            return None
        self._spent.append((flight.started_at, max(0, min(moved, flight.reserved))))
        return flight

    def abort(self, probe_id: str) -> None:
        """The command could not be sent: nothing moved, the pair may be tried
        again after its interval."""
        flight = self.in_flight.pop(probe_id, None)
        if flight is not None:
            self.started -= 1

    def forget_node(self, node_id: str) -> list[str]:
        """The node went offline: its probes in flight are given up (their
        reservation stays counted until the window passes)."""
        gone = [pid for pid, f in self.in_flight.items() if node_id in (f.src, f.dst)]
        for pid in gone:
            flight = self.in_flight.pop(pid)
            self._spent.append((flight.started_at, flight.reserved))
        return gone
