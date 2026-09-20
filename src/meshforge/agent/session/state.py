"""Agent-side session state machine (roadmap 0.3.8) - pure, like the controller's
``liveness.next_state``.

::

    DISCOVERING --controller_found--> CONNECTING --stream_opened--> HELLO_SENT
    HELLO_SENT --hello_accepted--> ONLINE
    HELLO_SENT --hello_retry-----> CONNECTING      (STALE_EPOCH: bump epoch, retry at once)
    HELLO_SENT --hello_rejected--> BACKOFF          (INTERNAL / DUPLICATE_SESSION / timeout)
    {CONNECTING, HELLO_SENT, ONLINE} --stream_ended--> BACKOFF
    DISCOVERING --discovery_failed--> BACKOFF
    BACKOFF --backoff_elapsed--> DISCOVERING        (a fixed address resolves instantly)
    any live state --stop_requested / fatal--> STOPPING --stopped--> STOPPED

``DISCOVERING`` is the "resolve the controller address" step; with a configured
``controller_address`` it completes immediately, with mDNS it browses. Putting the
resolve step in the loop means a controller that moved to another laptop is found
again on the next cycle without an agent restart.

Nothing here touches time, sockets or logs: the client applies the table and logs
``agent.session.state_changed``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = [
    "LIVE_STATES",
    "TERMINAL_STATES",
    "AgentEvent",
    "AgentState",
    "InvalidAgentTransitionError",
    "next_state",
]


class AgentState(StrEnum):
    DISCOVERING = "DISCOVERING"
    """Resolving the controller address (config or mDNS)."""
    CONNECTING = "CONNECTING"
    """Epoch bumped, channel opening, Hello about to be written."""
    HELLO_SENT = "HELLO_SENT"
    """Hello written; waiting for HelloAck."""
    ONLINE = "ONLINE"
    """HelloAck accepted; heartbeating."""
    BACKOFF = "BACKOFF"
    """Sleeping before the next cycle (full-jitter exponential)."""
    STOPPING = "STOPPING"
    """Stop requested or fatal error; tearing the stream down."""
    STOPPED = "STOPPED"
    """Terminal."""


class AgentEvent(StrEnum):
    CONTROLLER_FOUND = "controller_found"
    DISCOVERY_FAILED = "discovery_failed"
    STREAM_OPENED = "stream_opened"
    HELLO_ACCEPTED = "hello_accepted"
    HELLO_RETRY = "hello_retry"
    HELLO_REJECTED = "hello_rejected"
    STREAM_ENDED = "stream_ended"
    BACKOFF_ELAPSED = "backoff_elapsed"
    STOP_REQUESTED = "stop_requested"
    FATAL = "fatal"
    STOPPED = "stopped"


LIVE_STATES: Final[frozenset[AgentState]] = frozenset(
    {
        AgentState.DISCOVERING,
        AgentState.CONNECTING,
        AgentState.HELLO_SENT,
        AgentState.ONLINE,
        AgentState.BACKOFF,
    }
)
TERMINAL_STATES: Final[frozenset[AgentState]] = frozenset({AgentState.STOPPED})


class InvalidAgentTransitionError(MeshForgeError):
    """``event`` is not defined for ``state`` - a programming error, never a peer's."""

    default_failure_type = FailureType.INTERNAL_ERROR

    def __init__(self, state: AgentState, event: AgentEvent) -> None:
        super().__init__(
            f"no agent transition for {event.value!r} in state {state.value}",
            details={"state": state.value, "event": event.value},
        )
        self.state = state
        self.event = event


_TABLE: Final[dict[tuple[AgentState, AgentEvent], AgentState]] = {
    (AgentState.DISCOVERING, AgentEvent.CONTROLLER_FOUND): AgentState.CONNECTING,
    (AgentState.DISCOVERING, AgentEvent.DISCOVERY_FAILED): AgentState.BACKOFF,
    (AgentState.CONNECTING, AgentEvent.STREAM_OPENED): AgentState.HELLO_SENT,
    (AgentState.CONNECTING, AgentEvent.STREAM_ENDED): AgentState.BACKOFF,
    (AgentState.HELLO_SENT, AgentEvent.HELLO_ACCEPTED): AgentState.ONLINE,
    (AgentState.HELLO_SENT, AgentEvent.HELLO_RETRY): AgentState.CONNECTING,
    (AgentState.HELLO_SENT, AgentEvent.HELLO_REJECTED): AgentState.BACKOFF,
    (AgentState.HELLO_SENT, AgentEvent.STREAM_ENDED): AgentState.BACKOFF,
    (AgentState.ONLINE, AgentEvent.STREAM_ENDED): AgentState.BACKOFF,
    (AgentState.BACKOFF, AgentEvent.BACKOFF_ELAPSED): AgentState.DISCOVERING,
    (AgentState.STOPPING, AgentEvent.STOPPED): AgentState.STOPPED,
}
for _live in LIVE_STATES:
    _TABLE[(_live, AgentEvent.STOP_REQUESTED)] = AgentState.STOPPING
    _TABLE[(_live, AgentEvent.FATAL)] = AgentState.STOPPING


def next_state(state: AgentState, event: AgentEvent) -> AgentState:
    """The table above; raises :class:`InvalidAgentTransitionError` for any other pair."""
    try:
        return _TABLE[(state, event)]
    except KeyError:
        raise InvalidAgentTransitionError(state, event) from None
