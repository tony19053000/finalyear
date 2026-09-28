"""How a session ends (D19/D20/D28/D40/D41; split out of ``manager.py``, D42).

Every end goes through :meth:`SessionEnds.close` - idempotent, first reason wins -
which does the in-memory half at once (unregister, unblock the gRPC handler, stop
the reader) and then either **defers** the persistent half to a pending Hello that
owns the node's state (D28, :mod:`.claims`) or **finishes** it: the end's §12.3
event (:func:`~.live.end_event`), then ``close_session`` on the session's write
chain. An abrupt end that leaves the node SUSPECT **lingers**: the entry keeps its
lease tracker, the lease timer calls :meth:`SessionEnds.expire_linger`, and
``LEASE_EXPIRED -> OFFLINE`` is written at the normal edge unless a Hello ends the
linger first (:meth:`SessionEnds.end_linger`). Every state write is fenced on the
session id (D20) - and on the entry's ``fence_ids`` (D41).

Nothing here awaits: all writes are enqueued on the entry's
:class:`~.writer.WriteChain`. The tables (live, lingering) are the manager's,
shared by reference; the manager decides *when* a session ends, this class *what*
that end writes.
"""

from __future__ import annotations

import asyncio
import functools
from collections.abc import Callable

from meshforge.common.logging import get_logger
from meshforge.controller.liveness import (
    InvalidTransitionError,
    LivenessEvent,
    NodeState,
    next_state,
)
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.session.claims import AdmissionClaims
from meshforge.controller.session.live import LiveSession, end_event
from meshforge.controller.session.writer import WriteKind

__all__ = ["SessionEnds"]

log = get_logger(__name__)


