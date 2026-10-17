"""What heartbeats and pongs leave behind in the registry (split out of ``manager.py``, D42).

**Heartbeat persistence is coalesced.** The lease is extended in memory on every
heartbeat (``SessionManager``), but ``node.last_seen_at`` is written at most once
per ``heartbeat_interval_s`` per node: the first heartbeat of a window enqueues a
write at once; later ones mark the session dirty and the
:class:`~.leases.LeaseTimer` flushes it when the window closes. A burst of
heartbeats therefore costs one queued DB round-trip, not one per message.

**RTT** (roadmap 0.3.10) is measured on the controller's monotonic clock only:
``clock()`` at the Pong minus ``clock()`` at the Ping (never from the agent's
timestamps, which measure skew, not latency). Only the latest outstanding ping
counts; an unmatched Pong is logged at debug and ignored.

**Clock offset** (V0.9 G1, D213): the same Ping/Pong is one NTP-style
exchange - the controller's wall clock at the Ping, the Pong envelope's
``sent_at`` (the agent's wall clock), and the Ping's wall time plus the
monotonic RTT as the receive time (:mod:`.clocks`). The estimate rides on the
RTT write; entering the uncertain state writes one ``clock_uncertain`` audit
row (never dropped) and then calls :attr:`HeartbeatRecorder.on_clock_alert`
with the live ``alert`` payload. A Pong without ``sent_at`` (no agent clock)
records the RTT only.

Nothing here awaits the database: writes go on the session's
:class:`~.writer.WriteChain` as droppable kinds (a later one supersedes them).
"""

from __future__ import annotations

import functools
import time
from collections.abc import Callable

from meshforge.common.ids import new_command_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.registry import Registry
from meshforge.controller.session.clocks import ClockChange, resolution_ms
from meshforge.controller.session.live import LiveSession, update_ewma
from meshforge.controller.session.writer import WriteKind

__all__ = ["HeartbeatRecorder"]

log = get_logger(__name__)

#: The controller's own wall-clock tick (ms) - its ``sent`` stamps truncate to it.
_LOCAL_RESOLUTION_MS = time.get_clock_info("time").resolution * 1000.0


