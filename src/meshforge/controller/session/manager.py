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
timer awaits the database: every registry write after admission (``touch_node``,
``set_node_state``, ``record_rtt``, ``close_session``) is *enqueued* on the
session's ordered single-writer chain and performed by that chain's own task, so
a slow or unreachable PostgreSQL delays the *record* of liveness, never the
decision. The chain preserves order (SUSPECT, then ONLINE, then the close), logs a
failed write as ``registry.write_failed`` and continues, and ends after the
session's final write. Admission itself (``get_node`` for fencing, the upsert and
the session row) does await the registry - a Hello needs the answer - and only
ever blocks other Hellos, not readers or the timer. Contract violations by the
agent are :class:`~meshforge.controller.session.errors.SessionProtocolError` and
close the session with ``end_reason = protocol_error``.

Epoch fencing (ADR-0002 Updates §1) compares a Hello against **both** the live
session (if any) and the highest epoch the registry has ever persisted for the
node, so a stale process cannot be admitted by waiting for the winner's stream to
end. A legitimate reconnect always carries a strictly higher epoch (ADR-0013 §3:
the agent increments before every stream), so "equal" is stale too.

Heartbeat persistence is coalesced: the lease is extended in memory on every
heartbeat, but ``node.last_seen_at`` is written at most once per
``heartbeat_interval_s`` per node (the first heartbeat of a window enqueues a
write at once; later ones mark the session dirty and the
:class:`~.leases.LeaseTimer` flushes it when the window closes). A burst of
heartbeats therefore costs one queued DB round-trip, not one per message.

Bounds on peer-controlled input (:mod:`~.limits`): ``message_id`` must be a
``cmd_<ULID>`` (so the dedupe window holds at most ``dedupe_window`` x 30 bytes)
and the Hello's string fields are capped (and must be printable, D22) before
anything is persisted.

**How a session ends decides the liveness arrow** (ARCHITECTURE §12.3 2026-09-24
amendment, D19). Every end goes through :meth:`SessionManager._close`, which is
idempotent (first reason wins) and maps the reason to an event:

* clean - ``agent_closed`` (the agent half-closed) and ``controller_shutdown`` ->
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

**Half-close is confirmed, not assumed.** grpc.aio (1.84, measured) ends the
server's request iterator *normally* for a client half-close **and** for a
cancelled call, a closed channel or a killed client process; what distinguishes
the abrupt cases is that the handler task is then cancelled (about one loop
iteration later). So the reader treats a normal end of the iterator as a
*candidate* clean close and waits ``half_close_confirm_s`` before recording
``agent_closed``; if the handler is cancelled meanwhile, the servicer closes the
session ``stream_lost`` first and cancels the reader. A half-closed call stays
open until we send the status (the agent waits up to 2 s for it), so a genuine
half-close always survives the window.

