"""The agent-payload dispatch table of the session layer (D42).

``SessionManager.handle_message`` does the envelope work every payload shares -
stale ``session_id`` drop, ``message_id`` shape, dedupe, the Ack - and then calls
the one handler registered here for the payload's ``oneof`` name. A payload with
no handler goes to the table's *fallback* (V0.3: logged at debug, still acked),
so a later version adds a payload by **registering** a handler (V0.4 G4:
``attempt_status``, ``log_chunk``) instead of editing the manager's control flow.

Registration is checked: the name must be a real ``AgentMessage.payload`` field,
``hello`` is never dispatchable (admission owns it; a second Hello is a protocol
error), and a name can be registered once - a silent replacement would make the
order of imports decide which handler runs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Final

from meshforge.common.errors import InvalidIdError
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.session.errors import SessionProtocolError
from meshforge.controller.session.limits import validate_message_id
from meshforge.controller.session.live import LiveSession

__all__ = [
    "AGENT_PAYLOADS",
    "DispatchTable",
    "Handler",
    "check_envelope",
    "log_ack",
    "log_unhandled",
]

log = get_logger(__name__)

type Handler = Callable[[LiveSession, agent_pb2.AgentMessage], Awaitable[None]]
"""``(live, msg) -> None``; runs on the session's reader task, must not block on
the database (enqueue on ``live.writer`` instead)."""

AGENT_PAYLOADS: Final[frozenset[str]] = frozenset(
    f.name for f in agent_pb2.AgentMessage.DESCRIPTOR.oneofs_by_name["payload"].fields
)
"""Every ``AgentMessage.payload`` field name."""

_NOT_DISPATCHABLE: Final[frozenset[str]] = frozenset({"hello"})


class DispatchTable:
    """payload name -> handler, with a fallback for unregistered payloads."""

    __slots__ = ("_fallback", "_handlers")

    def __init__(self, fallback: Handler) -> None:
        self._fallback = fallback
        self._handlers: dict[str, Handler] = {}

    def register(self, payload: str, handler: Handler) -> None:
        """Route ``payload`` to ``handler``; ValueError if it cannot be routed."""
        if payload not in AGENT_PAYLOADS:
            raise ValueError(f"{payload!r} is not an AgentMessage payload")
        if payload in _NOT_DISPATCHABLE:
            raise ValueError(f"{payload!r} is handled by admission, not dispatch")
        if payload in self._handlers:
            raise ValueError(f"a handler for {payload!r} is already registered")
        self._handlers[payload] = handler

    def handler_for(self, payload: str) -> Handler:
        return self._handlers.get(payload, self._fallback)

    def registered(self) -> frozenset[str]:
        return frozenset(self._handlers)


def check_envelope(live: LiveSession, msg: agent_pb2.AgentMessage) -> str | None:
    """The payload name of a well-formed message for ``live``; ``None`` = stale.

    A message carrying another session's id is stale (the caller drops and
    counts it). A second Hello, a message without payload, or a missing or
    malformed ``message_id`` breaks the contract: :class:`SessionProtocolError`.
    """
    which = msg.WhichOneof("payload")
    if which == "hello":
        raise SessionProtocolError(
            "second Hello on an open stream",
            session_id=live.session_id,
            node_id=live.node_id,
        )
    if msg.session_id != live.session_id:
        return None
    if which is None:
        raise SessionProtocolError(
            "message without payload", session_id=live.session_id, node_id=live.node_id
        )
    if not msg.message_id:
        raise SessionProtocolError(
            f"{which} message without message_id",
            session_id=live.session_id,
            node_id=live.node_id,
        )
    try:
        validate_message_id(msg.message_id)
    except InvalidIdError as exc:
        # Bounds the dedupe window in bytes, not only in count (B3): the
        # value itself is never logged or kept.
        raise SessionProtocolError(
            f"{which} message: {exc}",
            session_id=live.session_id,
            node_id=live.node_id,
        ) from None
    return which


async def log_ack(live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
    """``ack``: the agent received a command (logged; commands arrive in V0.4)."""
    log.debug(
        "session.command_acked",
        node_id=live.node_id,
        session_id=live.session_id,
        acked_message_id=msg.ack.acked_message_id,
    )


async def log_unhandled(live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
    """The fallback: accepted and acked, no effect yet (``cache_report``, ...);
    the versions that own a payload register its handler."""
    log.debug(
        "session.message",
        node_id=live.node_id,
        session_id=live.session_id,
        payload=msg.WhichOneof("payload"),
    )