class HeartbeatRecorder:
    """Coalesced ``touch_node`` writes and RTT samples (see module docstring)."""

    def __init__(
        self,
        registry: Registry,
        liveness: LivenessConfig,
        *,
        clock: Callable[[], float],
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._registry = registry
        self._liveness = liveness
        self._clock = clock
        self._wall_clock = wall_clock
        """Epoch seconds; only the ``sent`` of a clock-offset exchange."""
        self.on_clock_alert: Callable[[dict[str, str]], None] | None = None
        """Set by the server: publishes the live ``alert`` event (D180)."""

    def touch(self, live: LiveSession, now: float, *, force: bool) -> None:
        """Queue a ``last_seen_at`` write at most once per ``heartbeat_interval_s``.

        ``force`` is the timer's flush of a closed window. The persisted value is
        the write's wall time, at most one interval (plus queueing) after the
        heartbeat it stands for - liveness itself is decided in memory.
        """
        interval = self._liveness.heartbeat_interval_s
        window_open = live.last_touch_at is not None and (
            now - live.last_touch_at < interval
        )
        if window_open and not force:
            live.touch_dirty = True
            return
        live.last_touch_at = now
        live.touch_dirty = False
        live.writer.enqueue(
            "touch_node",
            WriteKind.TOUCH,
            functools.partial(self._registry.touch_node, live.node_id),
        )

    def flush_touch(self, live: LiveSession, now: float) -> None:
        """Timer hook: queue a pending ``last_seen_at`` write once its window closed."""
        due = live.touch_due_at(self._liveness.heartbeat_interval_s)
        if due is None or now < due or live.ended:
            return
        self.touch(live, now, force=True)

    def ping(self, live: LiveSession, now: float) -> agent_pb2.ControllerMessage:
        """A new Ping for ``live``; the previous outstanding one simply expires."""
        ping_id = new_command_id()
        live.outstanding_ping = (ping_id, now)
        live.outstanding_ping_wall_ms = self._wall_clock() * 1000.0
        live.next_ping_at = now + self._liveness.offline_after_s
        live.pings_sent += 1
        return agent_pb2.ControllerMessage(
            message_id=ping_id,
            session_id=live.session_id,
            ping=agent_pb2.Ping(ping_id=ping_id),
        )

    async def on_pong_message(
        self, live: LiveSession, msg: agent_pb2.AgentMessage
    ) -> None:
        """The ``pong`` dispatch handler."""
        agent_ms = (
            msg.sent_at.ToNanoseconds() / 1_000_000.0
            if msg.HasField("sent_at")
            else None
        )
        self.on_pong(
            live,
            msg.pong.ping_id,
            agent_ms=agent_ms,
            clock_resolution_us=msg.pong.clock_resolution_us,
        )

    def on_pong(
        self,
        live: LiveSession,
        ping_id: str,
        *,
        agent_ms: float | None = None,
        clock_resolution_us: int = 0,
    ) -> None:
        """Record the RTT of the outstanding ping ``ping_id`` answers, and the
        clock-offset exchange when the Pong carried the agent's wall clock."""
        outstanding = live.outstanding_ping
        if outstanding is None or outstanding[0] != ping_id:
            log.debug(
                "session.pong_unmatched",
                node_id=live.node_id,
                session_id=live.session_id,
                ping_id=ping_id,
            )
            return
        rtt_ms = (self._clock() - outstanding[1]) * 1000.0
        sent_wall_ms = live.outstanding_ping_wall_ms
        live.outstanding_ping = None
        live.outstanding_ping_wall_ms = None
        live.last_rtt_ms = rtt_ms
        live.rtt_ewma_ms = update_ewma(live.rtt_ewma_ms, rtt_ms)
        live.pongs_matched += 1
        log.debug(
            "session.rtt",
            node_id=live.node_id,
            session_id=live.session_id,
            rtt_ms=round(rtt_ms, 3),
            rtt_ewma_ms=round(live.rtt_ewma_ms, 3),
        )
        change: ClockChange | None = None
        if agent_ms is not None and sent_wall_ms is not None:
            change = live.clock.observe(
                sent_wall_ms=sent_wall_ms,
                rtt_ms=rtt_ms,
                agent_ms=agent_ms,
                agent_resolution_ms=resolution_ms(clock_resolution_us),
                local_resolution_ms=_LOCAL_RESOLUTION_MS,
            )
        estimate = live.clock.latest if agent_ms is not None else None
        record = functools.partial(
            self._registry.record_rtt,
            live.session_id,
            last_ms=rtt_ms,
            ewma_ms=live.rtt_ewma_ms,
        )
        if estimate is not None:
            record = functools.partial(record, clock=estimate)
        live.writer.enqueue("record_rtt", WriteKind.RTT, record)
        if change is not None:
            self._clock_changed(live, change)

    def _clock_changed(self, live: LiveSession, change: ClockChange) -> None:
        """Log the flip; on entering the uncertain state also queue the audit
        row and, once it is written, the live alert (one per episode)."""
        clock = change.clock
        fields = {
            "node_id": live.node_id,
            "session_id": live.session_id,
            "clock_offset_ms": round(clock.offset_ms, 3),
            "clock_offset_bound_ms": round(clock.bound_ms, 3),
            "consistent": clock.consistent,
            "exchanges": clock.exchanges,
        }
        if not change.uncertain:
            log.info("session.clock_certain", **fields)
            return
        log.warning("session.clock_uncertain", reasons=list(change.reasons), **fields)

        async def write_alert() -> None:
            alert_id = await self._registry.record_clock_alert(
                live.node_id,
                offset_ms=clock.offset_ms,
                bound_ms=clock.bound_ms,
                consistent=clock.consistent,
                reasons=change.reasons,
            )
            publish = self.on_clock_alert
            if publish is None:
                return
            try:
                publish(
                    {
                        "id": str(alert_id),
                        "kind": "clock_uncertain",
                        "node_id": live.node_id,
                        "offset_ms": f"{clock.offset_ms:.3f}",
                        "bound_ms": f"{clock.bound_ms:.3f}",
                        "reasons": ",".join(change.reasons),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - an alert never breaks a session
                log.warning("session.clock_alert_failed", cause=type(exc).__name__)

        live.writer.enqueue("record_clock_alert", WriteKind.ALERT, write_alert)