**A pending Hello owns the state** (D28). A ``Hello`` for node N that is admitted
while N's previous session is still ending decides N's state; the previous
session then ends with **no** state event, exactly like a supersession. From the
moment :meth:`SessionManager.admit` has validated a Hello until its admission
returns, the Hello's epoch sits in ``_pending_hellos``; while an epoch *higher*
than a session's is pending, that session is "claimed" (:meth:`SessionManager.
admission_pending`):

* its end is **deferred**: ``_close`` still does everything in memory (unregister,
  unblock the handler, stop the reader, record the reason) but parks the session
  in ``_deferred_ends`` instead of applying the end's event and writing
  ``close_session``;
* the lease timer skips it (live or lingering): no ``LEASE_MISSED``/``EXPIRED``
  is decided against a node whose Hello is being admitted.

The admission then takes ``prior`` from the claimed session's **in-memory** state
(``old`` / deferred / lingering, in that order) - never from the database row it
read before the close - and, once the new session is persisted, ends the deferred
session with no event (its reason is kept: ``agent_closed`` is still how that
stream ended). If the admission does not succeed (rejected, stale, registry
error, shutdown), its ``finally`` settles the deferred end with the event it
would have fired (a genuine clean close still writes OFFLINE, only later) and
wakes the lease timer. A **stale** Hello (epoch not above the session's) claims
nothing, so it can never suppress a real close. The deferral is bounded by one
admission's registry round trips; shutdown never defers.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Final

import grpc
from google.protobuf.json_format import MessageToDict

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import new_command_id, new_session_id, validate_node_id
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
from meshforge.controller.scores import scores_document
from meshforge.controller.session.epoch import MAX_SESSION_EPOCH
from meshforge.controller.session.errors import (
    OutboundOverflowError,
    SessionProtocolError,
)
from meshforge.controller.session.limits import (
    check_hardware,
    check_hardware_scores,
    check_hello_strings,
    check_resource_update,
    validate_message_id,
)
from meshforge.controller.session.writer import (
    DEFAULT_WRITE_CAPACITY,
    WriteChain,
    WriteKind,
)

__all__ = [
    "HALF_CLOSE_CONFIRM_S",
    "RTT_EWMA_ALPHA",
    "Accepted",
    "AdmitResult",
    "LiveSession",
    "Rejected",
    "SessionManager",
    "end_event",
    "update_ewma",
]

log = get_logger(__name__)

RTT_EWMA_ALPHA: Final = 0.3
"""Weight of the newest RTT sample in ``rtt_ewma_ms`` (roadmap 0.3.10)."""

HALF_CLOSE_CONFIRM_S: Final = 0.25
"""How long a normal end of the request iterator must go without the call being
cancelled before it counts as the agent's half-close (see the module docstring).
Must stay well below the agent's own close grace (2 s), which is how long a
half-closed agent waits for our final status."""

_CLEAN_END: Final[frozenset[SessionEndReason]] = frozenset(
    {SessionEndReason.AGENT_CLOSED, SessionEndReason.CONTROLLER_SHUTDOWN}
)
_ABRUPT_END: Final[frozenset[SessionEndReason]] = frozenset(
    {
        SessionEndReason.STREAM_LOST,
        SessionEndReason.PROTOCOL_ERROR,
        SessionEndReason.INTERNAL_ERROR,
    }
)


def end_event(reason: SessionEndReason) -> LivenessEvent | None:
    """The §12.3 event a session end fires (D19); None = no state write.

    ``superseded`` fires nothing (the new session's Hello owns the state),
    ``lease_expired`` nothing extra (the timer already applied the expiry) and
    ``controller_restart`` is only ever written by the start-up sweep.
    """
    if reason in _CLEAN_END:
        return LivenessEvent.STREAM_CLOSED
    if reason in _ABRUPT_END:
        return LivenessEvent.STREAM_LOST
    return None


RejectCode = agent_pb2.HelloAck.RejectCode

_UNACKED: Final = frozenset({"ack", "pong"})
"""Agent payloads that get no Ack back (ADR-0002 Updates §4: never buffered)."""


def update_ewma(
    previous: float | None, sample: float, alpha: float = RTT_EWMA_ALPHA
) -> float:
    """Exponentially weighted moving average; the first sample seeds it."""
    if previous is None:
        return sample
    return alpha * sample + (1.0 - alpha) * previous


class _SeenIds:
    """Bounded ``message_id`` window (deque for order + set for O(1) lookup)."""

    __slots__ = ("_order", "_set")

    def __init__(self, capacity: int) -> None:
        self._order: deque[str] = deque(maxlen=capacity)
        self._set: set[str] = set()

    def remember(self, message_id: str) -> bool:
        """Record ``message_id``; return True if it was already present."""
        if message_id in self._set:
            return True
        if self._order.maxlen is not None and len(self._order) == self._order.maxlen:
            self._set.discard(self._order[0])
        self._order.append(message_id)
        self._set.add(message_id)
        return False

    def __len__(self) -> int:
        return len(self._set)


@dataclass(eq=False, slots=True)
class LiveSession:
    """In-memory state of one accepted stream. Owned by :class:`SessionManager`."""

    node_id: str
    session_id: str
    session_epoch: int
    peer_address: str
    tracker: LeaseTracker
    state: NodeState
    outbound: asyncio.Queue[agent_pb2.ControllerMessage | None]
    seen: _SeenIds
    next_ping_at: float
    writer: WriteChain
    """This session's ordered registry-write chain (see module docstring)."""
    reader_task: asyncio.Task[None] | None = None
    ended: bool = False
    end_reason: SessionEndReason | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    lingering: bool = False
    """The stream ended abruptly and the lease is still running (D19): the entry
    sits in ``SessionManager._lingering`` until a Hello, the lease or a shutdown
    resolves it. Its ``writer`` stays open until then."""
    # counters
    heartbeats: int = 0
    stale_messages: int = 0
    duplicates: int = 0
    resource_updates: int = 0
    resource_updates_clamped: int = 0
    """Reports that broke a D12 cap; logged once, counted always."""
    hardware_scores_received: int = 0
    hardware_scores_rejected: int = 0
    """Score sets (Hello or report) dropped by D38 validation; each is logged."""
    # RTT (roadmap 0.3.10) - monotonic clock only
    outstanding_ping: tuple[str, float] | None = None
    last_rtt_ms: float | None = None
    rtt_ewma_ms: float | None = None
    pings_sent: int = 0
    pongs_matched: int = 0
    # coalesced ``last_seen_at`` persistence (monotonic clock only)
    last_touch_at: float | None = None
    """When the last ``touch_node`` write was issued; None before the first."""
    touch_dirty: bool = False
    """Heartbeats arrived since ``last_touch_at`` that are not yet persisted."""

    def touch_due_at(self, interval_s: float) -> float | None:
        """Monotonic time the pending ``last_seen_at`` write is due (None if clean)."""
        if not self.touch_dirty or self.last_touch_at is None:
            return None
        return self.last_touch_at + interval_s


@dataclass(frozen=True, slots=True)
class Accepted:
    live: LiveSession
    hello_ack: agent_pb2.ControllerMessage


@dataclass(frozen=True, slots=True)
class Rejected:
    """Hello refused. ``abort_status`` set = close with that gRPC status instead of a
    HelloAck (malformed/forbidden Hello); otherwise a ``HelloAck{accepted=false}``."""

    reject_code: agent_pb2.HelloAck.RejectCode.ValueType
    reason: str
    abort_status: grpc.StatusCode | None = None


type AdmitResult = Accepted | Rejected


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
        half_close_confirm_s: float = HALF_CLOSE_CONFIRM_S,
    ) -> None:
        if not half_close_confirm_s >= 0:
            raise ValueError("half_close_confirm_s must be >= 0")
        self._registry = registry
        self._resources = resources
        """Where ResourceUpdates go (roadmap 0.3.11); None = not recorded."""
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
        self._pending_hellos: dict[str, list[int]] = {}
        """Epochs of validated Hellos whose admission has not returned, by node (D28)."""
        self._deferred_ends: dict[str, LiveSession] = {}
        """Sessions that ended while a newer Hello was pending, by node (D28)."""
        self._half_close_confirm_s = half_close_confirm_s
        self._tasks: set[asyncio.Task[None]] = set()
        self._chains: set[WriteChain] = set()
        """Write chains not yet finished (closed sessions included until drained)."""
        self.wake = asyncio.Event()
        """Set whenever a session registers; the lease timer waits on it."""
        self.stale_messages_total = 0
        self.hardware_scores_rejected_total = 0
        """Score sets dropped by D38 validation across all sessions."""
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

    def admission_pending(self, live: LiveSession) -> bool:
        """True while a Hello with a higher epoch than ``live``'s is being admitted
        for its node: that Hello owns the node's state (D28, module docstring)."""
        if self.closing:
            return False
        pending = self._pending_hellos.get(live.node_id)
        return pending is not None and max(pending) > live.session_epoch

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
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                "controller is shutting down",
                grpc.StatusCode.UNAVAILABLE,
            )
        # TRUST POINT (until V0.7 mTLS, ADR-0008): ``Hello.node_id`` is a claim
        # accepted on the trusted LAN (ADR-0013 §2). Pairing binds node_id -> key
        # -> certificate; then the transport, not this field, proves identity.
        try:
            validate_node_id(hello.node_id)
        except InvalidIdError as exc:
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                str(exc),
                grpc.StatusCode.INVALID_ARGUMENT,
            )
        too_long = check_hello_strings(
            hostname=hello.hostname,
            os=hello.os,
            platform=hello.platform,
            agent_version=hello.agent_version,
        )
        if too_long is not None:
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                too_long,
                grpc.StatusCode.INVALID_ARGUMENT,
            )
        # D12: the hardware summary is part of the enrolment contract - over a cap
        # the Hello is refused and nothing is persisted. The agent clips to the
        # same numbers before sending, so this only fires on a hostile or broken
        # peer (roadmap 0.3.3, G2 review follow-up on unbounded `hardware` JSON).
        oversized = check_hardware(hello.hardware)
        if oversized is not None:
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                oversized,
                grpc.StatusCode.INVALID_ARGUMENT,
            )
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
        self._pending_hellos.setdefault(node_id, []).append(hello.session_epoch)
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
        pending = self._pending_hellos[node_id]
        pending.remove(epoch)
        if not pending:
            del self._pending_hellos[node_id]
        deferred = self._deferred_ends.get(node_id)
        if deferred is not None and not self.admission_pending(deferred):
            del self._deferred_ends[node_id]
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
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                "controller is shutting down",
                grpc.StatusCode.UNAVAILABLE,
            )
        # Read the live and lingering entries *after* the await: the lease timer
        # and readers close sessions without the lock, so both tables may have
        # changed while we waited.
        old = self._live.get(node_id)
        deferred = self._deferred_ends.get(node_id)
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
        event = _hello_event(prior)
        state = prior if event is None else next_state(prior, event)

        now = self._clock()
        session_id = new_session_id()
        scores, scores_rejected = self._hello_scores(hello, session_id)
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
        if deferred is not None and self._deferred_ends.get(node_id) is deferred:
            # Likewise only now: this Hello owns the state, so the deferred end
            # fires no event (D28). Had admission failed, ``_release_claim``
            # would have settled it with its event instead.
            del self._deferred_ends[node_id]
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
            seen=_SeenIds(self._dedupe_window),
            next_ping_at=now,  # first RTT sample right away
            writer=writer,
            hardware_scores_received=int(scores is not None),
            hardware_scores_rejected=int(scores_rejected),
        )
        self._chains.add(writer)
        writer.start().add_done_callback(lambda _t: self._chains.discard(writer))
        self._live[node_id] = live
        if self._resources is not None:
            # A new session may be a *restarted* agent process, whose report
            # sequence starts at 1 again. Arm a cursor reset; this session's
            # Reconcile (if it announces a replay) cancels it before any report.
            # The epoch goes with it so the sink's zombie fence is armed from the
            # first instant of the session, not only from its first report.
            self._resources.begin_session(node_id, 0, session_epoch=hello.session_epoch)
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
        return Accepted(live, self._hello_ack(session_id))

    def _hello_scores(
        self, hello: agent_pb2.Hello, session_id: str
    ) -> tuple[dict[str, Any] | None, bool]:
        """The score document a Hello carries, validated (D38); ``(None, rejected)``.

        A Hello without scores - or with scores that fail validation - stores
        ``NULL`` (pending). A bad score set never refuses the Hello: scores are
        optional, enrolment is not (it is logged and counted instead).
        """
        if not hello.HasField("hardware_scores"):
            return None, False
        reason = check_hardware_scores(hello.hardware_scores)
        if reason is not None:
            self._reject_scores(hello.node_id, session_id, "hello", reason)
            return None, True
        document, _measured_at = scores_document(hello.hardware_scores)
        return document, False

    def _reject_scores(
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

    def _hello_ack(self, session_id: str) -> agent_pb2.ControllerMessage:
        ack = agent_pb2.HelloAck(
            accepted=True,
            session_id=session_id,
            heartbeat_interval_ms=round(self._liveness.heartbeat_interval_s * 1000),
            lease_ms=round(self._liveness.offline_after_s * 1000),
        )
        ack.controller_time.FromDatetime(self._wall_clock())
        return agent_pb2.ControllerMessage(message_id=new_command_id(), hello_ack=ack)

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
        """Consume the agent's messages; classify how the stream ended (D19).

        Only a *normal* end of the request iterator can be a clean close, and even
        that is confirmed first (module docstring: grpc.aio also ends the iterator
        normally when the call is cancelled). Everything else is abrupt.
        """
        try:
            async for msg in request_iterator:
                if live.ended:
                    return  # closed by supersede/lease/shutdown/handler meanwhile
                await self.handle_message(live, msg)
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
            # Candidate half-close. If the call was in fact cancelled, the gRPC
            # handler is cancelled within this window and closes the session
            # ``stream_lost`` first, which cancels this sleep (first reason wins).
            log.debug(
                "session.request_stream_ended",
                node_id=live.node_id,
                session_id=live.session_id,
                confirm_s=self._half_close_confirm_s,
            )
            await asyncio.sleep(self._half_close_confirm_s)
            reason = SessionEndReason.AGENT_CLOSED
        await self.end_session(live, reason)

    # -------------------------------------------------------------- dispatch

    async def handle_message(
        self, live: LiveSession, msg: agent_pb2.AgentMessage
    ) -> None:
        """Process one agent message on ``live`` (idempotent per ``message_id``)."""
        which = msg.WhichOneof("payload")
        if which == "hello":
            raise SessionProtocolError(
                "second Hello on an open stream",
                session_id=live.session_id,
                node_id=live.node_id,
            )
        if msg.session_id != live.session_id:
            live.stale_messages += 1
            self.stale_messages_total += 1
            log.warning(
                "session.stale_message",
                node_id=live.node_id,
                session_id=live.session_id,
                message_session_id=msg.session_id,
                payload=which,
                count=live.stale_messages,
            )
            return
        if which is None:
            raise SessionProtocolError(
                "message without payload",
                session_id=live.session_id,
                node_id=live.node_id,
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
            await self._dispatch(live, which, msg)
        if which not in _UNACKED:
            # Receipts (Ack) and probe replies (Pong) are never acknowledged - they
            # are never buffered or replayed, and acking them would ping-pong
            # forever. Every other message is, duplicates included: the agent may
            # have missed the first Ack.
            self.send(live, self._ack(live, msg.message_id))

    async def _dispatch(
        self, live: LiveSession, which: str, msg: agent_pb2.AgentMessage
    ) -> None:
        if which == "heartbeat":
            now = self._clock()
            live.tracker.observe_heartbeat(now)
            live.heartbeats += 1
            if live.state is NodeState.SUSPECT:
                await self.apply_event(live, LivenessEvent.HEARTBEAT, silence_s=0.0)
            self._touch(live, now, force=False)
        elif which == "pong":
            self._on_pong(live, msg.pong.ping_id)
        elif which == "ack":
            log.debug(
                "session.command_acked",
                node_id=live.node_id,
                session_id=live.session_id,
                acked_message_id=msg.ack.acked_message_id,
            )
        elif which == "resource_update":
            self._on_resource_update(live, msg.resource_update)
        elif which == "reconcile":
            self._on_reconcile(live, msg.reconcile)
        elif which == "hardware_scores":
            self._on_hardware_scores(live, msg.hardware_scores)
        else:
            # cache_report / ... : accepted and acked; their handlers arrive with
            # the versions that own them (V0.4+).
            log.debug(
                "session.message",
                node_id=live.node_id,
                session_id=live.session_id,
                payload=which,
            )

    def _on_reconcile(self, live: LiveSession, reconcile: agent_pb2.Reconcile) -> None:
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

    def _on_resource_update(
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

    def _on_hardware_scores(
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
        reason = check_hardware_scores(scores)
        if reason is not None:
            live.hardware_scores_rejected += 1
            self._reject_scores(live.node_id, live.session_id, "report", reason)
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

    def _touch(self, live: LiveSession, now: float, *, force: bool) -> None:
        """Queue a ``last_seen_at`` write at most once per ``heartbeat_interval_s``.

        Coalescing: the first heartbeat of a window enqueues a write immediately;
        the rest only mark the session dirty and the timer's flush (``force``)
        enqueues one write when the window closes. Asynchrony: the write itself
        runs on the session's :class:`~.writer.WriteChain`, so this method never
        awaits and a stalled database cannot hold up the reader. The persisted
        value is the write's wall time, at most one interval (plus queueing) after
        the heartbeat it stands for - liveness itself is decided in memory.
        """
        interval = self._liveness.heartbeat_interval_s
        window_open = live.last_touch_at is not None and (
            now - live.last_touch_at < interval
        )
        if window_open and not force:
            live.touch_dirty = True
            return
        live.last_touch_at = now
        live.touch_dirty = False
        live.writer.enqueue(
            "touch_node",
            WriteKind.TOUCH,
            functools.partial(self._registry.touch_node, live.node_id),
        )

    async def flush_touch(self, live: LiveSession, now: float) -> None:
        """Timer hook: queue a pending ``last_seen_at`` write once its window closed."""
        due = live.touch_due_at(self._liveness.heartbeat_interval_s)
        if due is None or now < due or live.ended:
            return
        self._touch(live, now, force=True)

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
        ping_id = new_command_id()
        live.outstanding_ping = (ping_id, now)
        live.next_ping_at = now + self._liveness.offline_after_s
        live.pings_sent += 1
        try:
            self.send(
                live,
                agent_pb2.ControllerMessage(
                    message_id=ping_id,
                    session_id=live.session_id,
                    ping=agent_pb2.Ping(ping_id=ping_id),
                ),
            )
        except OutboundOverflowError as exc:
            log.warning(
                "session.outbound_overflow",
                node_id=live.node_id,
                session_id=live.session_id,
                capacity=exc.details["capacity"],
            )
            await self.end_session(live, SessionEndReason.PROTOCOL_ERROR)

    def _on_pong(self, live: LiveSession, ping_id: str) -> None:
        outstanding = live.outstanding_ping
        if outstanding is None or outstanding[0] != ping_id:
            log.debug(
                "session.pong_unmatched",
                node_id=live.node_id,
                session_id=live.session_id,
                ping_id=ping_id,
            )
            return
        rtt_ms = (self._clock() - outstanding[1]) * 1000.0
        live.outstanding_ping = None
        live.last_rtt_ms = rtt_ms
        live.rtt_ewma_ms = update_ewma(live.rtt_ewma_ms, rtt_ms)
        live.pongs_matched += 1
        log.debug(
            "session.rtt",
            node_id=live.node_id,
            session_id=live.session_id,
            rtt_ms=round(rtt_ms, 3),
            rtt_ewma_ms=round(live.rtt_ewma_ms, 3),
        )
        live.writer.enqueue(
            "record_rtt",
            WriteKind.RTT,
            functools.partial(
                self._registry.record_rtt,
                live.session_id,
                last_ms=rtt_ms,
                ewma_ms=live.rtt_ewma_ms,
            ),
        )

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
            previous = self._deferred_ends.get(live.node_id)
            if previous is not None:  # pragma: no cover - one session per node ends
                self._finish_end(previous, fire_event=True)
            self._deferred_ends[live.node_id] = live
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
            heartbeats=live.heartbeats,
            stale_messages=live.stale_messages,
            duplicates=live.duplicates,
            pending_writes=live.writer.pending,
            last_rtt_ms=None
            if live.last_rtt_ms is None
            else round(live.last_rtt_ms, 3),
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
            for deferred in list(self._deferred_ends.values()):  # pragma: no cover
                del self._deferred_ends[deferred.node_id]
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


def _hello_event(prior: NodeState) -> LivenessEvent | None:
    """Which §12.3 arrow an accepted Hello is, given the node's previous state."""
    if prior is NodeState.ENROLLING:
        return LivenessEvent.HELLO_ACCEPTED
    if prior in (NodeState.SUSPECT, NodeState.OFFLINE):
        return LivenessEvent.RECONNECTED
    if prior is NodeState.ONLINE:
        return LivenessEvent.HEARTBEAT  # superseding stream: self-loop
    return None  # DRAINING keeps draining; REVOKED is rejected before this


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
