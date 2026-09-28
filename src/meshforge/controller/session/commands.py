"""Controller -> agent commands and their Acks (V0.4 G4, D64).

:class:`CommandChannel` sends a command on a node's live session - through the
session's one outbound queue, so the gRPC handler task stays the only writer of
the stream - and remembers it as awaiting its Ack (bounded by
:data:`~.live.MAX_AWAITING_ACK`). The agent's ``Ack`` resolves it and fires
``SessionHooks.on_command_acked``. There is no generic retry layer: the attempt
logic decides what to re-send (D66). A full outbound queue means the agent is not
reading: the session is ended ``protocol_error`` (the agent reconnects and
reconciles, which re-sends whatever still matters).

It also carries the other session -> job-layer hooks (:mod:`.hooks`): every hook
is called synchronously and a hook that raises is logged and ignored.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from meshforge.common.ids import new_command_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.dispatch import log_ack
from meshforge.controller.session.errors import OutboundOverflowError
from meshforge.controller.session.hooks import SessionHooks
from meshforge.controller.session.live import (
    MAX_AWAITING_ACK,
    LiveSession,
    PendingCommand,
)

__all__ = ["CommandChannel"]

log = get_logger(__name__)


class CommandChannel:
    """Send commands, resolve Acks, call hooks (see module docstring)."""

    def __init__(
        self,
        *,
        live: dict[str, LiveSession],
        send: Callable[[LiveSession, agent_pb2.ControllerMessage], None],
        close: Callable[[LiveSession, SessionEndReason], None],
        queue_size: int,
    ) -> None:
        self._live = live
        self._send = send
        self._close = close
        self._queue_size = queue_size
        self.hooks = SessionHooks()

    def send_command(
        self,
        node_id: str,
        payload_name: str,
        payload: Any,
        *,
        attempt_id: str = "",
        fencing_token: int = 0,
    ) -> str | None:
        """Send one command to ``node_id``'s live session; its ``message_id``, or
        ``None`` (no live session, or the queue was full and the session ended)."""
        live = self._live.get(node_id)
        if live is None or live.ended:
            return None
        msg = agent_pb2.ControllerMessage(
            message_id=new_command_id(), session_id=live.session_id
        )
        getattr(msg, payload_name).CopyFrom(payload)
        try:
            self._send(live, msg)
        except OutboundOverflowError:
            self.overflowed(live)
            return None
        if len(live.awaiting_ack) >= MAX_AWAITING_ACK:
            live.awaiting_ack.pop(next(iter(live.awaiting_ack)))
        live.awaiting_ack[msg.message_id] = PendingCommand(
            payload_name, attempt_id, fencing_token
        )
        live.commands_sent += 1
        log.info(
            "session.command_sent",
            node_id=node_id,
            session_id=live.session_id,
            message_id=msg.message_id,
            payload=payload_name,
            attempt_id=attempt_id or None,
        )
        return msg.message_id

    def overflowed(self, live: LiveSession) -> None:
        log.warning(
            "session.outbound_overflow",
            node_id=live.node_id,
            session_id=live.session_id,
            capacity=self._queue_size,
        )
        self._close(live, SessionEndReason.PROTOCOL_ERROR)

    async def on_ack(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        """``ack``: the agent received a command; resolves it (D64)."""
        acked = msg.ack.acked_message_id
        pending = live.awaiting_ack.pop(acked, None)
        await log_ack(live, msg)
        if pending is None:
            return
        live.commands_acked += 1
        self.call_hook("on_command_acked", live, acked, pending)

    def call_hook(self, name: str, *args: Any) -> None:
        hook = getattr(self.hooks, name)
        if hook is None:
            return
        try:
            hook(*args)
        except Exception:  # a job-layer bug must never break a session
            log.exception("session.hook_failed", hook=name)
