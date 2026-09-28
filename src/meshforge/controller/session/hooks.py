"""Callbacks from the session layer to the job layer (V0.4 G4, D64/D66).

The session layer knows streams and liveness; the job layer (the dispatcher)
knows attempts. Rather than importing the job layer, the
:class:`~.manager.SessionManager` calls these hooks - **synchronously, on the
reader task or the lease timer**, so a hook must never block or await the
database: it records the event (e.g. on its own queue) and returns. A hook that
raises is logged and ignored; it can never break a session.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessEvent
from meshforge.controller.session.live import LiveSession, PendingCommand

__all__ = ["SessionHooks"]


@dataclass(slots=True)
class SessionHooks:
    on_command_acked: Callable[[LiveSession, str, PendingCommand], None] | None = None
    """The agent acked a command this controller sent (``message_id``)."""
    on_reconcile: Callable[[LiveSession, agent_pb2.Reconcile], None] | None = None
    """A session's ``Reconcile`` arrived (its first message after HelloAck)."""
    on_node_offline: Callable[[LiveSession, LivenessEvent], None] | None = None
    """A node went OFFLINE because of the agent (lease expired, Goodbye) - never
    for a controller shutdown, never for SUSPECT (D66)."""
