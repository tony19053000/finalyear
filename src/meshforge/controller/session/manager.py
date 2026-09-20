"""Live-session table and message dispatch for ``AgentControl.Session`` (0.3.6/0.3.10).

One :class:`LiveSession` per connected ``node_id``; the table is a ``dict`` behind a
single ``asyncio.Lock`` (admission, supersede and close all take it). Each stream has
exactly one reader task (created by :meth:`SessionManager.start_reader`) and one
bounded outbound queue drained by the gRPC handler; the two never share a coroutine.

Time: every lease/RTT computation uses the injected monotonic ``clock`` (the event
loop's ``time()`` in production). The wall clock appears only in ``HelloAck.
controller_time`` and in the registry's database timestamps.

Failure handling: driver errors from the registry are typed
(:class:`~meshforge.controller.registry.RegistryError`) and logged; they never tear
down a session on their own - liveness is decided in memory and the database is
brought up to date on the next write. Contract violations by the agent are
:class:`~meshforge.controller.session.errors.SessionProtocolError` and close the
session with ``end_reason = protocol_error``.

Epoch fencing (ADR-0002 Updates §1) compares a Hello against **both** the live
session (if any) and the highest epoch the registry has ever persisted for the
node, so a stale process cannot be admitted by waiting for the winner's stream to
end. A legitimate reconnect always carries a strictly higher epoch (ADR-0013 §3:
the agent increments before every stream), so "equal" is stale too.

Heartbeat persistence is coalesced: the lease is extended in memory on every
heartbeat, but ``node.last_seen_at`` is written at most once per
``heartbeat_interval_s`` per node (the first heartbeat of a window writes at once;
later ones mark the session dirty and the :class:`~.leases.LeaseTimer` flushes it
when the window closes). A burst of heartbeats therefore costs one DB round-trip,
not one per message, and can no longer outrun its own lease.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any, Final

import grpc
from google.protobuf.json_format import MessageToDict

from meshforge.common.errors import InvalidIdError, MeshForgeError
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
from meshforge.controller.session.epoch import MAX_SESSION_EPOCH
from meshforge.controller.session.errors import (
    OutboundOverflowError,
    SessionProtocolError,
)

__all__ = [
    "RTT_EWMA_ALPHA",
    "Accepted",
    "AdmitResult",
    "LiveSession",
    "Rejected",
    "SessionManager",
    "update_ewma",
]

log = get_logger(__name__)

RTT_EWMA_ALPHA: Final = 0.3
"""Weight of the newest RTT sample in ``rtt_ewma_ms`` (roadmap 0.3.10)."""

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
    reader_task: asyncio.Task[None] | None = None
    ended: bool = False
    end_reason: SessionEndReason | None = None
    done: asyncio.Event = field(default_factory=asyncio.Event)
    # counters
    heartbeats: int = 0
    stale_messages: int = 0
    duplicates: int = 0
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
        wall_clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._registry = registry
        self._liveness = liveness
        self._clock = clock
        self._wall_clock = wall_clock or (lambda: dt.datetime.now(dt.UTC))
        self._versions = accepted_protocol_versions
        self._queue_size = outbound_queue_size
        self._dedupe_window = dedupe_window
        self._lock = asyncio.Lock()
        self._live: dict[str, LiveSession] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._writes: set[asyncio.Task[None]] = set()
        """In-flight registry writes; never cancelled (see :meth:`_persist`)."""
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

    def __len__(self) -> int:
        return len(self._live)

    # -------------------------------------------------------------- admission

    async def admit(self, hello: agent_pb2.Hello, peer_address: str) -> AdmitResult:
        """Apply the Hello rules (ADR-0002 Updates §1) and register the session.

        Holds the table lock for the whole admission (including the registry
        writes) so two Hellos for one node are strictly ordered. Hellos are rare;
        simplicity wins over a per-node lock here.
        """
        if self.closing:
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                "controller is shutting down",
                grpc.StatusCode.UNAVAILABLE,
            )
        try:
            validate_node_id(hello.node_id)
        except InvalidIdError as exc:
            return Rejected(
                RejectCode.REJECT_CODE_INTERNAL,
                str(exc),
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
        async with self._lock:
            return await self._admit_locked(hello, peer_address)

    async def _admit_locked(
        self, hello: agent_pb2.Hello, peer_address: str
    ) -> AdmitResult:
        node_id = hello.node_id
        old = self._live.get(node_id)
        # One read per Hello: the registry's high-water mark. Fencing must hold
        # across disconnects, not only against a live competitor (ADR-0002 §1).
        record = await self._registry.get_node(node_id)
        highest = -1 if record is None else record.session_epoch
        if old is not None:
            highest = max(highest, old.session_epoch)
        if hello.session_epoch <= highest:
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
            await self._end_session_locked(old, SessionEndReason.SUPERSEDED)
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
        await self._registry.upsert_node_on_hello(
            node_id=node_id,
            hostname=hello.hostname,
            os=hello.os,
            platform=hello.platform,
            hardware=MessageToDict(hello.hardware, preserving_proto_field_name=True),
            session_epoch=hello.session_epoch,
            session_id=session_id,
            state=state,
        )
        await self._registry.open_session(
            session_id=session_id,
            node_id=node_id,
            session_epoch=hello.session_epoch,
            peer_address=peer_address,
            agent_version=hello.agent_version,
            protocol_version=hello.protocol_version,
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
        )
        self._live[node_id] = live
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
        reason = SessionEndReason.AGENT_CLOSED
        try:
            async for msg in request_iterator:
                if live.ended:
                    break
                await self.handle_message(live, msg)
        except asyncio.CancelledError:
            raise  # ended by supersede/lease/shutdown; they close the session
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
        except Exception:
            # A bug in dispatch must not leave a zombie session behind; the stream
            # is closed and the traceback logged. protocol_error is the closest
            # persisted reason (see Follow-ups: an internal_error reason).
            log.exception(
                "session.reader_crashed",
                node_id=live.node_id,
                session_id=live.session_id,
            )
            reason = SessionEndReason.PROTOCOL_ERROR
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
            await self._touch(live, now, force=False)
        elif which == "pong":
            await self._on_pong(live, msg.pong.ping_id)
        elif which == "ack":
            log.debug(
                "session.command_acked",
                node_id=live.node_id,
                session_id=live.session_id,
                acked_message_id=msg.ack.acked_message_id,
            )
        else:
            # reconcile / resource_update / cache_report / ... : accepted and acked;
            # their handlers arrive with the versions that own them (G4, V0.4+).
            log.debug(
                "session.message",
                node_id=live.node_id,
                session_id=live.session_id,
                payload=which,
            )

    async def _touch(self, live: LiveSession, now: float, *, force: bool) -> None:
        """Persist ``last_seen_at`` at most once per ``heartbeat_interval_s``.

        The first heartbeat of a window writes immediately; the rest only mark the
        session dirty. ``force`` is the timer's flush: it writes whenever dirty.
        The persisted value is the write's wall time, at most one interval after
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
        await self._persist(
            self._registry.touch_node(live.node_id), live, "registry.touch_failed"
        )

    async def flush_touch(self, live: LiveSession, now: float) -> None:
        """Timer hook: write a pending ``last_seen_at`` once its window has closed."""
        due = live.touch_due_at(self._liveness.heartbeat_interval_s)
        if due is None or now < due or live.ended:
            return
        await self._touch(live, now, force=True)

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

    async def _on_pong(self, live: LiveSession, ping_id: str) -> None:
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
        await self._persist(
            self._registry.record_rtt(
                live.session_id, last_ms=rtt_ms, ewma_ms=live.rtt_ewma_ms
            ),
            live,
            "registry.rtt_failed",
        )

    # ------------------------------------------------------------- liveness

    async def apply_event(
        self, live: LiveSession, event: LivenessEvent, *, silence_s: float
    ) -> NodeState:
        """Apply a §12.3 arrow to ``live``; persist and log a real change.

        An :class:`InvalidTransitionError` is a bug in the caller, not in the agent:
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
        await self._persist(
            self._registry.set_node_state(live.node_id, new),
            live,
            "registry.state_persist_failed",
        )
        log.info(
            "node.state_changed",
            node_id=live.node_id,
            session_id=live.session_id,
            **{"from": old.value, "to": new.value},
            silence_s=round(silence_s, 3),
            liveness_event=event.value,
        )
        return new

    # --------------------------------------------------------------- closing

    async def end_session(self, live: LiveSession, reason: SessionEndReason) -> None:
        """Close ``live`` (idempotent): unregister, stop both tasks, persist the reason."""
        async with self._lock:
            await self._end_session_locked(live, reason)

    async def _end_session_locked(
        self, live: LiveSession, reason: SessionEndReason
    ) -> None:
        if live.ended:
            return
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
        await self._persist(
            self._registry.close_session(live.session_id, reason),
            live,
            "registry.close_failed",
        )
        log.info(
            "session.closed",
            node_id=live.node_id,
            session_id=live.session_id,
            end_reason=reason.value,
            heartbeats=live.heartbeats,
            stale_messages=live.stale_messages,
            duplicates=live.duplicates,
            last_rtt_ms=None
            if live.last_rtt_ms is None
            else round(live.last_rtt_ms, 3),
        )

    async def shutdown(self) -> None:
        """Refuse new Hellos and close every live session as ``controller_shutdown``."""
        self.closing = True
        async with self._lock:
            for live in list(self._live.values()):
                await self._end_session_locked(
                    live, SessionEndReason.CONTROLLER_SHUTDOWN
                )
        pending = [t for t in self._tasks if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        # Let in-flight writes (the close reasons above included) reach the database.
        writes = [t for t in self._writes if not t.done()]
        if writes:
            await asyncio.gather(*writes, return_exceptions=True)

    # --------------------------------------------------------------- helpers

    async def _persist(
        self, op: Coroutine[Any, Any, Any], live: LiveSession, log_event: str
    ) -> None:
        """Await a registry write; a typed failure is logged, never propagated.

        The write runs in its own task and is *shielded*: a reader task cancelled
        mid-write (supersede, lease expiry, shutdown) must not abandon a pooled
        connection half-way through a statement. The caller still observes its
        own cancellation; the write finishes (or fails and is logged) on its own.
        """
        task = asyncio.create_task(
            self._persist_logged(op, live, log_event),
            name=f"registry-write:{live.session_id}",
        )
        self._writes.add(task)
        task.add_done_callback(self._writes.discard)
        task.add_done_callback(_log_task_failure)
        await asyncio.shield(task)

    async def _persist_logged(
        self, op: Coroutine[Any, Any, Any], live: LiveSession, log_event: str
    ) -> None:
        try:
            await op
        except MeshForgeError as exc:
            log.exception(
                log_event,
                node_id=live.node_id,
                session_id=live.session_id,
                error=str(exc),
                failure_type=exc.failure_type.value,
                retryable=exc.retryable,
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
