"""Typed failures of the session layer."""

from __future__ import annotations

from meshforge.common.errors import FailureType, MeshForgeError

__all__ = ["OutboundOverflowError", "SessionProtocolError"]


class SessionProtocolError(MeshForgeError):
    """The agent broke the session contract (second Hello, empty ``message_id``...).

    The session is closed with ``end_reason = protocol_error``.
    """

    default_failure_type = FailureType.CONFLICT

    def __init__(self, message: str, *, session_id: str, node_id: str) -> None:
        super().__init__(
            message, details={"session_id": session_id, "node_id": node_id}
        )


class OutboundOverflowError(SessionProtocolError):
    """The per-session controller->agent queue is full: the agent is not reading.

    Never a silent drop - the session is closed (``protocol_error``) and the agent
    reconnects with a Reconcile.
    """

    default_failure_type = FailureType.RESOURCE_BUSY

    def __init__(self, *, session_id: str, node_id: str, capacity: int) -> None:
        super().__init__(
            f"outbound queue full ({capacity} messages); agent is not reading",
            session_id=session_id,
            node_id=node_id,
        )
        self.details["capacity"] = capacity
