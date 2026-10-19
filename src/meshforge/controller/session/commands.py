"""Controller -> agent messages: commands, Acks and job-layer hooks (V0.4 D64).

Split out of ``manager.py`` (D77). :class:`CommandChannel` owns everything the
controller *writes* to a session's one outbound queue - the gRPC handler task
stays the only writer of the stream:

* :meth:`CommandChannel.send` queues any ``ControllerMessage`` (Ping, Ack,
  command); a full queue raises :class:`~.errors.OutboundOverflowError`.
* :meth:`CommandChannel.send_command` sends a command and remembers it as
  awaiting its Ack (bounded by :data:`~.live.MAX_AWAITING_ACK`); the agent's
  ``Ack`` resolves it and fires ``SessionHooks.on_command_acked``. **Re-send
  on a missing Ack (V0.6 G1, D113):** a command in :data:`RESEND_ON_TIMEOUT`
  (``gc``, ``prepare_env``, ``probe_request``, ``revocation_update`` - V0.7
  G3, D138) not acked ``command_resend_s``
  after it was sent is queued again - the **same envelope, same
  ``message_id``**, so an agent that applied it but whose Ack was lost drops
  the copy and acks again - at most ``command_resend_tries`` times, then logged
  ``session.command_unacked`` (kept waiting: a late Ack still resolves it).
  The lease timer drives it (:meth:`CommandChannel.resend_due`). Attempt
  commands are never re-sent by a timer: Reconcile decides what still matters
  for an attempt (D66) and re-sends with a fresh envelope. Within one stream
  nothing is lost; a stream that ends takes its pending commands with it
  (the next session's Hello/Reconcile re-sends pins, prepares and attempts).
  A full outbound queue means the agent is not reading: the session is ended
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

__all__ = ["RESEND_ON_TIMEOUT", "STREAMED", "UNACKED", "CommandChannel"]

log = get_logger(__name__)

UNACKED: Final = frozenset(
    {"ack", "pong", "goodbye", "log_chunk", "chunk_request", "lease_renew"}
)
"""Agent payloads that get no Ack back (ADR-0002 Updates §4: never buffered;
V0.10 G4: a lane's ChunkRequest / LeaseRenew are simply sent again)."""
RESEND_ON_TIMEOUT: Final = frozenset(
    {"gc", "prepare_env", "probe_request", "revocation_update"}
)
"""Commands the controller re-sends itself on a missing Ack (D113); attempt
commands are re-sent by Reconcile instead (D66)."""
STREAMED: Final = frozenset({"log_chunk"})
"""Best-effort payloads (V0.4 D71): no ``message_id`` dedupe (offsets dedupe
them; a log flood must not churn the dedupe window of reports), no Ack."""


class CommandChannel:
    """Send commands and Acks, resolve Acks, call hooks (see module docstring)."""

    def __init__(
        self,
        *,
        live: dict[str, LiveSession],
        close: Callable[[LiveSession, SessionEndReason], None],
        closing: Callable[[], bool],
        queue_size: int,
        clock: Callable[[], float] | None = None,
        resend_s: float = 10.0,
        resend_tries: int = 5,
    ) -> None:
        if not resend_s > 0 or resend_tries < 0:
            raise ValueError("resend_s must be > 0 and resend_tries >= 0")
        self._live = live
        self._close = close
        self._closing = closing
        self._queue_size = queue_size
        self._clock = clock or (lambda: 0.0)
        self.resend_s = resend_s
        self.resend_tries = resend_tries
        self.resent = 0
        self.unacked = 0
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
            payload_name,
            attempt_id,
            fencing_token,
            message=msg if payload_name in RESEND_ON_TIMEOUT else None,
            sent_at=self._clock(),
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

    def resend_due(self, live: LiveSession, now: float) -> float | None:
        """D113: re-send every :data:`RESEND_ON_TIMEOUT` command of ``live`` not
        acked ``resend_s`` after its last send (same envelope); give up after
        ``resend_tries`` re-sends. Returns the next time one is due (None =
        nothing waiting). Called by the lease timer."""
        if live.ended:
            return None
        next_due: float | None = None
        for message_id, pending in list(live.awaiting_ack.items()):
            if pending.message is None:
                continue
            due = pending.sent_at + self.resend_s
            if now < due:
                next_due = due if next_due is None else min(next_due, due)
                continue
            if pending.sends > self.resend_tries:
                pending.message = None  # stop; a late Ack still resolves it
                self.unacked += 1
                log.warning(
                    "session.command_unacked",
                    node_id=live.node_id,
                    session_id=live.session_id,
                    message_id=message_id,
                    payload=pending.payload,
                    sends=pending.sends,
                )
                continue
            try:
                self.send(live, pending.message)
            except OutboundOverflowError:
                self.overflowed(live)
                return None
            pending.sends += 1
            pending.sent_at = now
            self.resent += 1
            live.commands_resent += 1
            log.info(
                "session.command_resent",
                node_id=live.node_id,
                session_id=live.session_id,
                message_id=message_id,
                payload=pending.payload,
                sends=pending.sends,
            )
            due = now + self.resend_s
            next_due = due if next_due is None else min(next_due, due)
        return next_due

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
