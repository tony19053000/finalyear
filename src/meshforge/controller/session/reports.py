"""Per-report handlers of the session layer (split out of ``manager.py``, D42).

Reports are the agent payloads that are buffered in the agent's replay ring,
replayed after a reconnect and acked (``resource_update``, ``hardware_scores``;
V0.4 adds ``attempt_status`` and ``log_chunk``). This module holds their handlers,
the ``Reconcile`` marker that tells the resource sink where a replay starts, the
Hello's optional score set, and the counters of what was dropped:

* ``resource_update`` - bounded by D12 (clamp, never reject) and handed to the
  in-memory :class:`~meshforge.controller.resources.ResourceSink`;
* ``hardware_scores`` (report or Hello) - validated by D38/D39; an invalid set is
  dropped, logged ``session.hardware_scores_rejected`` and counted (per session
  and in :attr:`ReportHandlers.hardware_scores_rejected_total`), never a reason
  to close the stream or refuse the Hello. A valid report goes on the session's
  write chain (fenced, ordered with its state writes).

Nothing here awaits the database: every handler is synchronous (the sink is in
memory, registry writes are enqueued on the session's
:class:`~meshforge.controller.session.writer.WriteChain`). The manager's dispatch
table calls these; acking is the manager's job.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.registry import Registry
from meshforge.controller.resources import ResourceSink
from meshforge.controller.scores import scores_document
from meshforge.controller.session.dispatch import Handler
from meshforge.controller.session.limits import (
    check_hardware_scores,
    check_resource_update,
)
from meshforge.controller.session.live import LiveSession
from meshforge.controller.session.writer import WriteKind

__all__ = ["ReportHandlers", "declared_gpus"]

log = get_logger(__name__)


def declared_gpus(hardware: agent_pb2.HardwareSummary) -> frozenset[int]:
    """The GPU indices a Hello declared (D39)."""
    return frozenset(gpu.index for gpu in hardware.gpus)


class ReportHandlers:
    """Handlers + counters for report payloads (see module docstring)."""

    def __init__(
        self,
        registry: Registry,
        *,
        clock: Callable[[], float],
        resources: ResourceSink | None = None,
    ) -> None:
        self._registry = registry
        self._clock = clock
        self._resources = resources
        """Where ResourceUpdates go (roadmap 0.3.11); None = not recorded."""
        self.hardware_scores_rejected_total = 0
        """Score sets dropped by D38 validation across all sessions."""

    def handlers(self) -> dict[str, Handler]:
        """The report payloads' dispatch handlers (D42), by payload name."""

        async def resource_update(
            live: LiveSession, msg: agent_pb2.AgentMessage
        ) -> None:
            self.on_resource_update(live, msg.resource_update)

        async def reconcile(live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
            self.on_reconcile(live, msg.reconcile)

        async def hardware_scores(
            live: LiveSession, msg: agent_pb2.AgentMessage
        ) -> None:
            self.on_hardware_scores(live, msg.hardware_scores)

        return {
            "resource_update": resource_update,
            "reconcile": reconcile,
            "hardware_scores": hardware_scores,
        }

    # ----------------------------------------------------------------- hello

    def begin_session(self, node_id: str, session_epoch: int) -> None:
        """A session was admitted: arm the resource sink's cursor reset.

        A new session may be a *restarted* agent process, whose report sequence
        starts at 1 again. This session's Reconcile (if it announces a replay)
        cancels the reset before any report. The epoch goes with it so the sink's
        zombie fence is armed from the first instant of the session, not only from
        its first report.
        """
        if self._resources is not None:
            self._resources.begin_session(node_id, 0, session_epoch=session_epoch)

    def hello_scores(
        self, hello: agent_pb2.Hello, session_id: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """The score document a Hello carries, validated (D38); ``(None, rejected)``.

        A Hello without scores - or with scores that fail validation - stores
        ``NULL`` (pending). A bad score set never refuses the Hello: scores are
        optional, enrolment is not (it is logged and counted instead).
        """
        if not hello.HasField("hardware_scores"):
            return None, False
        reason = check_hardware_scores(
            hello.hardware_scores, declared_gpus(hello.hardware)
        )
        if reason is not None:
            self.reject_scores(hello.node_id, session_id, "hello", reason)
            return None, True
        document, _measured_at = scores_document(hello.hardware_scores)
        return document, False

    def reject_scores(
        self, node_id: str, session_id: str, source: str, reason: str
    ) -> None:
        self.hardware_scores_rejected_total += 1
        log.warning(
            "session.hardware_scores_rejected",
            node_id=node_id,
            session_id=session_id,
            source=source,
            reason=reason,
            rejected_total=self.hardware_scores_rejected_total,
        )

    # --------------------------------------------------------------- reports

    def on_reconcile(self, live: LiveSession, reconcile: agent_pb2.Reconcile) -> None:
        """The reconnect marker: it says where the agent's replay starts.

        ``replay_from_sequence > 0`` means this is the *same* agent process
        replaying its un-acked reports, so the resource sink must keep its dedupe
        cursor and reject them; ``0`` (nothing buffered) leaves the reset armed by
        the session's opening. ``running_attempts`` belongs to V0.4's executor and
        is only counted here.
        """
        if self._resources is not None:
            self._resources.begin_session(live.node_id, reconcile.replay_from_sequence)
        log.info(
            "session.reconciled",
            node_id=live.node_id,
            session_id=live.session_id,
            replay_from_sequence=reconcile.replay_from_sequence,
            running_attempts=len(reconcile.running_attempts),
        )

    def on_resource_update(
        self, live: LiveSession, update: agent_pb2.ResourceUpdate
    ) -> None:
        """Bound the report (D12: clamp, never reject) and hand it to the sink.

        Synchronous on purpose: the sink is in-memory (live ring + downsampler),
        so the reader never waits on it, and the database write happens later in
        the controller's snapshot flusher.
        """
        bounded = check_resource_update(update)
        if bounded.clamped:
            live.resource_updates_clamped += 1
            if live.resource_updates_clamped == 1:
                # Once per session: a broken agent must not fill the log.
                log.warning(
                    "session.resource_update_clamped",
                    node_id=live.node_id,
                    session_id=live.session_id,
                    dropped=list(bounded.dropped),
                )
        live.resource_updates += 1
        if self._resources is not None:
            self._resources.record(
                live.node_id, live.session_epoch, bounded.update, self._clock()
            )

    def on_hardware_scores(
        self, live: LiveSession, scores: agent_pb2.HardwareScores
    ) -> None:
        """Validate a score report (D38) and queue its fenced, ordered write.

        A rejected set is dropped - logged with the reason, counted - and the
        message is still acked by the caller, so the agent stops replaying it; the
        stream stays up (a bad optional field must not take a node offline). A
        valid one goes on the session's write chain, so it can never overtake or
        be overtaken by this session's state writes, and the registry fences it on
        ``last_session_id`` and ignores it if the stored run is newer.
        """
        reason = check_hardware_scores(scores, live.declared_gpus)
        if reason is not None:
            live.hardware_scores_rejected += 1
            self.reject_scores(live.node_id, live.session_id, "report", reason)
            return
        document, measured_at = scores_document(scores)
        live.hardware_scores_received += 1
        live.writer.enqueue(
            "set_hardware_scores",
            WriteKind.SCORES,
            functools.partial(
                self._registry.set_hardware_scores,
                live.node_id,
                document,
                session_id=live.session_id,
                measured_at=measured_at,
            ),
        )
        log.info(
            "session.hardware_scores_received",
            node_id=live.node_id,
            session_id=live.session_id,
            run_id=scores.run_id,
            metrics=len(scores.scores),
        )
