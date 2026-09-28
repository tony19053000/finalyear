"""Controller -> agent messages: commands, Acks and job-layer hooks (V0.4 D64).

Split out of ``manager.py`` (D77). :class:`CommandChannel` owns everything the
controller *writes* to a session's one outbound queue - the gRPC handler task
stays the only writer of the stream:

* :meth:`CommandChannel.send` queues any ``ControllerMessage`` (Ping, Ack,
  command); a full queue raises :class:`~.errors.OutboundOverflowError`.
* :meth:`CommandChannel.send_command` sends a command and remembers it as
  awaiting its Ack (bounded by :data:`~.live.MAX_AWAITING_ACK`); the agent's
  ``Ack`` resolves it and fires ``SessionHooks.on_command_acked``. There is no
  generic retry layer: the attempt logic decides what to re-send (D66). A full
  outbound queue means the agent is not reading: the session is ended
  ``protocol_error`` (the agent reconnects and reconciles, which re-sends
  whatever still matters).
* **Deferred Acks.** A payload registered with :meth:`CommandChannel.defer_ack`
  (``attempt_status``) is not acked by the manager: its handler acks it with
  :meth:`CommandChannel.ack_message` once it is **persisted**, so an Ack always
  means "stored" and an unacked report is replayed after a reconnect. Such a
  payload reaches its (idempotent) handler even when its ``message_id`` was seen
  before - acking a duplicate before the first copy is stored would lose it.
* The session -> job-layer hooks (:mod:`.hooks`): every hook is called
  synchronously and a hook that raises is logged and ignored.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Final

from meshforge.common.ids import new_command_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import LivenessEvent
from meshforge.controller.registry import SessionEndReason
from meshforge.controller.session.dispatch import log_ack
from meshforge.controller.session.errors import OutboundOverflowError
from meshforge.controller.session.hooks import SessionHooks
from meshforge.controller.session.live import (
    MAX_AWAITING_ACK,
    LiveSession,
    PendingCommand,
)

__all__ = ["UNACKED", "CommandChannel"]

log = get_logger(__name__)

UNACKED: Final = frozenset({"ack", "pong", "goodbye"})
"""Agent payloads that get no Ack back (ADR-0002 Updates §4: never buffered)."""


class CommandChannel:
    """Send commands and Acks, resolve Acks, call hooks (see module docstring)."""

    def __init__(
        self,
        *,
        live: dict[str, LiveSession],
        close: Callable[[LiveSession, SessionEndReason], None],
        closing: Callable[[], bool],
        queue_size: int,
    ) -> None:
        self._live = live
        self._close = close
        self._closing = closing
        self._queue_size = queue_size
        self.hooks = SessionHooks()
        self.deferred: set[str] = set()
        """Payloads whose handler acks them once stored (:meth:`defer_ack`)."""

    # ---------------------------------------------------------------- output

    def send(self, live: LiveSession, msg: agent_pb2.ControllerMessage) -> None:
        """Queue a controller->agent message. Raises :class:`OutboundOverflowError`."""
        if live.ended:
            return
        try:
            live.outbound.put_nowait(msg)
        except asyncio.QueueFull:
            raise OutboundOverflowError(
                session_id=live.session_id,
                node_id=live.node_id,
                capacity=self._queue_size,
            ) from None

    def ack(self, live: LiveSession, acked: str) -> None:
        """Queue an Ack for agent message ``acked`` (raises on overflow)."""
        self.send(
            live,
            agent_pb2.ControllerMessage(
                message_id=new_command_id(),
                session_id=live.session_id,
                ack=agent_pb2.Ack(acked_message_id=acked),
            ),
        )

    def defer_ack(self, payload: str) -> None:
        """Let ``payload``'s handler ack it (after persisting it)."""
        if payload in UNACKED:
            raise ValueError(f"{payload!r} is never acked")
        self.deferred.add(payload)

    def ack_message(self, live: LiveSession, message_id: str) -> bool:
        """Ack an agent message now (a deferred Ack). False if the session ended
        or the Ack could not be queued (the agent then replays the report)."""
        if live.ended:
            return False
        try:
            self.ack(live, message_id)
        except OutboundOverflowError:
            self.overflowed(live)
            return False
        return True

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
            self.send(live, msg)
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

    # ----------------------------------------------------------------- input

    async def on_ack(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        """``ack``: the agent received a command; resolves it (D64)."""
        acked = msg.ack.acked_message_id
        pending = live.awaiting_ack.pop(acked, None)
        await log_ack(live, msg)
        if pending is None:
            return
        live.commands_acked += 1
        self.call_hook("on_command_acked", live, acked, pending)

    def on_reconcile(self, live: LiveSession, reconcile: agent_pb2.Reconcile) -> None:
        self.call_hook("on_reconcile", live, reconcile)

    def on_node_offline(self, live: LiveSession, event: LivenessEvent) -> None:
        if not self._closing():  # a controller shutdown is not the agent leaving
            self.call_hook("on_node_offline", live, event)

    def call_hook(self, name: str, *args: Any) -> None:
        hook = getattr(self.hooks, name)
        if hook is None:
            return
        try:
            hook(*args)
        except Exception:  # a job-layer bug must never break a session
            log.exception("session.hook_failed", hook=name)
