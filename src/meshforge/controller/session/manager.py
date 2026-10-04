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
amendment, D19). Every end goes through :meth:`.ends.SessionEnds.close`, which is
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

**Admission has a deadline** (D41): the lock wait and the registry round trips
of one Hello run as their own task under ``admission_timeout_s`` (< the lease).
Past it the Hello gets ``HelloAck{REJECT_CODE_UNAVAILABLE}`` (retryable), the
claim is released and a deferred end settled with its event, so a hung database
suppresses a node's lease events for at most that long. An admission cut short
after it may have written the node row becomes a lingering entry (SUSPECT, then
OFFLINE at the lease edge) so the row never reads ONLINE for a session that does
not exist (:meth:`SessionManager._abandon`).

**Dispatch is a table** (D42): :meth:`SessionManager.handle_message` does the
envelope work (stale session, ``message_id``, dedupe, Ack) and calls the handler
registered for the payload in :class:`~.dispatch.DispatchTable`; report
handlers and their counters live in :class:`~.reports.ReportHandlers`.

**Commands (V0.4 D64)** - everything written to a session's outbound queue
(commands, Acks, deferred Acks for ``attempt_status``) and the job-layer hooks
are :class:`~.commands.CommandChannel` (``self.commands``); its module docstring
is the full rule.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import AsyncIterator, Callable
from typing import Any

import grpc
from google.protobuf.json_format import MessageToDict

