"""Node events for the bus: ``node.status`` and ``node.metrics`` (V0.8 G1, D172).

The session layer changes a node's liveness on its reader tasks and its lease
timer, synchronously and on a hot path; rather than threading a callback
through every arrow, :class:`NodeWatcher` **diffs** the session manager's
in-memory view every ``interval_s`` (0.25 s by default) and publishes what
changed:

* ``node.status`` ``{node_id, state, connected}`` when a node's state or its
  "has a live stream" flag differs from the last tick (a node whose session is
  gone entirely is published once as ``connected: false``);
* ``node.metrics`` - the newest live resource sample (with ``recv_at``, the
  controller's wall clock when it arrived) of each connected node,
  when it is new and at least ``metrics_interval_s`` (1 s) after the last one
  published for that node (agents report every 2 s; the cap is a guard).

The worst-case delay a diff adds is one interval - well inside the 2 s the
dashboard promises (criterion 2). Owned by the controller runtime; supervised
(an exception in one tick is logged and the next tick runs).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from meshforge.common.logging import get_logger
from meshforge.controller.events.bus import Audience, EventBus, EventType
from meshforge.controller.resources import LiveSample, ResourceTracker
from meshforge.controller.session.manager import SessionManager

__all__ = ["NodeWatcher", "metrics_frame"]

log = get_logger(__name__)

DEFAULT_INTERVAL_S: Final = 0.25
DEFAULT_METRICS_INTERVAL_S: Final = 1.0
_MAX_FORGOTTEN: Final = 4096


@dataclass(frozen=True, slots=True)
class _Status:
    state: str
    connected: bool


def metrics_frame(node_id: str, sample: LiveSample) -> dict[str, object]:
    """The ``node.metrics`` payload (also one point of ``GET .../metrics``)."""
    received = sample.received_wall
    return {
        "node_id": node_id,
        "observed_at": sample.observed_at.isoformat(),
        # The controller's wall clock at arrival (epoch seconds, like the bus
        # event's ``at``): latency is timed from here, never from the agent's
        # ``observed_at`` (V0.9 G0, D213 - cross-host clocks are skewed).
        "recv_at": None if received is None else round(received.timestamp(), 3),
        "cpu_percent": round(sample.cpu_percent, 2),
        "ram_used_bytes": sample.ram_used_bytes,
        "ram_total_bytes": sample.ram_total_bytes,
        "gpus": [
            {
                "index": g.index,
                "util_percent": g.util_percent,
                "vram_used_bytes": g.vram_used_bytes,
                "vram_total_bytes": g.vram_total_bytes,
            }
            for g in sample.gpus
        ],
    }


class NodeWatcher:
    """See module docstring."""

    def __init__(
        self,
        bus: EventBus,
        manager: SessionManager,
        resources: ResourceTracker,
        *,
        clock: Callable[[], float],
        interval_s: float = DEFAULT_INTERVAL_S,
        metrics_interval_s: float = DEFAULT_METRICS_INTERVAL_S,
    ) -> None:
        if interval_s <= 0 or metrics_interval_s <= 0:
            raise ValueError("intervals must be positive")
        self._bus = bus
        self._manager = manager
        self._resources = resources
        self._clock = clock
        self.interval_s = interval_s
        self.metrics_interval_s = metrics_interval_s
        self._status: dict[str, _Status] = {}
        self._metrics_at: dict[str, float] = {}
        self._metrics_seen: dict[str, tuple[int, int]] = {}
        self.ticks = 0
        self.crashes = 0

    def tick(self) -> None:
        """One diff (synchronous: no await between reading and publishing)."""
        self.ticks += 1
        current: dict[str, _Status] = {}
        for live in (*self._manager.lingering(), *self._manager.snapshot()):
            # A live entry wins over a lingering one of the same node.
            current[live.node_id] = _Status(
                state=live.state.value, connected=not live.ended and not live.lingering
            )
        for node_id, status in current.items():
            if self._status.get(node_id) != status:
                self._bus.publish(
                    EventType.NODE_STATUS,
                    {
                        "node_id": node_id,
                        "state": status.state,
                        "connected": status.connected,
                    },
                    audience=Audience.ALL,
                )
        for node_id, before in list(self._status.items()):
            if node_id not in current:
                if before.connected or before.state != "offline":
                    self._bus.publish(
                        EventType.NODE_STATUS,
                        {"node_id": node_id, "state": None, "connected": False},
                        audience=Audience.ALL,
                    )
                del self._status[node_id]
                self._metrics_at.pop(node_id, None)
                self._metrics_seen.pop(node_id, None)
        self._status.update(current)
        while len(self._status) > _MAX_FORGOTTEN:  # pragma: no cover - bounded
            self._status.pop(next(iter(self._status)))
        self._publish_metrics([n for n, s in current.items() if s.connected])

    def _publish_metrics(self, node_ids: list[str]) -> None:
        now = self._clock()
        for node_id in node_ids:
            sample = self._resources.latest(node_id)
            if sample is None:
                continue
            key = (sample.session_epoch, sample.sequence)
            if self._metrics_seen.get(node_id) == key:
                continue
            last = self._metrics_at.get(node_id)
            if last is not None and now - last < self.metrics_interval_s:
                continue
            self._metrics_seen[node_id] = key
            self._metrics_at[node_id] = now
            self._bus.publish(
                EventType.NODE_METRICS,
                metrics_frame(node_id, sample),
                audience=Audience.ALL,
            )

    async def run(self) -> None:
        while True:
            try:
                self.tick()
            except Exception as exc:  # noqa: BLE001 - supervised: log and go on
                self.crashes += 1
                log.error(  # noqa: TRY400 - the type only, no traceback of user data
                    "controller.events.watcher_crashed", cause=type(exc).__name__
                )
            await asyncio.sleep(self.interval_s)
