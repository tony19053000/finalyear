"""Admission claims and deferred session ends (D28; split out of ``manager.py``, D42).

**A pending Hello owns the state.** A ``Hello`` for node N that is being admitted
while N's previous session is still ending decides N's state; the previous
session then ends with **no** state event, exactly like a supersession.

From the moment ``SessionManager.admit`` has validated a Hello until its
admission returns, the Hello's epoch is *claimed* here (:meth:`AdmissionClaims.
claim`). While an epoch **higher** than a session's is claimed for its node, that
session is claimed too (:meth:`AdmissionClaims.owns`):

* its end is **deferred**: ``SessionEnds.close`` still does everything in
  memory (unregister, unblock the handler, stop the reader, record the reason)
  but parks the session here (:meth:`defer`) instead of applying the end's event
  and writing ``close_session``;
* the lease timer skips it (live or lingering): no ``LEASE_MISSED``/``EXPIRED``
  is decided against a node whose Hello is being admitted.

A successful admission takes ``prior`` from the claimed session's **in-memory**
state and, once the new session is persisted, :meth:`consume`\\ s the deferred end
(it then fires no event; its reason is kept). If the admission does not succeed
(rejected, stale, registry error, timeout, shutdown), :meth:`release` hands the
deferred session back so the manager settles it **with** the event it would have
fired (a genuine clean close still writes OFFLINE, only later). A **stale** Hello
(epoch not above the session's) claims nothing, so it can never suppress a real
close. The deferral is bounded by one admission (``admission_timeout_s``, D41);
a closing manager claims nothing.

This class only keeps the bookkeeping; it never writes, logs or applies events -
the manager does, with what these methods return. Not thread-safe (one event
loop, no awaits inside).
"""

from __future__ import annotations

from meshforge.controller.session.live import LiveSession

__all__ = ["AdmissionClaims"]


class AdmissionClaims:
    """Pending Hello epochs and the session ends they defer, by node (D28)."""

    __slots__ = ("deferred", "pending")

    def __init__(self) -> None:
        self.pending: dict[str, list[int]] = {}
        """Epochs of validated Hellos whose admission has not returned, by node."""
        self.deferred: dict[str, LiveSession] = {}
        """Sessions that ended while a newer Hello was pending, by node."""

    def claim(self, node_id: str, epoch: int) -> None:
        """A Hello with ``epoch`` for ``node_id`` is now being admitted."""
        self.pending.setdefault(node_id, []).append(epoch)

    def owns(self, live: LiveSession, *, closing: bool = False) -> bool:
        """True while a Hello with a higher epoch than ``live``'s is being admitted
        for its node: that Hello owns the node's state. Never while ``closing``."""
        if closing:
            return False
        pending = self.pending.get(live.node_id)
        return pending is not None and max(pending) > live.session_epoch

    def defer(self, live: LiveSession) -> LiveSession | None:
        """Park the end of claimed ``live``; return a session this displaced.

        At most one session per node is ever deferred (a node has one live
        session, and it ends once). Should a second one arrive anyway, the one
        displaced is returned for the caller to settle with its event now, so no
        write chain is ever left without its end.
        """
        previous = self.deferred.get(live.node_id)
        self.deferred[live.node_id] = live
        return None if previous is None or previous is live else previous

    def deferred_for(self, node_id: str) -> LiveSession | None:
        return self.deferred.get(node_id)

    def consume(self, live: LiveSession) -> bool:
        """A successful admission took over ``live``'s end (no event): forget it.

        False if ``live`` is no longer the deferred end of its node.
        """
        if self.deferred.get(live.node_id) is not live:
            return False
        del self.deferred[live.node_id]
        return True

    def release(
        self, node_id: str, epoch: int, *, closing: bool = False
    ) -> LiveSession | None:
        """End one Hello's claim; return a deferred end nobody owns any more.

        The caller settles the returned session **with** its event, as if no
        Hello had been pending. ``None`` when nothing is left to settle (the
        admission consumed it, or another pending Hello still owns it).
        """
        pending = self.pending[node_id]
        pending.remove(epoch)
        if not pending:
            del self.pending[node_id]
        deferred = self.deferred.get(node_id)
        if deferred is None or self.owns(deferred, closing=closing):
            return None
        del self.deferred[node_id]
        return deferred

    def drain(self) -> list[LiveSession]:
        """Forget and return every deferred end (the shutdown backstop)."""
        ends = list(self.deferred.values())
        self.deferred.clear()
        return ends