from meshforge.common.ids import new_session_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.liveness import (
    LeaseTracker,
    LivenessConfig,
    LivenessEvent,
    NodeState,
    next_state,
)
from meshforge.controller.registry import Registry, SessionEndReason
from meshforge.controller.resources import ResourceSink
from meshforge.controller.session.admission import (
    DEFAULT_ADMISSION_TIMEOUT_S,
    AdmissionAttempt,
    RejectCode,
    check_hello,
    hello_ack,
    hello_event,
    peer_endpoint_of,
    shutting_down,
    unavailable,
)
from meshforge.controller.session.claims import AdmissionClaims
from meshforge.controller.session.commands import STREAMED, UNACKED, CommandChannel
from meshforge.controller.session.dispatch import (
    DispatchTable,
    check_envelope,
    log_unhandled,
)
from meshforge.controller.session.ends import SessionEnds
from meshforge.controller.session.epoch import MAX_SESSION_EPOCH
from meshforge.controller.session.errors import (
    OutboundOverflowError,
    SessionProtocolError,
)
from meshforge.controller.session.heartbeats import HeartbeatRecorder
from meshforge.controller.session.hooks import SessionHooks
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
from meshforge.controller.session.writer import DEFAULT_WRITE_CAPACITY, WriteChain

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
        admission_timeout_s: float = DEFAULT_ADMISSION_TIMEOUT_S,
        peer_token_key: bytes = b"",
        trust_peer_endpoints: bool = False,
    ) -> None:
        if not admission_timeout_s > 0:
            raise ValueError("admission_timeout_s must be > 0")
        self._admission_timeout_s = admission_timeout_s
        self._peer_token_key = peer_token_key
        """D98: the controller's Ed25519 public key, sent in every HelloAck."""
        self._trust_peer_endpoints = trust_peer_endpoints
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
        self.commands = CommandChannel(
            live=self._live,
            close=lambda live, reason: self._ends.close(live, reason),
            closing=lambda: self.closing,
            queue_size=outbound_queue_size,
        )
        """Outbound messages, commands, Acks and hooks (D64, :mod:`.commands`)."""
        self.send = self.commands.send
        self.send_command = self.commands.send_command
        self.defer_ack = self.commands.defer_ack
        self.ack_message = self.commands.ack_message
        self.dispatch.register("ack", self.commands.on_ack)
        self.dispatch.register("goodbye", self._on_goodbye)
        for payload, handler in self.reports.handlers().items():
            self.dispatch.register(payload, handler)
        self.reports.reconcile_listener = self.commands.on_reconcile
        self._tasks: set[asyncio.Task[Any]] = set()
        self._chains: set[WriteChain] = set()
        """Write chains not yet finished (closed sessions included until drained)."""
        self.wake = asyncio.Event()
        """Set whenever a session registers; the lease timer waits on it."""
        self.stale_messages_total = 0
        self.closing = False
        """True once :meth:`shutdown` started; new Hellos are refused."""
        self._ends = SessionEnds(
            registry,
            clock=clock,
            live=self._live,
            lingering=self._lingering,
            claims=self._claims,
            owned=self.admission_pending,
            wake=self.wake,
            on_offline=self.commands.on_node_offline,
        )
        """What a session end writes (D19/D28, :mod:`.ends`)."""

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
        # D41: and that claim is bounded - the admission (lock wait + registry
        # round trips) runs as its own task under ``admission_timeout_s``; past
        # it the Hello is refused UNAVAILABLE (the agent backs off and retries)
        # and the task is cancelled but not awaited: a hung database must not
        # hold this handler, or the claim, past the deadline.
        node_id = hello.node_id
        attempt = AdmissionAttempt(hello, peer_address)
        self._claims.claim(node_id, hello.session_epoch)
        body = asyncio.create_task(
            self._admit_body(attempt), name=f"session-admit:{node_id}"
        )
        try:
            done, _ = await asyncio.wait({body}, timeout=self._admission_timeout_s)
            if body in done:
                return body.result()  # a registry error propagates as before
            self._abandon(attempt, body)
            log.warning(
                "session.admission_timeout",
                node_id=node_id,
                session_id=attempt.session_id or None,
                hello_epoch=hello.session_epoch,
                timeout_s=self._admission_timeout_s,
                row_written=attempt.row_written,
                peer=peer_address,
            )
            return unavailable(self._admission_timeout_s)
        except BaseException:  # the admission raised, or our handler was cancelled
            self._ends.close_orphaned_admission(body)
            self._abandon(attempt, body)
            raise
        finally:
            self._release_claim(node_id, hello.session_epoch)

    async def _admit_body(self, attempt: AdmissionAttempt) -> AdmitResult:
        async with self._lock:
            return await self._admit_locked(attempt)

    def _abandon(self, attempt: AdmissionAttempt, body: asyncio.Task[Any]) -> None:
        """An admission failed or missed its deadline (D41): cancel its task (never
        awaited here, but tracked so its outcome is logged), then leave the node
        row honest (:meth:`.ends.SessionEnds.abandon_admission`)."""
        if not body.done():
            body.cancel()
            self._track(body)
        self._ends.abandon_admission(
            attempt,
            body,
            lambda a: self._new_session(
                a.hello, a.peer_address, a.session_id, a.state or NodeState.SUSPECT
            ),
        )

    def _release_claim(self, node_id: str, epoch: int) -> None:
        """End one pending Hello's claim; settle a deferred end nobody owns now.

        Runs after the admission returned: a successful one has already consumed
        the deferred end (no event); otherwise the deferred session ends with the
        event its reason fires, as if no Hello had been pending.
        """
        deferred = self._claims.release(node_id, epoch, closing=self.closing)
        if deferred is not None:
            self._ends.finish_end(deferred, fire_event=True)
        # The timer skipped this node's sessions while the claim stood.
        self.wake.set()

    async def _admit_locked(self, attempt: AdmissionAttempt) -> AdmitResult:
        hello, peer_address = attempt.hello, attempt.peer_address
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
            self._ends.close(old, SessionEndReason.SUPERSEDED)
            attempt.superseded = old
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

        session_id = new_session_id()
        scores, scores_rejected = self.reports.hello_scores(hello, session_id)
        attempt.session_id, attempt.state = session_id, state
        attempt.row_written = True  # from here the row may name this session...
        try:
            await self._registry.upsert_node_on_hello(
                node_id=node_id,
                hostname=hello.hostname,
                os=hello.os,
                platform=hello.platform,
                hardware=MessageToDict(
                    hello.hardware, preserving_proto_field_name=True
                ),
                session_epoch=hello.session_epoch,
                session_id=session_id,
                state=state,
                hardware_scores=scores,
            )
        except Exception:
            attempt.row_written = False  # ...unless the store refused it (rolled back)
            raise
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
            self._ends.finish_end(deferred, fire_event=False)
        if lingering is not None:
            # Resolved only now that the new session is persisted: had admission
            # failed above, the old lease would still be running. Nothing is
            # written for it - its fenced writes still queued are stale by now.
            self._ends.end_linger(lingering, cause="reconnected")
        live = self._new_session(hello, peer_address, session_id, state)
        live.hardware_scores_received = int(scores is not None)
        live.hardware_scores_rejected = int(scores_rejected)
        attempt.admitted = True
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
        return Accepted(
            live,
            hello_ack(
                session_id,
                self._liveness,
                self._wall_clock(),
                peer_token_key=self._peer_token_key,
            ),
        )

    def _new_session(
        self, hello: agent_pb2.Hello, peer: str, session_id: str, state: NodeState
    ) -> LiveSession:
        """A LiveSession with a started write chain (tracked until it finishes)."""
        writer = WriteChain(
            node_id=hello.node_id, session_id=session_id, capacity=self._write_capacity
        )
        live = LiveSession(
            node_id=hello.node_id,
            session_id=session_id,
            session_epoch=hello.session_epoch,
            peer_address=peer,
            tracker=LeaseTracker(self._liveness, self._clock),
            state=state,
            outbound=asyncio.Queue(maxsize=self._queue_size),
            seen=SeenIds(self._dedupe_window),
            next_ping_at=self._clock(),  # first RTT sample right away
            writer=writer,
            declared_gpus=declared_gpus(hello.hardware),
        )
        endpoint, refusal = peer_endpoint_of(
            hello, peer, trust=self._trust_peer_endpoints
        )
        live.peer_endpoint = endpoint
        if refusal is not None:
            log.warning(
                "session.peer_endpoint_ignored",
                node_id=hello.node_id,
                session_id=session_id,
                reason=refusal,
            )
        self._chains.add(writer)
        writer.start().add_done_callback(lambda _t: self._chains.discard(writer))
        return live

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

    def _track(self, task: asyncio.Task[Any]) -> None:
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
        if which in STREAMED:
            await self.dispatch.handler_for(which)(live, msg)
            return
        if which in self.commands.deferred:
            # Acked by its handler once stored (module docstring); a duplicate
            # goes to the idempotent handler too, never straight to an Ack.
            if live.seen.remember(msg.message_id):
                live.duplicates += 1
            await self.dispatch.handler_for(which)(live, msg)
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
        if which not in UNACKED:
            # Receipts (Ack) and probe replies (Pong) are never acknowledged - they
            # are never buffered or replayed, and acking them would ping-pong
            # forever. Every other message is, duplicates included: the agent may
            # have missed the first Ack.
            self.commands.ack(live, msg.message_id)

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
        self._ends.close(live, SessionEndReason.AGENT_CLOSED)

    # ------------------------------------------------------------- commands

    @property
    def hooks(self) -> SessionHooks:
        """Callbacks into the job layer (V0.4 G4, :mod:`.hooks`)."""
        return self.commands.hooks

    async def flush_touch(self, live: LiveSession, now: float) -> None:
        """Timer hook: queue a pending ``last_seen_at`` write once its window closed."""
        self.heartbeats.flush_touch(live, now)

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

        Never awaits the registry (see :meth:`.ends.SessionEnds.apply_event`).
        """
        return self._ends.apply_event(live, event, silence_s=silence_s)

    # --------------------------------------------------------------- closing

    async def end_session(self, live: LiveSession, reason: SessionEndReason) -> None:
        """Close ``live`` (idempotent): unregister, stop the reader, queue the reason.

        Synchronous in memory (no lock, no database await) so the lease timer and
        a reader can close sessions while an admission is waiting on the registry.
        """
        self._ends.close(live, reason)

    def end_session_nowait(self, live: LiveSession, reason: SessionEndReason) -> None:
        """:meth:`end_session` for synchronous callers (a gRPC done-callback)."""
        self._ends.close(live, reason)

    def expire_linger(self, entry: LiveSession, now: float) -> None:
        """Lease timer hook: the lingering entry's due lease event (D19)."""
        self._ends.expire_linger(entry, now)

    async def shutdown(self, *, drain_s: float = 5.0) -> None:
        """Refuse new Hellos, close every live session as ``controller_shutdown``
        (``STREAM_CLOSED``: OFFLINE), write OFFLINE for every lingering entry and
        end its chain, then let the write chains finish for at most ``drain_s``
        (the close reasons above included). Chains still running after that are
        cancelled and logged ``registry.write_abandoned``; the start-up sweep is
        the backstop for anything they did not persist.

        It takes the admission lock first, so it can wait up to
        ``admission_timeout_s`` behind an admission in flight (D41: a hung one is
        cancelled at its deadline, which frees the lock) before closing anything
        (D61-5). ``drain_s`` bounds only the write chains after that.
        """
        self.closing = True
        async with self._lock:  # an admission in flight finishes (or refuses) first
            for live in list(self._live.values()):
                self._ends.close(live, SessionEndReason.CONTROLLER_SHUTDOWN)
            # Not expected (every admission settles its claim before the lock is
            # free again); a backstop so no chain is left without END - tested
            # directly in test_session_signals.py.
            for deferred in self._claims.drain():
                self._ends.finish_end(deferred, fire_event=True)
            for entry in list(self._lingering.values()):
                # No controller = no liveness claim, exactly like the start-up
                # sweep: a lingering SUSPECT goes OFFLINE now.
                self._ends.apply_event(
                    entry,
                    LivenessEvent.STREAM_CLOSED,
                    silence_s=entry.tracker.silence(self._clock()),
                )
                self._ends.end_linger(entry, cause="controller_shutdown")
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


def _log_task_failure(task: asyncio.Task[Any]) -> None:
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
