"""Node liveness: the §12.3 state machine and the lease timer, as pure logic.

Roadmap task 0.3.7 (logic half). No asyncio, no sockets, no wall clock: the
session manager (:mod:`meshforge.controller.session`) owns the timers and the DB
row and calls into this module with its monotonic ``loop.time()``. Keeping the
rules pure makes every (state, event) pair and every lease edge a table-driven
unit test.

Timing (ADR-0002 Updates §5, ARCHITECTURE §12.3)::

    heartbeat_interval_s = 2.0     agent sends a Heartbeat every 2 s
    suspect_after_s      = 2.5     one missed heartbeat + 0.5 s jitter allowance
    offline_after_s      = 6.0     lease = 3 x heartbeat (criterion 2: <= 8 s)

:class:`LeaseTracker` turns "silence since the last heartbeat" into at most one
``LEASE_MISSED`` and at most one ``LEASE_EXPIRED`` per silence; a heartbeat resets
it. :func:`next_state` applies the §12.3 arrows and rejects everything else with
:class:`InvalidTransitionError`, so a bug in the caller cannot invent a transition.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = [
    "InvalidTransitionError",
    "LeaseTracker",
    "LivenessConfig",
    "LivenessEvent",
    "NodeState",
    "next_state",
]


class NodeState(StrEnum):
    """Node liveness states, exactly ARCHITECTURE §12.3."""

    ENROLLING = "enrolling"
    ONLINE = "online"
    SUSPECT = "suspect"
    OFFLINE = "offline"
    DRAINING = "draining"
    REVOKED = "revoked"


class LivenessEvent(StrEnum):
    """Inputs to :func:`next_state`."""

    HELLO_ACCEPTED = "hello_accepted"
    HEARTBEAT = "heartbeat"
    LEASE_MISSED = "lease_missed"
    LEASE_EXPIRED = "lease_expired"
    RECONNECTED = "reconnected"
    DRAIN = "drain"
    UNDRAIN = "undrain"
    REVOKE = "revoke"


class InvalidTransitionError(MeshForgeError):
    """``event`` is not an arrow out of ``state`` in §12.3."""

    default_failure_type = FailureType.CONFLICT

    def __init__(self, state: NodeState, event: LivenessEvent) -> None:
        super().__init__(
            f"no transition from {state.value} on {event.value}",
            details={"state": state.value, "event": event.value},
        )
        self.state = state
        self.event = event


#: The §12.3 arrows (as amended 2026-09-20). Anything not listed raises
#: InvalidTransitionError.
_TRANSITIONS: Final[dict[tuple[NodeState, LivenessEvent], NodeState]] = {
    (NodeState.ENROLLING, LivenessEvent.HELLO_ACCEPTED): NodeState.ONLINE,
    (NodeState.ONLINE, LivenessEvent.HEARTBEAT): NodeState.ONLINE,  # self-loop
    (NodeState.ONLINE, LivenessEvent.LEASE_MISSED): NodeState.SUSPECT,
    (NodeState.SUSPECT, LivenessEvent.HEARTBEAT): NodeState.ONLINE,
    (NodeState.SUSPECT, LivenessEvent.LEASE_EXPIRED): NodeState.OFFLINE,
    # A Hello while SUSPECT (agent reconnected before the lease expired) is the
    # same arrow as the reconnect from OFFLINE.
    (NodeState.SUSPECT, LivenessEvent.RECONNECTED): NodeState.ONLINE,
    (NodeState.OFFLINE, LivenessEvent.RECONNECTED): NodeState.ONLINE,
    (NodeState.ONLINE, LivenessEvent.DRAIN): NodeState.DRAINING,
    (NodeState.DRAINING, LivenessEvent.UNDRAIN): NodeState.ONLINE,
    # A draining node can still lose its lease; the loss cancels the drain.
    (NodeState.DRAINING, LivenessEvent.LEASE_MISSED): NodeState.SUSPECT,
    # Revocation is possible from every live state.
    (NodeState.ONLINE, LivenessEvent.REVOKE): NodeState.REVOKED,
    (NodeState.SUSPECT, LivenessEvent.REVOKE): NodeState.REVOKED,
    (NodeState.OFFLINE, LivenessEvent.REVOKE): NodeState.REVOKED,
    (NodeState.DRAINING, LivenessEvent.REVOKE): NodeState.REVOKED,
}


def next_state(state: NodeState, event: LivenessEvent) -> NodeState:
    """Pure §12.3 transition. Raises :class:`InvalidTransitionError` for a non-arrow."""
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidTransitionError(state, event) from None


@dataclass(frozen=True, slots=True)
class LivenessConfig:
    """Heartbeat/lease timing. All seconds; defaults from ADR-0002 Updates §5.

    Settings-backed in 0.3.6 (``ControllerSettings.liveness``); the agent receives
    ``heartbeat_interval_s`` in ``HelloAck.heartbeat_interval_ms``.
    """

    heartbeat_interval_s: float = 2.0
    miss_allowance_s: float = 0.5
    """Extra silence tolerated beyond one interval before SUSPECT (jitter, GC pauses)."""
    lease_multiplier: int = 3

    def __post_init__(self) -> None:
        if not self.heartbeat_interval_s > 0:
            raise ValueError("heartbeat_interval_s must be > 0")
        if not self.miss_allowance_s >= 0:
            raise ValueError("miss_allowance_s must be >= 0")
        if not self.lease_multiplier > 0:
            raise ValueError("lease_multiplier must be > 0")
        if not self.offline_after_s > self.suspect_after_s:
            raise ValueError(
                f"offline_after_s ({self.offline_after_s}) must be greater than "
                f"suspect_after_s ({self.suspect_after_s})"
            )

    @property
    def suspect_after_s(self) -> float:
        """Silence after which the node becomes SUSPECT (one missed heartbeat)."""
        return self.heartbeat_interval_s + self.miss_allowance_s

    @property
    def offline_after_s(self) -> float:
        """Silence after which the lease is expired and the node is OFFLINE."""
        return self.heartbeat_interval_s * self.lease_multiplier


class LeaseTracker:
    """Per-session lease on an injected **monotonic** clock.

    ``clock`` is only used when a caller omits ``now``; every method also accepts
    an explicit ``now`` so tests and the session manager can drive it directly.
    Each silence yields ``LEASE_MISSED`` at most once (when silence reaches
    ``suspect_after_s``) and ``LEASE_EXPIRED`` at most once (when silence reaches
    ``offline_after_s``); a heartbeat starts a new silence.
    """

    __slots__ = (
        "_cfg",
        "_clock",
        "_expired_emitted",
        "_last_heartbeat",
        "_missed_emitted",
    )

    def __init__(self, cfg: LivenessConfig, clock: Callable[[], float]) -> None:
        self._cfg = cfg
        self._clock = clock
        self._last_heartbeat: float = clock()
        self._missed_emitted = False
        self._expired_emitted = False

    @property
    def config(self) -> LivenessConfig:
        return self._cfg

    @property
    def last_heartbeat(self) -> float:
        """Monotonic time of the last observed heartbeat (or of construction)."""
        return self._last_heartbeat

    def observe_heartbeat(self, now: float | None = None) -> None:
        """Record a heartbeat; resets both one-shot events."""
        now = self._clock() if now is None else now
        # Monotonic clocks never go backwards; guard anyway so a caller passing a
        # stale timestamp cannot move the lease into the past.
        self._last_heartbeat = max(now, self._last_heartbeat)
        self._missed_emitted = False
        self._expired_emitted = False

    def silence(self, now: float | None = None) -> float:
        """Seconds since the last heartbeat."""
        now = self._clock() if now is None else now
        return max(0.0, now - self._last_heartbeat)

    def check(self, now: float | None = None) -> LivenessEvent | None:
        """Return the lease event due at ``now``, or None. Idempotent per silence."""
        silence = self.silence(now)
        if silence >= self._cfg.offline_after_s and not self._expired_emitted:
            # A tracker that was never polled during the SUSPECT window still
            # reports the miss first, so the state machine walks ONLINE -> SUSPECT
            # -> OFFLINE and never skips an arrow.
            if not self._missed_emitted:
                self._missed_emitted = True
                return LivenessEvent.LEASE_MISSED
            self._expired_emitted = True
            return LivenessEvent.LEASE_EXPIRED
        # ">=" on both edges so a timer that fires exactly at deadline() sees the
        # event instead of spinning on an unchanged deadline.
        if silence >= self._cfg.suspect_after_s and not self._missed_emitted:
            self._missed_emitted = True
            return LivenessEvent.LEASE_MISSED
        return None

    def deadline(self) -> float | None:
        """Earliest monotonic time at which :meth:`check` could return an event.

        None once both events have fired (nothing left to wait for until the next
        heartbeat). A deadline already in the past means "check now".
        """
        if not self._missed_emitted:
            return self._last_heartbeat + self._cfg.suspect_after_s
        if not self._expired_emitted:
            return self._last_heartbeat + self._cfg.offline_after_s
        return None
