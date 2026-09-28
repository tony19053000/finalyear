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

Nothing here awaits the database: writes go on the session's
:class:`~.writer.WriteChain` as droppable kinds (a later one supersedes them).
"""

from __future__ import annotations

import functools
from collections.abc import Callable

from meshforge.common.ids import new_command_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.registry import Registry
from meshforge.controller.session.live import LiveSession, update_ewma
from meshforge.controller.session.writer import WriteKind

__all__ = ["HeartbeatRecorder"]

log = get_logger(__name__)


class HeartbeatRecorder:
    """Coalesced ``touch_node`` writes and RTT samples (see module docstring)."""

    def __init__(
        self,
        registry: Registry,
        liveness: LivenessConfig,
        *,
        clock: Callable[[], float],
    ) -> None:
        self._registry = registry
        self._liveness = liveness
        self._clock = clock

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
        self.on_pong(live, msg.pong.ping_id)

    def on_pong(self, live: LiveSession, ping_id: str) -> None:
        """Record the RTT of the outstanding ping ``ping_id`` answers."""
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
        live.outstanding_ping = None
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
        live.writer.enqueue(
            "record_rtt",
            WriteKind.RTT,
            functools.partial(
                self._registry.record_rtt,
                live.session_id,
                last_ms=rtt_ms,
                ewma_ms=live.rtt_ewma_ms,
            ),
        )
