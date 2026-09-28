"""Live-session table and message dispatch for ``AgentControl.Session`` (0.3.6/0.3.10).

One :class:`LiveSession` per connected ``node_id``; the table is a ``dict`` behind a
single ``asyncio.Lock`` taken by *admission only* (two Hellos for one node are
strictly ordered; closing a session is synchronous in memory and needs no lock).
Each stream has exactly one reader task (created by
:meth:`SessionManager.start_reader`), one bounded outbound queue drained by the
gRPC handler, and one :class:`~.writer.WriteChain` that carries its registry
writes; the three never share a coroutine.

Time: every lease/RTT computation uses the injected monotonic ``clock`` (the event
loop's ``time()`` in production). The wall clock appears only in ``HelloAck.
controller_time`` and in the registry's database timestamps.

**Liveness is decided in memory.** Nothing on the heartbeat path or in the lease
timer awaits the database: every registry write after admission is *enqueued* on
the session's ordered :class:`~.writer.WriteChain` (see its docstring), so a slow
or unreachable PostgreSQL delays the *record* of liveness, never the decision.
Admission itself (``get_node`` for fencing, the upsert and the session row) does
await the registry - a Hello needs the answer - and only ever blocks other
Hellos, not readers or the timer. Contract violations by the agent are
:class:`~.errors.SessionProtocolError` and close the session ``protocol_error``.

Epoch fencing (ADR-0002 Updates §1) compares a Hello against **both** the live
session (if any) and the highest epoch the registry has ever persisted for the
node, so a stale process cannot be admitted by waiting for the winner's stream to
end. A legitimate reconnect always carries a strictly higher epoch (ADR-0013 §3:
the agent increments before every stream), so "equal" is stale too.

Heartbeat persistence is coalesced and RTT is sampled by
:class:`~.heartbeats.HeartbeatRecorder`. Bounds on peer-controlled input
(:mod:`~.limits`): ``message_id`` must be a ``cmd_<ULID>`` (so the dedupe window
holds at most ``dedupe_window`` x 30 bytes) and the Hello is bounded by
:func:`~.admission.check_hello` before anything is persisted.

**How a session ends decides the liveness arrow** (ARCHITECTURE §12.3 2026-09-24
amendment, D19). Every end goes through :meth:`SessionManager._close`, which is
idempotent (first reason wins) and maps the reason to an event:

* clean - ``agent_closed`` (the agent said ``Goodbye``) and ``controller_shutdown`` ->
  ``STREAM_CLOSED``: ``ONLINE/SUSPECT/DRAINING -> OFFLINE`` written now, then
  ``close_session`` ends the chain;
* abrupt - ``stream_lost``, ``protocol_error``, ``internal_error`` ->
  ``STREAM_LOST``: ``ONLINE/DRAINING -> SUSPECT`` written now, ``close_session``
  written promptly as a :attr:`~.writer.WriteKind.CLOSE` that keeps the chain
  open, and the session stays in :attr:`SessionManager._lingering` with its lease
  tracker. The lease timer walks lingering entries too: ``LEASE_EXPIRED -> OFFLINE``
  at the normal edge measured from the last heartbeat, then the chain is finished.
  A newer ``Hello`` first resolves the linger with **no** OFFLINE write
  (``SUSPECT -> RECONNECTED -> ONLINE``); a stale-epoch Hello does not;
* ``superseded`` - no event, no state write: the new session's Hello owns it;
* ``lease_expired`` - none extra: the timer already applied ``LEASE_EXPIRED``.

**Only a Goodbye is a clean close** (D40). The agent's last message on a stream
it closes on purpose is ``Goodbye`` (with this session's id); the session ends
``agent_closed`` **on receipt** - whatever the stream does afterwards (half-close,
cancel) - and the Goodbye is never acked. **Any end of the stream without a prior
Goodbye is ``stream_lost``**, however normal the iterator's end looked: grpc.aio
ends the server's request iterator normally for a half-close *and* for a
cancelled call, a closed channel or a killed client process, so a normal end
proves nothing. An agent without Goodbye therefore degrades to SUSPECT, then
OFFLINE at the lease edge - the safe direction (OFFLINE late never duplicates
work). A Goodbye whose detail breaks its bound is a protocol error; a Goodbye with
another session's id is a stale message (dropped, counted), so a zombie stream can
never close the session that replaced it. Messages after a Goodbye are not read.

**A pending Hello owns the state** (D28): while a validated Hello for node N is
being admitted, N's older session is *claimed* - its end is deferred (no event
yet) and the lease timer skips it. Success consumes the deferred end with no
event; any other outcome settles it with its event. The bookkeeping is
:class:`~.claims.AdmissionClaims`; its module docstring is the full rule.

**Dispatch is a table** (D42): :meth:`SessionManager.handle_message` does the
envelope work (stale session, ``message_id``, dedupe, Ack) and calls the handler
registered for the payload in :class:`~.dispatch.DispatchTable`; report
handlers and their counters live in :class:`~.reports.ReportHandlers`.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
from collections.abc import AsyncIterator, Callable
from typing import Final

import grpc
from google.protobuf.json_format import MessageToDict

from meshforge.common.ids import new_command_id, new_session_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import (
    InvalidTransitionError,
    LeaseTracker,
    LivenessConfig,
    LivenessEvent,
    NodeState,
    next_state,
)
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.resources import ResourceSink
from meshforge.controller.session.admission import (
    RejectCode,
    check_hello,
    hello_ack,
    hello_event,
    shutting_down,
)
from meshforge.controller.session.claims import AdmissionClaims
from meshforge.controller.session.dispatch import (
    DispatchTable,
    check_envelope,
    log_ack,
    log_unhandled,
)
from meshforge.controller.session.epoch import MAX_SESSION_EPOCH
from meshforge.controller.session.errors import (
    OutboundOverflowError,
    SessionProtocolError,
)
from meshforge.controller.session.heartbeats import HeartbeatRecorder
from meshforge.controller.session.limits import check_goodbye
from meshforge.controller.session.live import (
    RTT_EWMA_ALPHA,
    Accepted,
    AdmitResult,
    LiveSession,
    Rejected,
    SeenIds,
    end_event,
    update_ewma,
)
from meshforge.controller.session.reports import ReportHandlers, declared_gpus
from meshforge.controller.session.writer import (
    DEFAULT_WRITE_CAPACITY,
    WriteChain,
    WriteKind,
)

__all__ = [
    "RTT_EWMA_ALPHA",
    "Accepted",
    "AdmitResult",
    "LiveSession",
    "Rejected",
    "SessionManager",
    "declared_gpus",
    "end_event",
    "update_ewma",
]

log = get_logger(__name__)

_UNACKED: Final = frozenset({"ack", "pong", "goodbye"})
"""Agent payloads that get no Ack back (ADR-0002 Updates §4: never buffered)."""


class SessionManager:
    """The live-session table plus dispatch (see module docstring)."""

    def __init__(
        self,
        registry: Registry,
        liveness: LivenessConfig,
        *,
        clock: Callable[[], float],
        accepted_protocol_versions: tuple[int, int] = (1, 1),
        outbound_queue_size: int = 1000,
        dedupe_window: int = 10_000,
        write_capacity: int = DEFAULT_WRITE_CAPACITY,
        wall_clock: Callable[[], dt.datetime] | None = None,
        resources: ResourceSink | None = None,
    ) -> None:
        self._registry = registry
        self._liveness = liveness
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: dt.datetime.now(dt.UTC))
        self._versions = accepted_protocol_versions
        self._queue_size = outbound_queue_size
        self._dedupe_window = dedupe_window
        self._write_capacity = write_capacity
        self._lock = asyncio.Lock()
        self._live: dict[str, LiveSession] = {}
        self._lingering: dict[str, LiveSession] = {}
        """Abruptly ended sessions whose lease still runs, by node (D19)."""
        self._claims = AdmissionClaims()
        """Pending Hellos and the session ends they defer (D28, :mod:`.claims`)."""
        self.reports = ReportHandlers(registry, clock=clock, resources=resources)
        """Report handlers and their counters (:mod:`.reports`)."""
        self.heartbeats = HeartbeatRecorder(registry, liveness, clock=clock)
        """Coalesced ``last_seen_at`` writes and RTT samples (:mod:`.heartbeats`)."""
        self.dispatch = DispatchTable(log_unhandled)
        """payload name -> handler (D42); later versions register theirs here."""
        self.dispatch.register("heartbeat", self._on_heartbeat)
        self.dispatch.register("pong", self.heartbeats.on_pong_message)
        self.dispatch.register("ack", log_ack)
        self.dispatch.register("goodbye", self._on_goodbye)
        for payload, handler in self.reports.handlers().items():
            self.dispatch.register(payload, handler)
        self._tasks: set[asyncio.Task[None]] = set()
        self._chains: set[WriteChain] = set()
        """Write chains not yet finished (closed sessions included until drained)."""
        self.wake = asyncio.Event()
        """Set whenever a session registers; the lease timer waits on it."""
        self.stale_messages_total = 0
        self.closing = False
        """True once :meth:`shutdown` started; new Hellos are refused."""

    # ------------------------------------------------------------- read side

    @property
    def liveness(self) -> LivenessConfig:
        return self._liveness

    def snapshot(self) -> list[LiveSession]:
        """Current live sessions (a copy; safe to iterate while others mutate)."""
        return list(self._live.values())

    def get(self, node_id: str) -> LiveSession | None:
        return self._live.get(node_id)

    def lingering(self) -> list[LiveSession]:
        """Abruptly ended sessions whose lease still runs (a copy, D19)."""
        return list(self._lingering.values())

    def get_lingering(self, node_id: str) -> LiveSession | None:
        return self._lingering.get(node_id)

    def __len__(self) -> int:
        return len(self._live)

    @property
    def hardware_scores_rejected_total(self) -> int:
        """Score sets dropped by D38 validation across all sessions."""
        return self.reports.hardware_scores_rejected_total

    @property
    def _pending_hellos(self) -> dict[str, list[int]]:
        return self._claims.pending

    @property
    def _deferred_ends(self) -> dict[str, LiveSession]:
        return self._claims.deferred

    def admission_pending(self, live: LiveSession) -> bool:
        """True while a Hello with a higher epoch than ``live``'s is being admitted
        for its node: that Hello owns the node's state (D28, :mod:`.claims`)."""
        return self._claims.owns(live, closing=self.closing)

    # -------------------------------------------------------------- admission

    async def admit(self, hello: agent_pb2.Hello, peer_address: str) -> AdmitResult:
        """Apply the Hello rules (ADR-0002 Updates §1) and register the session.

        Holds the table lock for the whole admission (including the registry
        reads/writes a Hello genuinely needs) so two Hellos for one node are
        strictly ordered. Hellos are rare; simplicity wins over a per-node lock
        here. Nothing but admission takes the lock, so a stalled database delays
        other Hellos only - never a reader or the lease timer.
        """
        if self.closing:
            return shutting_down()
        refused = check_hello(hello)
        if refused is not None:
            return refused
        if not 0 <= hello.session_epoch <= MAX_SESSION_EPOCH:
            # Unreachable from a protobuf uint64; guards non-proto callers so the
            # bound lives in one place (session/epoch.py) and nothing is persisted.
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                f"session_epoch {hello.session_epoch} exceeds the controller's "
                f"maximum {MAX_SESSION_EPOCH}",
                grpc.StatusCode.INVALID_ARGUMENT,
            )
        lo, hi = self._versions
        if not lo <= hello.protocol_version <= hi:
            return Rejected(
                RejectCode.REJECT_CODE_PROTOCOL_VERSION,
                f"protocol_version {hello.protocol_version} not in [{lo}, {hi}]",
            )
        # D28: from here until this admission returns, the Hello claims the
        # node's state against any older session still ending (module docstring).
        node_id = hello.node_id
        self._claims.claim(node_id, hello.session_epoch)
        try:
            async with self._lock:
                return await self._admit_locked(hello, peer_address)
        finally:
            self._release_claim(node_id, hello.session_epoch)

    def _release_claim(self, node_id: str, epoch: int) -> None:
        """End one pending Hello's claim; settle a deferred end nobody owns now.

        Runs after the admission returned: a successful one has already consumed
        the deferred end (no event); otherwise the deferred session ends with the
        event its reason fires, as if no Hello had been pending.
        """
        deferred = self._claims.release(node_id, epoch, closing=self.closing)
        if deferred is not None:
            self._finish_end(deferred, fire_event=True)
        # The timer skipped this node's sessions while the claim stood.
        self.wake.set()

    async def _admit_locked(
        self, hello: agent_pb2.Hello, peer_address: str
    ) -> AdmitResult:
        node_id = hello.node_id
        # One read per Hello: the registry's high-water mark. Fencing must hold
        # across disconnects, not only against a live competitor (ADR-0002 §1).
        record = await self._registry.get_node(node_id)
        if self.closing:  # shutdown began while we waited on the database
            return shutting_down()
        # Read the live and lingering entries *after* the await: the lease timer
        # and readers close sessions without the lock, so both tables may have
        # changed while we waited.
        old = self._live.get(node_id)
        deferred = self._claims.deferred_for(node_id)
        lingering = self._lingering.get(node_id)
        highest = -1 if record is None else record.session_epoch
        for entry in (old, deferred, lingering):
            if entry is not None:
                highest = max(highest, entry.session_epoch)
        if hello.session_epoch <= highest:
            # A stale Hello never resolves a linger: the lease keeps running.
            log.warning(
                "session.stale_epoch",
                node_id=node_id,
                session_id=old.session_id if old else None,
                hello_epoch=hello.session_epoch,
                highest_epoch=highest,
                live=old is not None,
                peer=peer_address,
            )
            return Rejected(
                RejectCode.REJECT_CODE_STALE_EPOCH,
                f"epoch {hello.session_epoch} <= highest seen {highest}",
            )
        if old is not None:
            prior = old.state
            self._close(old, SessionEndReason.SUPERSEDED)
        elif deferred is not None:
            # D28: the previous session ended while this Hello was pending; its
            # in-memory state (before any end event) is the truth, not ``record``,
            # which was read before that close.
            prior = deferred.state
        elif lingering is not None:
            # The in-memory state is ahead of the row (its writes are queued):
            # SUSPECT here, so this Hello is SUSPECT -> RECONNECTED -> ONLINE.
            prior = lingering.state
        else:
            prior = NodeState.ENROLLING if record is None else record.state

        if prior is NodeState.REVOKED:
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                "node is revoked",
                grpc.StatusCode.PERMISSION_DENIED,
            )
        event = hello_event(prior)
        state = prior if event is None else next_state(prior, event)

        now = self._clock()
        session_id = new_session_id()
        scores, scores_rejected = self.reports.hello_scores(hello, session_id)
        await self._registry.upsert_node_on_hello(
            node_id=node_id,
            hostname=hello.hostname,
            os=hello.os,
            platform=hello.platform,
            hardware=MessageToDict(hello.hardware, preserving_proto_field_name=True),
            session_epoch=hello.session_epoch,
            session_id=session_id,
            state=state,
            hardware_scores=scores,
        )
        await self._registry.open_session(
            session_id=session_id,
            node_id=node_id,
            session_epoch=hello.session_epoch,
            peer_address=peer_address,
            agent_version=hello.agent_version,
            protocol_version=hello.protocol_version,
        )
        if deferred is not None and self._claims.consume(deferred):
            # Likewise only now: this Hello owns the state, so the deferred end
            # fires no event (D28). Had admission failed, ``_release_claim``
            # would have settled it with its event instead.
            self._finish_end(deferred, fire_event=False)
        if lingering is not None:
            # Resolved only now that the new session is persisted: had admission
            # failed above, the old lease would still be running. Nothing is
            # written for it - its fenced writes still queued are stale by now.
            self._end_linger(lingering, cause="reconnected")
        writer = WriteChain(
            node_id=node_id, session_id=session_id, capacity=self._write_capacity
        )
        live = LiveSession(
            node_id=node_id,
            session_id=session_id,
            session_epoch=hello.session_epoch,
            peer_address=peer_address,
            tracker=LeaseTracker(self._liveness, self._clock),
            state=state,
            outbound=asyncio.Queue(maxsize=self._queue_size),
            seen=SeenIds(self._dedupe_window),
            next_ping_at=now,  # first RTT sample right away
            writer=writer,
            declared_gpus=declared_gpus(hello.hardware),
            hardware_scores_received=int(scores is not None),
            hardware_scores_rejected=int(scores_rejected),
        )
        self._chains.add(writer)
        writer.start().add_done_callback(lambda _t: self._chains.discard(writer))
        self._live[node_id] = live
        self.reports.begin_session(node_id, hello.session_epoch)
        self.wake.set()
        if prior is not state:
            log.info(
                "node.state_changed",
                node_id=node_id,
                session_id=session_id,
                **{"from": prior.value, "to": state.value},
                silence_s=0.0,
                liveness_event=event.value if event else None,
            )
        log.info(
            "session.opened",
            node_id=node_id,
            session_id=session_id,
            session_epoch=hello.session_epoch,
            peer=peer_address,
            agent_version=hello.agent_version,
            superseded=old.session_id if old else None,
        )
        return Accepted(live, hello_ack(session_id, self._liveness, self._wall_clock()))

    # ---------------------------------------------------------------- reader

    def start_reader(
        self,
        live: LiveSession,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
    ) -> asyncio.Task[None]:
        """Spawn the one reader task of ``live``; its exceptions are always logged."""
        task = asyncio.create_task(
            self._read_loop(live, request_iterator),
            name=f"session-reader:{live.session_id}",
        )
        live.reader_task = task
        self._track(task)
        return task

    def _track(self, task: asyncio.Task[None]) -> None:
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(_log_task_failure)

    async def _read_loop(
        self,
        live: LiveSession,
        request_iterator: AsyncIterator[agent_pb2.AgentMessage],
    ) -> None:
        """Consume the agent's messages; classify how the stream ended (D19/D40).

        A Goodbye closes the session ``agent_closed`` inside :meth:`handle_message`
        and nothing after it is read. Every other end - the iterator finishing
        normally included - is abrupt (module docstring).
        """
        try:
            async for msg in request_iterator:
                if live.ended:
                    return  # closed by supersede/lease/shutdown/handler meanwhile
                await self.handle_message(live, msg)
                if live.done.is_set():  # ``ended``, re-read after the await
                    return  # a Goodbye (or a close meanwhile): read nothing more
        except asyncio.CancelledError:
            raise  # ended by supersede/lease/shutdown/handler; they close it
        except SessionProtocolError as exc:
            log.warning(
                "session.protocol_error",
                node_id=live.node_id,
                session_id=live.session_id,
                error=str(exc),
                failure_type=exc.failure_type.value,
            )
            reason = SessionEndReason.PROTOCOL_ERROR
        except grpc.RpcError as exc:
            log.info(
                "session.stream_error",
                node_id=live.node_id,
                session_id=live.session_id,
                cause=type(exc).__name__,
            )
            reason = SessionEndReason.STREAM_LOST
        except Exception:
            # A bug in dispatch must not leave a zombie session behind; the stream
            # is closed and the traceback logged. The reason is internal_error, not
            # protocol_error: the peer did nothing wrong (0003 widened the enum).
            log.exception(
                "session.reader_crashed",
                node_id=live.node_id,
                session_id=live.session_id,
            )
            reason = SessionEndReason.INTERNAL_ERROR
        else:
            if live.ended:
                return
            # D40: the request stream ended without a Goodbye. grpc.aio shows a
            # half-close, a cancelled call and a dead client alike, so this is
            # never evidence of a clean close.
            log.info(
                "session.stream_ended_without_goodbye",
                node_id=live.node_id,
                session_id=live.session_id,
            )
            reason = SessionEndReason.STREAM_LOST
        await self.end_session(live, reason)

    # -------------------------------------------------------------- dispatch

    async def handle_message(
        self, live: LiveSession, msg: agent_pb2.AgentMessage
    ) -> None:
        """Process one agent message on ``live`` (idempotent per ``message_id``)."""
        which = check_envelope(live, msg)
        if which is None:
            live.stale_messages += 1
            self.stale_messages_total += 1
            log.warning(
                "session.stale_message",
                node_id=live.node_id,
                session_id=live.session_id,
                message_session_id=msg.session_id,
                payload=msg.WhichOneof("payload"),
                count=live.stale_messages,
            )
            return
        if live.seen.remember(msg.message_id):
            live.duplicates += 1
            log.debug(
                "session.duplicate_message",
                node_id=live.node_id,
                session_id=live.session_id,
                message_id=msg.message_id,
                payload=which,
            )
        else:
            await self.dispatch.handler_for(which)(live, msg)
        if which not in _UNACKED:
            # Receipts (Ack) and probe replies (Pong) are never acknowledged - they
            # are never buffered or replayed, and acking them would ping-pong
            # forever. Every other message is, duplicates included: the agent may
            # have missed the first Ack.
            self.send(live, self._ack(live, msg.message_id))

    async def _on_heartbeat(
        self, live: LiveSession, _msg: agent_pb2.AgentMessage
    ) -> None:
        now = self._clock()
        live.tracker.observe_heartbeat(now)
        live.heartbeats += 1
        if live.state is NodeState.SUSPECT:
            await self.apply_event(live, LivenessEvent.HEARTBEAT, silence_s=0.0)
        self.heartbeats.touch(live, now, force=False)

    async def _on_goodbye(self, live: LiveSession, msg: agent_pb2.AgentMessage) -> None:
        """The agent's announced end (D40): ``agent_closed`` now, never acked."""
        refused = check_goodbye(msg.goodbye)
        if refused is not None:
            raise SessionProtocolError(
                refused, session_id=live.session_id, node_id=live.node_id
            )
        reason = msg.goodbye.reason
        log.info(
            "session.goodbye",
            node_id=live.node_id,
            session_id=live.session_id,
            reason=agent_pb2.Goodbye.Reason.Name(reason)
            if reason in agent_pb2.Goodbye.Reason.values()
            else reason,
            detail=msg.goodbye.detail or None,
        )
        self._close(live, SessionEndReason.AGENT_CLOSED)

    async def flush_touch(self, live: LiveSession, now: float) -> None:
        """Timer hook: queue a pending ``last_seen_at`` write once its window closed."""
        self.heartbeats.flush_touch(live, now)

    def _ack(self, live: LiveSession, acked: str) -> agent_pb2.ControllerMessage:
        return agent_pb2.ControllerMessage(
            message_id=new_command_id(),
            session_id=live.session_id,
            ack=agent_pb2.Ack(acked_message_id=acked),
        )

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

    # ------------------------------------------------------------------- RTT

    async def send_ping(self, live: LiveSession, now: float) -> None:
        """Send one Ping; the previous outstanding ping (if any) simply expires."""
        try:
            self.send(live, self.heartbeats.ping(live, now))
        except OutboundOverflowError as exc:
            log.warning(
                "session.outbound_overflow",
                node_id=live.node_id,
                session_id=live.session_id,
                capacity=exc.details["capacity"],
            )
            await self.end_session(live, SessionEndReason.PROTOCOL_ERROR)

    # ------------------------------------------------------------- liveness

    async def apply_event(
        self, live: LiveSession, event: LivenessEvent, *, silence_s: float
    ) -> NodeState:
        """Apply a §12.3 arrow to ``live``; queue the write and log a real change.

        Never awaits the registry (the timer calls this for every session and
        every lingering entry). See :meth:`_apply_event`.
        """
        return self._apply_event(live, event, silence_s=silence_s)

    def _apply_event(
        self, live: LiveSession, event: LivenessEvent, *, silence_s: float
    ) -> NodeState:
        """Synchronous core of :meth:`apply_event` (``_close`` needs it inline).

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
        live.writer.enqueue(
            "set_node_state",
            WriteKind.STATE,
            functools.partial(
                self._registry.set_node_state,
                live.node_id,
                new,
                session_id=live.session_id,
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

    # --------------------------------------------------------------- closing

    async def end_session(self, live: LiveSession, reason: SessionEndReason) -> None:
        """Close ``live`` (idempotent): unregister, stop the reader, queue the reason.

        Synchronous in memory (no lock, no database await) so the lease timer and
        a reader can close sessions while an admission is waiting on the registry.
        """
        self._close(live, reason)

    def end_session_nowait(self, live: LiveSession, reason: SessionEndReason) -> None:
        """:meth:`end_session` for synchronous callers (a gRPC done-callback)."""
        self._close(live, reason)

    def _close(self, live: LiveSession, reason: SessionEndReason) -> None:
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
        if end_event(reason) is not None and self.admission_pending(live):
            # D28: a newer Hello is being admitted and owns the state; its
            # admission finishes this end (no event) or, if it fails, settles it
            # with the event (``_release_claim``).
            previous = self._claims.defer(live)
            if previous is not None:  # pragma: no cover - one session per node ends
                self._finish_end(previous, fire_event=True)
            log.info(
                "session.end_deferred",
                node_id=live.node_id,
                session_id=live.session_id,
                end_reason=reason.value,
                state=live.state.value,
            )
            return
        self._finish_end(live, fire_event=True)

    def _finish_end(self, live: LiveSession, *, fire_event: bool) -> None:
        """The persistent half of a close: the end's event (D19), then
        ``close_session`` on the chain, lingering if the loss left it SUSPECT.

        ``fire_event=False`` is a deferred end a newer Hello took over (D28): no
        state event, the chain simply ends with ``close_session``.
        """
        reason = live.end_reason
        if reason is None:  # pragma: no cover - _close sets it before any call
            raise RuntimeError("_finish_end called on a session that never closed")
        # D19: the end decides the arrow; the state write goes on this session's
        # chain *before* close_session, so the two can never be reordered.
        event = end_event(reason) if fire_event else None
        if event is not None:
            self._apply_event(
                live, event, silence_s=live.tracker.silence(self._clock())
            )
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
                self._end_linger(previous, cause="replaced")  # admission resolves it
            self._lingering[live.node_id] = live
            live.writer.enqueue("close_session", WriteKind.CLOSE, close)
            self.wake.set()  # the timer must learn the lingering deadline
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
            self._apply_event(entry, event, silence_s=silence)
            if event is LivenessEvent.LEASE_EXPIRED:
                log.warning(
                    "node.lease_expired",
                    node_id=entry.node_id,
                    session_id=entry.session_id,
                    silence_s=round(silence, 3),
                    lingering=True,
                )
                self._end_linger(entry, cause="lease_expired")

    def _end_linger(self, entry: LiveSession, *, cause: str) -> None:
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

    async def shutdown(self, *, drain_s: float = 5.0) -> None:
        """Refuse new Hellos, close every live session as ``controller_shutdown``
        (``STREAM_CLOSED``: OFFLINE), write OFFLINE for every lingering entry and
        end its chain, then let the write chains finish for at most ``drain_s``
        (the close reasons above included). Chains still running after that are
        cancelled and logged ``registry.write_abandoned``; the start-up sweep is
        the backstop for anything they did not persist.
        """
        self.closing = True
        async with self._lock:  # an admission in flight finishes (or refuses) first
            for live in list(self._live.values()):
                self._close(live, SessionEndReason.CONTROLLER_SHUTDOWN)
            # Unreachable in practice (every admission settles its claim before
            # the lock is free again); a backstop so no chain is left without END.
            for deferred in self._claims.drain():  # pragma: no cover
                self._finish_end(deferred, fire_event=True)
            for entry in list(self._lingering.values()):
                # No controller = no liveness claim, exactly like the start-up
                # sweep: a lingering SUSPECT goes OFFLINE now.
                self._apply_event(
                    entry,
                    LivenessEvent.STREAM_CLOSED,
                    silence_s=entry.tracker.silence(self._clock()),
                )
                self._end_linger(entry, cause="controller_shutdown")
        pending = [t for t in self._tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        chains = [c for c in self._chains if not c.done()]
        if chains:
            results = await asyncio.gather(*(c.drain(drain_s) for c in chains))
            abandoned = sum(1 for ok in results if not ok)
            log.info(
                "session.writes_drained",
                chains=len(chains),
                abandoned=abandoned,
                drain_s=drain_s,
            )


def _log_task_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error(
            "session.task_failed",
            task=task.get_name(),
            cause=type(exc).__name__,
            error=str(exc),
        )