class SessionEnds:
    """The end of a session and its liveness arrow (see module docstring)."""

    def __init__(
        self,
        registry: Registry,
        *,
        clock: Callable[[], float],
        live: dict[str, LiveSession],
        lingering: dict[str, LiveSession],
        claims: AdmissionClaims,
        owned: Callable[[LiveSession], bool],
        wake: asyncio.Event,
        on_offline: Callable[[LiveSession, LivenessEvent], None] | None = None,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._live = live
        self._lingering = lingering
        self._claims = claims
        self._owned = owned
        """``SessionManager.admission_pending``: a pending Hello owns the state."""
        self._wake = wake
        self._on_offline = on_offline
        """Called once per real ``-> OFFLINE`` arrow (V0.4 D66: the node's live
        attempts are lost). Never for SUSPECT: a blip must not kill work."""

    def apply_event(
        self, live: LiveSession, event: LivenessEvent, *, silence_s: float
    ) -> NodeState:
        """Apply a §12.3 arrow to ``live``; queue the write and log a real change.

        Never awaits the registry (the lease timer calls this for every session
        and every lingering entry).
        The write is **fenced** on this session (D20): it lands only while the
        node's ``last_session_id`` is still this session, so a write queued
        behind a slow chain can never overwrite a newer session's state. An
        :class:`InvalidTransitionError` is a bug in the caller, not in the agent:
        it is logged at error level and the state is left unchanged.
        """
        old = live.state
        try:
            new = next_state(old, event)
        except InvalidTransitionError as exc:
            log.exception(
                "liveness.invalid_transition",
                node_id=live.node_id,
                session_id=live.session_id,
                state=old.value,
                liveness_event=event.value,
                failure_type=exc.failure_type.value,
            )
            return old
        if new is old:
            return old
        live.state = new
        if new is NodeState.OFFLINE and self._on_offline is not None:
            self._on_offline(live, event)
        for fence in (live.session_id, *live.fence_ids):  # D41: at most one lands
            live.writer.enqueue(
                "set_node_state",
                WriteKind.STATE,
                functools.partial(
                    self._registry.set_node_state, live.node_id, new, session_id=fence
                ),
            )
        log.info(
            "node.state_changed",
            node_id=live.node_id,
            session_id=live.session_id,
            **{"from": old.value, "to": new.value},
            silence_s=round(silence_s, 3),
            liveness_event=event.value,
            lingering=live.lingering,
        )
        return new

    def close(self, live: LiveSession, reason: SessionEndReason) -> None:
        """End ``live`` (idempotent, first reason wins): unregister it, unblock its
        handler, stop its reader, then settle or defer the persistent half."""
        if live.ended:
            return  # first reason wins
        live.ended = True
        live.end_reason = reason
        if self._live.get(live.node_id) is live:
            del self._live[live.node_id]
        # Unblock the gRPC handler: whatever is still queued is moot now.
        while not live.outbound.empty():
            live.outbound.get_nowait()
        live.outbound.put_nowait(None)
        task = live.reader_task
        if task is not None and task is not asyncio.current_task() and not task.done():
            task.cancel()
        live.done.set()
        if end_event(reason) is not None and self._owned(live):
            # D28: a newer Hello is being admitted and owns the state; its
            # admission finishes this end (no event) or, if it fails, settles it
            # with the event (``SessionManager._release_claim``).
            previous = self._claims.defer(live)
            if previous is not None:  # pragma: no cover - one session per node ends
                self.finish_end(previous, fire_event=True)
            log.info(
                "session.end_deferred",
                node_id=live.node_id,
                session_id=live.session_id,
                end_reason=reason.value,
                state=live.state.value,
            )
            return
        self.finish_end(live, fire_event=True)

    def finish_end(self, live: LiveSession, *, fire_event: bool) -> None:
        """The persistent half of a close: the end's event (D19), then
        ``close_session`` on the chain, lingering if the loss left it SUSPECT.

        ``fire_event=False`` is a deferred end a newer Hello took over (D28): no
        state event, the chain simply ends with ``close_session``.
        """
        reason = live.end_reason
        if reason is None:  # pragma: no cover - close() sets it before any call
            raise RuntimeError("finish_end called on a session that never closed")
        # D19: the end decides the arrow; the state write goes on this session's
        # chain *before* close_session, so the two can never be reordered.
        event = end_event(reason) if fire_event else None
        if event is not None:
            self.apply_event(live, event, silence_s=live.tracker.silence(self._clock()))
        linger = event is LivenessEvent.STREAM_LOST and live.state is NodeState.SUSPECT
        close = functools.partial(self._registry.close_session, live.session_id, reason)
        if linger:
            # The loss already counts as the miss; LEASE_EXPIRED is all that is
            # left, at last heartbeat + offline_after_s. close_session is written
            # now (CLOSE), the chain stays open for that possible OFFLINE.
            live.tracker.mark_missed()
            live.lingering = True
            previous = self._lingering.get(live.node_id)
            if previous is not None and previous is not live:  # pragma: no cover
                self.end_linger(previous, cause="replaced")  # admission resolves it
            self._lingering[live.node_id] = live
            live.writer.enqueue("close_session", WriteKind.CLOSE, close)
            self._wake.set()  # the timer must learn the lingering deadline
        else:
            # The final write of this session's chain; the chain task ends after it.
            live.writer.enqueue("close_session", WriteKind.END, close)
        log.info(
            "session.closed",
            node_id=live.node_id,
            session_id=live.session_id,
            end_reason=reason.value,
            liveness_event=event.value if event else None,
            state=live.state.value,
            lingering=linger,
            owned_by_hello=not fire_event,
            **live.summary(),
        )

    def expire_linger(self, entry: LiveSession, now: float) -> None:
        """Lease timer hook: apply the lingering entry's due lease event (D19).

        ``LEASE_EXPIRED`` writes ``SUSPECT -> OFFLINE`` on the entry's chain and
        ends the linger (and with it the chain). A no-op for an entry that is no
        longer lingering (a Hello resolved it) or whose lease is not due.
        """
        while entry.lingering:
            event = entry.tracker.check(now)
            if event is None:
                return
            silence = entry.tracker.silence(now)
            self.apply_event(entry, event, silence_s=silence)
            if event is LivenessEvent.LEASE_EXPIRED:
                log.warning(
                    "node.lease_expired",
                    node_id=entry.node_id,
                    session_id=entry.session_id,
                    silence_s=round(silence, 3),
                    lingering=True,
                )
                self.end_linger(entry, cause="lease_expired")

    def end_linger(self, entry: LiveSession, *, cause: str) -> None:
        """Stop lingering: forget the entry and let its chain end (idempotent)."""
        if not entry.lingering:
            return
        entry.lingering = False
        if self._lingering.get(entry.node_id) is entry:
            del self._lingering[entry.node_id]
        entry.writer.finish()
        log.info(
            "session.linger_ended",
            node_id=entry.node_id,
            session_id=entry.session_id,
            cause=cause,
            state=entry.state.value,
            pending_writes=entry.writer.pending,
        )
