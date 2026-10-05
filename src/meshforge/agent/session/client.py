"""The agent's side of ``AgentControl.Session`` (roadmap 0.3.8): connect, Hello,
heartbeat, answer pings, ack commands, buffer + replay reports, reconnect.

**One task owns the stream.** :meth:`AgentSessionClient.run` is that task: it runs
cycle after cycle (resolve -> epoch bump -> channel -> Hello -> ONLINE -> stream end
-> backoff) until :meth:`request_stop` or a fatal, typed error. The only helper task
is the heartbeat sender, created per accepted stream, referenced, cancelled when the
stream ends, and its exceptions logged. gRPC allows one in-flight write per call,
so every write goes through one ``asyncio.Lock``; a Pong is written by the owner
task the moment the Ping is read, so it never waits behind anything but the write
already on the wire.

**Per attempt** (ADR-0002 Updates §1-§4): ``identity.next_epoch`` is persisted in a
thread *before* the channel opens; ``Hello`` carries the new epoch; ``HelloAck``
must arrive within ``hello_timeout_s``. Accepted -> ``session_id`` goes on every
envelope, heartbeats every ``heartbeat_interval_ms`` with a process-monotonic
``sequence`` and the wall-clock ``agent_time`` (display skew only, never RTT). On a
*re*connect the first messages after HelloAck are ``Reconcile{replay_from_sequence}``
and the replay of every un-acked report in the ring, oldest first.

**Requested stop = Goodbye** (V0.4 D40). On :meth:`AgentSessionClient.request_stop`
with a stream established, the agent writes ``Goodbye{SHUTDOWN}`` as the **last**
message of the stream (every later write on that call is refused with
``InvalidStateError``, which each writer already treats as "the stream ended"),
then half-closes and waits up to ``_CLOSE_GRACE_S`` for the controller's final
status. The controller records ``agent_closed`` (OFFLINE now) only because of the
Goodbye: a stream that simply ends is ``stream_lost`` (SUSPECT, OFFLINE at the lease
edge). A Goodbye is never buffered in the replay ring, so it can never be replayed
onto a later stream.

**Stream end** is classified by :func:`~meshforge.agent.session.policy.stream_end_action`:
transient -> BACKOFF (full jitter, monotonic clock); ``ABORTED superseded`` ->
:class:`~meshforge.agent.session.errors.SupersededError` (exit ``CONFLICT``);
``INVALID_ARGUMENT`` / ``PERMISSION_DENIED`` / ``PROTOCOL_VERSION`` ->
:class:`~meshforge.agent.session.errors.AgentContractError` (exit ``CONFIG_ERROR``).

**Reconcile is the first message of every reconnect - structurally (D21).** Each
accepted stream gets a :class:`_StreamGate`: a per-stream *generation* and an
``asyncio.Event`` that is set right after ``Reconcile`` and the replay went out (at
once on a first connect, which has nothing to reconcile). ``send_report`` and the
heartbeat loop wait for it before writing to the stream, so no suspension point
anywhere in ``_online`` can let a fresh report overtake the ``Reconcile``. After the
wait a report skips its direct write when the generation moved on (the stream
ended - its gate is set on the way out, so a waiter never blocks forever), the
state is not ONLINE, or the replay already carried it: the message is in the ring
either way, and the next stream's replay sends it.

**Execution hooks (V0.4 G4).** Attributes the agent process sets (all optional):
``command_handler`` receives every new ``StartAttempt``/``CancelAttempt``
synchronously *before* its Ack is written, so an Ack always means "the executor
holds it" (D66); ``held_attempts`` fills ``Reconcile.running_attempts``, which is
now sent on **every** accepted stream - on a first connect with
``replay_from_sequence = 0`` and no replay, so the resource sink's behaviour is
unchanged - because after an agent restart the controller must learn that the
new process holds nothing; ``on_report_acked`` hears which buffered report the
controller acknowledged; ``before_goodbye`` is awaited (bounded) on a requested
stop before the Goodbye, so running attempts can report ``agent_shutdown``
first (D52); ``after_reconcile`` is called once a stream's Reconcile and replay
are out, and :meth:`AgentSessionClient.resend_report` writes a still-buffered
report again with its own ``message_id`` - together they let the executor
re-send a final ``AttemptStatus`` until it is acked (V0.4 D75).

**Live logs (V0.4 D71)** never use ``send_report``: :meth:`AgentSessionClient.send_log`
puts a ``LogChunk`` on a separate bounded queue (``[agent] log_queue_chunks``),
and one pump task per accepted stream - after the Reconcile, like the heartbeat -
writes them through the same write lock. A full queue or no stream drops the
chunk (counted); nothing is buffered across a reconnect (the queue is cleared
when a stream ends), so a log flood can neither evict reports from the replay
ring nor delay the replay. The chunk's byte offset lets the controller see the
gap; the final log upload fills it.

Idempotency: a controller command with a foreign ``session_id`` is dropped and
counted (``agent.session.stale_message``); a repeated ``message_id`` is acked again
but has no second effect; a duplicate ``Ack`` for a report already removed from the
ring is ignored.

Time: ``loop.time()`` (injectable ``clock``) for backoff and stream lifetime; the
wall clock only for ``sent_at`` / ``agent_time`` timestamps.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any, Final, Protocol

import grpc

from meshforge.agent.discovery import ControllerResolver, resolver_for
from meshforge.agent.identity import NodeIdentity, next_epoch
from meshforge.agent.session.errors import AgentContractError, SupersededError
from meshforge.agent.session.hello import build_hello
from meshforge.agent.session.policy import (
    Backoff,
    BackoffPolicy,
    HelloRejectAction,
    StreamEndAction,
    hello_reject_action,
    stream_end_action,
)
from meshforge.agent.session.replay import REPORT_PAYLOADS, RecentIds, ReplayRing
from meshforge.agent.session.state import AgentEvent, AgentState, next_state
from meshforge.agent.settings import AgentSettings
from meshforge.common.ids import new_command_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2, agent_pb2_grpc

__all__ = ["AgentSessionClient", "SessionStats", "StreamEnd", "grpc_channel_options"]

log = get_logger(__name__)

_COMMAND_DEDUPE_WINDOW: Final = 10_000
_CLOSE_GRACE_S: Final = 2.0
"""After half-closing on stop, how long to wait for the controller's final status."""
BEFORE_GOODBYE_BOUND_S: Final = 10.0
"""Longest wait for ``before_goodbye`` (running attempts stop within grace + 2 s)."""
_COMMAND_PAYLOADS: Final = frozenset(
    {"start_attempt", "cancel_attempt", "refresh_fetch_token", "gc", "prepare_env"}
)
GOODBYE_DETAIL: Final = "agent stop requested"
"""``Goodbye.detail`` on a requested stop (well inside the controller's bound)."""
#: Bounds on the controller-owned heartbeat interval. Below the minimum the
#: agent would spin; above the maximum a buggy or hostile controller could buy
#: itself days of silence (a value of 10**9 ms is ~11.6 days). Out-of-range
#: values are clamped and logged, never obeyed verbatim.
MIN_HEARTBEAT_INTERVAL_S: Final = 0.1
MAX_HEARTBEAT_INTERVAL_S: Final = 30.0


def clamp_heartbeat_interval_s(requested_s: float) -> float:
    """The heartbeat interval the agent will actually use for ``requested_s``.

    Clamped into ``[MIN_HEARTBEAT_INTERVAL_S, MAX_HEARTBEAT_INTERVAL_S]``; a
    non-positive request is still a protocol violation for the caller to
    reject - this function only bounds what would otherwise be obeyed.
    """
    return min(max(requested_s, MIN_HEARTBEAT_INTERVAL_S), MAX_HEARTBEAT_INTERVAL_S)


_UNACKED_CONTROLLER_PAYLOADS: Final[frozenset[str]] = frozenset(
    {"hello_ack", "ack", "ping"}
)
"""Controller payloads that get no Ack back: receipts, probes and the admission
reply itself (a Ping is answered by a Pong, not an Ack)."""


def _as_controller_message(msg: object) -> agent_pb2.ControllerMessage:
    if not isinstance(msg, agent_pb2.ControllerMessage):
        raise TypeError(
            f"unexpected message type on Session stream: {type(msg).__name__}"
        )
    return msg


def grpc_channel_options(
    *, keepalive_time_ms: int, keepalive_timeout_ms: int
) -> list[tuple[str, int]]:
    """Client channel options: keepalive per ADR-0002 Updates §3."""
    return [
        ("grpc.keepalive_time_ms", keepalive_time_ms),
        ("grpc.keepalive_timeout_ms", keepalive_timeout_ms),
        ("grpc.keepalive_permit_without_calls", 1),
        ("grpc.http2.max_pings_without_data", 0),
    ]


@dataclass(frozen=True, slots=True)
class StreamEnd:
    code: grpc.StatusCode | None
    details: str
    action: StreamEndAction


@dataclass(slots=True)
class SessionStats:
    """Counters for tests and the status surface (G5)."""

    cycles: int = 0
    sessions_accepted: int = 0
    heartbeats_sent: int = 0
    pings_answered: int = 0
    commands_acked: int = 0
    duplicates: int = 0
    stale_messages: int = 0
    reports_sent: int = 0
    reports_acked: int = 0
    replayed: int = 0
    reports_resent: int = 0
    log_chunks_sent: int = 0
    log_chunks_dropped: int = 0
    stale_epoch_retries: int = 0
    stream_ends: list[StreamEnd] = field(default_factory=list)


@dataclass(slots=True)
class _StreamGate:
    """Per-stream ordering gate (D21); see the module docstring."""

    generation: int
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    replayed_through: int = 0
    """Highest report ``sequence`` the Reconcile replay put on this stream."""


class EpochBumper(Protocol):
    """``identity.next_epoch``'s shape (injectable for tests)."""

    def __call__(
        self, identity: NodeIdentity, *, lock_timeout_s: float
    ) -> NodeIdentity: ...


class _Outcome(Enum):
    """What one connection attempt concluded."""

    BACKOFF = "backoff"
    RETRY_STALE = "retry_stale"
    STOP = "stop"


class _Stopped:
    """Sentinel: the awaited operation was pre-empted by :meth:`request_stop`."""


_STOPPED: Final = _Stopped()


class AgentSessionClient:
    """See module docstring. Construct, then ``await run()`` in one task."""

    def __init__(
        self,
        settings: AgentSettings,
        identity: NodeIdentity,
        *,
        resolver: ControllerResolver | None = None,
        clock: Callable[[], float] | None = None,
        rand: Callable[[], float] | None = None,
        hardware: agent_pb2.HardwareSummary | None = None,
        hardware_scores: agent_pb2.HardwareScores | None = None,
        host_facts: tuple[str, str, str] | None = None,
        bump_epoch: EpochBumper = next_epoch,
        on_online: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """``on_online`` is a **test hook**: awaited between the ONLINE transition
        and the Reconcile, so a test can put a suspension point exactly where the
        ordering used to depend on there being none (D21). Production passes
        nothing."""
        self.settings = settings
        self.identity = identity
        self.node_id: str = str(identity.node_id)
        self._resolver: ControllerResolver = resolver or resolver_for(
            settings.agent.controller_address,
            settings.agent.discovery,
            node_id=self.node_id,
        )
        self._clock_override = clock
        self._rand = rand
        self._hardware = hardware
        self.hardware_scores: agent_pb2.HardwareScores | None = hardware_scores
        """What every later ``Hello`` carries (roadmap 0.3.4, D37): the cached run
        valid for this machine, replaced by the scoring task's newest run. Only the
        event loop touches it (the owner task reads it when it builds a Hello)."""
        self._facts = host_facts
        self._bump_epoch = bump_epoch

        self.state: AgentState = AgentState.DISCOVERING
        self.session_id: str = ""
        self.heartbeat_interval_s: float = 0.0
        self.lease_s: float = 0.0
        self.stats = SessionStats()
        self.ring = ReplayRing(settings.agent.replay_ring_size)
        self._seen = RecentIds(_COMMAND_DEDUPE_WINDOW)
        self._sequence = 0  # heartbeats, per process, never reset
        self._report_sequence = 0  # reports, per process, never reset

        self._stop = asyncio.Event()
        self._state_changed = asyncio.Event()
        self._write_lock = asyncio.Lock()
        self._call: Any = None
        self._writes_closed = False
        """True once this call carried its Goodbye: nothing may follow it (D40)."""
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._log_task: asyncio.Task[None] | None = None
        self._log_queue: asyncio.Queue[agent_pb2.LogChunk] = asyncio.Queue(
            settings.agent.log_queue_chunks
        )
        self._stop_waiter: asyncio.Task[bool] | None = None
        self._backoff: Backoff | None = None
        self._running = False
        self._on_online = on_online
        self._generation = 0
        """Bumped when a stream is accepted and again when it ends (D21)."""
        self._gate: _StreamGate | None = None
        # V0.4 G4 execution hooks (module docstring); None = not wired.
        self.command_handler: Callable[[agent_pb2.ControllerMessage], None] | None = (
            None
        )
        self.held_attempts: Callable[[], list[agent_pb2.AttemptStatus]] | None = None
        self.on_report_acked: Callable[[str], None] | None = None
        self.before_goodbye: Callable[[], Awaitable[None]] | None = None
        self.after_reconcile: Callable[[], None] | None = None
        # V0.5 G4 hooks (D97-D101); None/empty = not wired.
        self.peer_endpoint: Callable[[], str] | None = None
        """``host:port`` for ``Hello.peer_endpoint`` (asked for every Hello)."""
        self.on_hello_ack: Callable[[agent_pb2.HelloAck], None] | None = None
        """Called with every ACCEPTED HelloAck before anything else of the
        session runs (the peer server installs ``peer_token_key`` from it)."""
        self.after_reconcile_hooks: list[Callable[[], None]] = []
        """Called after ``after_reconcile`` on every stream (cache snapshot)."""
        self.controller_target: str = ""
        """The ``host:port`` this agent dialled for its current/last stream."""

    # ------------------------------------------------------------ lifecycle

    def request_stop(self) -> None:
        """Ask the owner task to finish its current step and exit ``run()``."""
        if not self._stop.is_set():
            log.info(
                "agent.session.stop_requested",
                node_id=self.node_id,
                session_id=self.session_id or None,
            )
            self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    async def wait_for_state(self, *states: AgentState, timeout_s: float) -> AgentState:
        """Test/operator helper: block until ``state`` is one of ``states``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_s
        while self.state not in states:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(
                    f"agent state is {self.state.value}, wanted one of "
                    f"{[s.value for s in states]} within {timeout_s} s"
                )
            self._state_changed.clear()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._state_changed.wait(), remaining)
        return self.state

    async def run(self) -> None:
        """Run cycles until :meth:`request_stop`. Raises :class:`SupersededError`,
        :class:`AgentContractError`, :class:`~meshforge.common.errors.IdentityError`
        or :class:`~meshforge.common.errors.ConfigError` when the agent cannot
        continue; returns normally on a requested stop."""
        if self._running:
            raise RuntimeError("AgentSessionClient.run() is already running")
        self._running = True
        loop = asyncio.get_running_loop()
        clock = self._clock_override or loop.time
        rc = self.settings.agent.reconnect
        self._backoff = Backoff(
            BackoffPolicy(rc.base_s, rc.cap_s, rc.reset_after_s),
            clock=clock,
            rand=self._rand,
        )
        self._stop_waiter = asyncio.create_task(
            self._stop.wait(), name=f"agent-stop-waiter:{self.node_id}"
        )
        log.info(
            "agent.session.starting",
            node_id=self.node_id,
            controller_address=self.settings.agent.controller_address or None,
            discovery=self.settings.agent.uses_discovery,
        )
        try:
            while not self._stop.is_set():
                self.stats.cycles += 1
                await self._cycle()
        except BaseException as exc:
            if self.state not in (AgentState.STOPPING, AgentState.STOPPED):
                self._transition(AgentEvent.FATAL, reason=type(exc).__name__)
            raise
        else:
            if self.state not in (AgentState.STOPPING, AgentState.STOPPED):
                self._transition(AgentEvent.STOP_REQUESTED, reason="stop")
        finally:
            self._stop_waiter.cancel()
            await asyncio.gather(self._stop_waiter, return_exceptions=True)
            self._stop_waiter = None
            if self.state is AgentState.STOPPING:
                self._transition(AgentEvent.STOPPED, reason="exit")
            self._running = False
            log.info(
                "agent.session.stopped",
                node_id=self.node_id,
                cycles=self.stats.cycles,
                sessions=self.stats.sessions_accepted,
            )

    # ---------------------------------------------------------------- cycle

    async def _cycle(self) -> None:
        if self.state is AgentState.BACKOFF:
            self._transition(AgentEvent.BACKOFF_ELAPSED, reason="retry")

        target = await self._until_stop(self._resolver.resolve())
        if isinstance(target, _Stopped):
            return
        if target is None:
            self._transition(AgentEvent.DISCOVERY_FAILED, reason="controller_not_found")
            await self._sleep_backoff()
            return
        self._transition(AgentEvent.CONTROLLER_FOUND, reason=target)
        self.controller_target = target

        stale_retries = 0
        while True:
            outcome = await self._attempt(target, stale_retries)
            if outcome is _Outcome.RETRY_STALE:
                stale_retries += 1
                self.stats.stale_epoch_retries += 1
                self._transition(AgentEvent.HELLO_RETRY, reason="stale_epoch")
                continue
            break
        if outcome is _Outcome.BACKOFF and not self._stop.is_set():
            await self._sleep_backoff()

    async def _sleep_backoff(self) -> None:
        delay = self._backoff_or_raise().next_delay()
        log.info(
            "agent.session.backoff",
            node_id=self.node_id,
            delay_s=round(delay, 3),
            attempt=self._backoff_or_raise().attempt,
        )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), delay)

    # -------------------------------------------------------------- attempt

    async def _attempt(self, target: str, stale_retries: int) -> _Outcome:
        self.identity = await asyncio.to_thread(
            self._bump_epoch,
            self.identity,
            lock_timeout_s=self.settings.agent.identity_lock_timeout_s,
        )
        cfg = self.settings.agent
        options = grpc_channel_options(
            keepalive_time_ms=cfg.keepalive.time_ms,
            keepalive_timeout_ms=cfg.keepalive.timeout_ms,
        )
        channel = grpc.aio.insecure_channel(target, options=options)
        try:
            call = agent_pb2_grpc.AgentControlStub(channel).Session()
            self._call = call
            self._writes_closed = False
            self.session_id = ""
            hello = build_hello(
                node_id=self.node_id,
                session_epoch=self.identity.session_epoch,
                agent_version=cfg.agent_version,
                hardware=self._hardware,
                facts=self._facts,
                hardware_scores=self.hardware_scores,
                peer_endpoint=self._peer_endpoint(),
            )
            try:
                await self._write(agent_pb2.AgentMessage(hello=hello))
            except (
                grpc.aio.AioRpcError,
                grpc.aio.UsageError,
                asyncio.InvalidStateError,
            ):
                # The call already failed (typically UNAVAILABLE); read the status.
                return await self._finish_stream(await self._status_of(call))
            self._transition(
                AgentEvent.STREAM_OPENED,
                reason="hello_sent",
                session_epoch=self.identity.session_epoch,
            )

            first = await self._until_stop(self._read_first(call))
            if isinstance(first, _Stopped):
                await self._close_call(call)
                return _Outcome.STOP
            if first is None:  # timeout waiting for HelloAck
                log.warning(
                    "agent.session.hello_timeout",
                    node_id=self.node_id,
                    timeout_s=cfg.hello_timeout_s,
                    controller_address=target,
                )
                call.cancel()
                self._transition(AgentEvent.HELLO_REJECTED, reason="hello_timeout")
                return _Outcome.BACKOFF
            if isinstance(first, StreamEnd):
                return await self._finish_stream(first)

            if first.WhichOneof("payload") != "hello_ack":
                log.error(
                    "agent.session.protocol_violation",
                    node_id=self.node_id,
                    payload=first.WhichOneof("payload"),
                    detail="first controller message was not HelloAck",
                )
                call.cancel()
                self._transition(AgentEvent.HELLO_REJECTED, reason="not_hello_ack")
                return _Outcome.BACKOFF

            ack = first.hello_ack
            if not ack.accepted:
                return await self._on_rejected(call, ack, stale_retries)

            return await self._online(call, ack)
        finally:
            self._call = None
            await channel.close()

    async def _read_first(
        self, call: Any
    ) -> agent_pb2.ControllerMessage | StreamEnd | None:
        """The first controller message, ``None`` on timeout, or the end status."""
        try:
            msg = await asyncio.wait_for(
                call.read(), self.settings.agent.hello_timeout_s
            )
        except TimeoutError:
            return None
        except grpc.aio.AioRpcError as exc:
            return self._stream_end(exc.code(), exc.details() or "")
        if msg is grpc.aio.EOF:
            return await self._status_of(call)
        return _as_controller_message(msg)

    async def _on_rejected(
        self, call: Any, ack: agent_pb2.HelloAck, stale_retries: int
    ) -> _Outcome:
        code_name = agent_pb2.HelloAck.RejectCode.Name(ack.reject_code)
        action = hello_reject_action(ack.reject_code, stale_retries)
        log.warning(
            "agent.session.hello_rejected",
            node_id=self.node_id,
            reject_code=code_name,
            reject_reason=ack.reject_reason,
            action=action.value,
            session_epoch=self.identity.session_epoch,
        )
        # The controller closes the stream right after a rejection; drain it so
        # the channel closes cleanly (bounded).
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._status_of(call), _CLOSE_GRACE_S)
        call.cancel()
        if action is HelloRejectAction.FATAL_CONFIG:
            self._transition(AgentEvent.FATAL, reason=code_name)
            raise AgentContractError(
                f"the controller rejected this agent's Hello ({code_name}): "
                f"{ack.reject_reason or 'no reason given'}. Upgrade the agent or the "
                "controller so their protocol versions overlap.",
                details={"reject_code": code_name, "node_id": self.node_id},
            )
        if action is HelloRejectAction.RETRY_NOW:
            return _Outcome.RETRY_STALE
        self._transition(AgentEvent.HELLO_REJECTED, reason=code_name)
        return _Outcome.BACKOFF

    # --------------------------------------------------------------- online

    def _peer_endpoint(self) -> str:
        if self.peer_endpoint is None:
            return ""
        try:
            return self.peer_endpoint()
        except Exception:  # a broken hook must not stop enrolment
            log.exception("agent.session.peer_endpoint_failed", node_id=self.node_id)
            return ""

    async def _online(self, call: Any, ack: agent_pb2.HelloAck) -> _Outcome:
        self.session_id = ack.session_id
        if self.on_hello_ack is not None:
            self.on_hello_ack(ack)
        requested_s = ack.heartbeat_interval_ms / 1000.0
        self.heartbeat_interval_s = clamp_heartbeat_interval_s(requested_s)
        if requested_s > 0 and self.heartbeat_interval_s != requested_s:
            log.warning(
                "agent.session.heartbeat_interval_clamped",
                node_id=self.node_id,
                session_id=self.session_id,
                requested_s=requested_s,
                used_s=self.heartbeat_interval_s,
            )
        self.lease_s = ack.lease_ms / 1000.0
        reconnect = self.stats.sessions_accepted > 0
        self.stats.sessions_accepted += 1
        self._backoff_or_raise().stream_started()
        violation = None
        if requested_s <= 0:
            violation = "HelloAck.heartbeat_interval_ms must be > 0"
        elif not self.session_id:
            violation = "HelloAck.session_id must not be empty on an accepted Hello"
        # The gate exists before anything can observe ONLINE, and is closed (its
        # waiters released) however this method is left.
        gate = self._open_gate()
        try:
            return await self._run_stream(
                call, gate, reconnect=reconnect, violation=violation
            )
        finally:
            self._close_gate(gate)

    async def _run_stream(
        self,
        call: Any,
        gate: _StreamGate,
        *,
        reconnect: bool,
        violation: str | None,
    ) -> _Outcome:
        self._transition(
            AgentEvent.HELLO_ACCEPTED,
            reason="reconnected" if reconnect else "connected",
            heartbeat_interval_s=self.heartbeat_interval_s,
            lease_s=self.lease_s,
        )
        if violation is not None:
            log.error(
                "agent.session.protocol_violation",
                node_id=self.node_id,
                session_id=self.session_id,
                detail=violation,
            )
            call.cancel()
            return await self._finish_stream(
                self._stream_end(grpc.StatusCode.CANCELLED, violation)
            )

        self._heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(call, gate), name=f"agent-heartbeat:{self.node_id}"
        )
        self._heartbeat_task.add_done_callback(self._on_heartbeat_done)
        self._log_task = asyncio.create_task(
            self._log_pump(gate), name=f"agent-logs:{self.node_id}"
        )
        self._log_task.add_done_callback(self._on_heartbeat_done)
        try:
            # ORDERING (D21): ``Reconcile`` must be the first ``AgentMessage`` of a
            # reconnect - otherwise a fresh report consumes the controller's armed
            # cursor reset, the cursor jumps to its sequence and every replayed
            # report below it is dropped as a duplicate: telemetry silently lost.
            # This used to rest on there being no ``await`` between the ONLINE
            # transition above and the reconcile below. It no longer does:
            # ``send_report`` and the heartbeat wait for ``gate.ready``, which is
            # set only after the Reconcile and the replay. Suspension points here
            # are harmless (``on_online`` proves it in the tests). Pinned by
            # tests/component/agent/test_reconnect_reconcile_first.py and
            # tests/unit/agent/test_session_client_ordering.py.
            if self._on_online is not None:
                await self._on_online()
            # V0.4 G4 (D66): a Reconcile on every stream - on a first connect it
            # replays nothing (replay_from_sequence 0) but tells the controller
            # which attempts this process holds (none, after a restart).
            reconciled = await self._reconcile_safely(gate, replay=reconnect)
            if reconciled:
                gate.ready.set()
                if self.after_reconcile is not None:
                    self.after_reconcile()
                for hook in self.after_reconcile_hooks:
                    hook()
            end = await self._read_loop(call)
        finally:
            self._heartbeat_task.cancel()
            self._log_task.cancel()
            await asyncio.gather(
                self._heartbeat_task, self._log_task, return_exceptions=True
            )
            self._heartbeat_task = None
            self._log_task = None
            self._drop_queued_logs()
        if self._stop.is_set():
            # Requested stop: we said Goodbye and half-closed, the controller
            # answered with its final status (OK / agent_closed). Not a reconnect,
            # no state change here - run() moves ONLINE -> STOPPING -> STOPPED.
            self.stats.stream_ends.append(end)
            log.info(
                "agent.session.closed",
                node_id=self.node_id,
                session_id=self.session_id or None,
                code=end.code.name if end.code is not None else None,
                lived_s=round(self._backoff_or_raise().stream_ended(), 3),
            )
            return _Outcome.STOP
        return await self._finish_stream(end)

    async def _reconcile_safely(self, gate: _StreamGate, *, replay: bool) -> bool:
        """Reconcile + replay; False if the stream died under it.

        A write failing here means the call already ended; the read loop that
        follows reports how. The gate then stays unset (nothing may be written to
        a dead stream) until ``_online`` closes it on the way out.
        """
        try:
            gate.replayed_through = await self._reconcile_and_replay(replay=replay)
        except (grpc.aio.AioRpcError, grpc.aio.UsageError, asyncio.InvalidStateError):
            log.info(
                "agent.session.reconcile_interrupted",
                node_id=self.node_id,
                session_id=self.session_id or None,
            )
            return False
        return True

    async def _reconcile_and_replay(self, *, replay: bool = True) -> int:
        """Write ``Reconcile`` and replay the ring; return the last sequence sent.

        ``replay=False`` (a first connect): the Reconcile carries the held
        attempts and ``replay_from_sequence = 0``, and nothing is replayed.
        """
        pending = self.ring.pending() if replay else []
        # Wire contract (agent.proto Reconcile): 0 when nothing is replayed.
        replay_from = (self.ring.first_sequence() or 0) if pending else 0
        held = self.held_attempts() if self.held_attempts is not None else []
        await self._write(
            agent_pb2.AgentMessage(
                reconcile=agent_pb2.Reconcile(
                    replay_from_sequence=replay_from, running_attempts=held
                )
            )
        )
        for entry in pending:
            await self._write(entry.message)  # message_id kept: controller dedupes
            self.stats.replayed += 1
        log.info(
            "agent.session.reconciled",
            node_id=self.node_id,
            session_id=self.session_id,
            replay_from_sequence=replay_from,
            replayed=len(pending),
            first_connect=not replay,
            held_attempts=len(held),
            replay_dropped=self.ring.dropped,
        )
        return pending[-1].sequence if pending else 0

    # ------------------------------------------------------------ the gate

    def _open_gate(self) -> _StreamGate:
        """A new stream generation whose writes wait for its Reconcile (D21)."""
        if self._gate is not None:  # pragma: no cover - _online always closes it
            self._close_gate(self._gate)
        self._generation += 1
        gate = _StreamGate(self._generation)
        self._gate = gate
        return gate

    def _close_gate(self, gate: _StreamGate) -> None:
        """The stream is over: move the generation on and release every waiter
        (each re-checks the generation and skips its direct write)."""
        if self._gate is gate:
            self._gate = None
            self._generation += 1
        gate.ready.set()

    async def _wait_for_stream(self) -> int | None:
        """Wait until the current stream may carry direct writes (D21).

        Returns the generation the caller may write on, or ``None`` when it must
        not write directly (not ONLINE, no stream, or the stream ended while
        waiting - which releases the wait, so this never blocks past a stream).
        """
        gate = self._gate
        if self.state is not AgentState.ONLINE or gate is None:
            return None
        generation = gate.generation
        await gate.ready.wait()
        if (
            self._generation != generation
            or self._gate is not gate
            or self.state is not AgentState.ONLINE
            or self._call is None
        ):
            return None
        return generation

    async def _read_loop(self, call: Any) -> StreamEnd:
        """Consume controller messages until the stream ends; returns how.

        A requested stop does **not** cancel the read: the agent writes its
        ``Goodbye`` (D40), half-closes (``done_writing``) and keeps reading so the
        controller can end the stream ``OK`` and record ``agent_closed``. If no
        status arrives within ``_CLOSE_GRACE_S`` the call is cancelled locally.
        """
        if self._stop_waiter is None:
            raise RuntimeError("AgentSessionClient is not running")
        closing = False
        while True:
            try:
                if closing:
                    msg = await asyncio.wait_for(call.read(), _CLOSE_GRACE_S)
                else:
                    read_task: asyncio.Task[Any] = asyncio.ensure_future(call.read())
                    done, _pending = await asyncio.wait(
                        {read_task, self._stop_waiter},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if read_task in done:
                        msg = read_task.result()
                    else:
                        closing = True
                        log.info(
                            "agent.session.closing",
                            node_id=self.node_id,
                            session_id=self.session_id or None,
                        )
                        await self._before_goodbye()
                        await self._say_goodbye()
                        with contextlib.suppress(Exception):
                            await call.done_writing()
                        msg = await asyncio.wait_for(read_task, _CLOSE_GRACE_S)
            except TimeoutError:
                call.cancel()
                return self._stream_end(
                    grpc.StatusCode.CANCELLED, "no final status within close grace"
                )
            except grpc.aio.AioRpcError as exc:
                # grpc.aio raises on an aborted stream instead of returning EOF.
                return self._stream_end(exc.code(), exc.details() or "")
            if msg is grpc.aio.EOF:
                return await self._status_of(call)
            try:
                await self._handle(_as_controller_message(msg))
            except (
                grpc.aio.AioRpcError,
                grpc.aio.UsageError,
                asyncio.InvalidStateError,
            ):
                # A write raced the end of the stream (or our half-close); the next
                # read tells us how it ended.
                continue

    async def _handle(self, msg: agent_pb2.ControllerMessage) -> None:
        which = msg.WhichOneof("payload")
        if msg.session_id != self.session_id:
            self.stats.stale_messages += 1
            log.warning(
                "agent.session.stale_message",
                node_id=self.node_id,
                session_id=self.session_id,
                message_session_id=msg.session_id,
                payload=which,
                count=self.stats.stale_messages,
            )
            return
        if which == "ping":
            # Before anything else: RTT is measured on this reply.
            await self._write(
                agent_pb2.AgentMessage(pong=agent_pb2.Pong(ping_id=msg.ping.ping_id))
            )
            self.stats.pings_answered += 1
            return
        if which == "hello_ack":
            log.warning(
                "agent.session.protocol_violation",
                node_id=self.node_id,
                session_id=self.session_id,
                detail="second HelloAck on an open stream (ignored)",
            )
            return
        if which == "ack":
            if self.ring.ack(msg.ack.acked_message_id):
                self.stats.reports_acked += 1
                if self.on_report_acked is not None:
                    self.on_report_acked(msg.ack.acked_message_id)
            return
        if which is None:
            log.warning(
                "agent.session.protocol_violation",
                node_id=self.node_id,
                session_id=self.session_id,
                detail="controller message without payload",
            )
            return
        duplicate = bool(msg.message_id) and self._seen.remember(msg.message_id)
        if duplicate:
            self.stats.duplicates += 1
        else:
            log.info(
                "agent.session.command_received",
                node_id=self.node_id,
                session_id=self.session_id,
                payload=which,
                message_id=msg.message_id,
            )
            if which in _COMMAND_PAYLOADS and self.command_handler is not None:
                # Synchronous and before the Ack (D66: Acked = held).
                self.command_handler(msg)
        if which not in _UNACKED_CONTROLLER_PAYLOADS and msg.message_id:
            await self._write(
                agent_pb2.AgentMessage(
                    ack=agent_pb2.Ack(acked_message_id=msg.message_id)
                )
            )
            self.stats.commands_acked += 1

    async def _heartbeat_loop(self, call: Any, gate: _StreamGate) -> None:
        interval = self.heartbeat_interval_s
        # Not even a heartbeat may precede the Reconcile (D21).
        await gate.ready.wait()
        if self._gate is not gate:
            return
        try:
            while True:
                await asyncio.sleep(interval)
                self._sequence += 1
                hb = agent_pb2.Heartbeat(sequence=self._sequence)
                hb.agent_time.FromDatetime(datetime.now(UTC))
                await self._write(agent_pb2.AgentMessage(heartbeat=hb))
                self.stats.heartbeats_sent += 1
        except (grpc.aio.AioRpcError, grpc.aio.UsageError, asyncio.InvalidStateError):
            return  # the stream ended under us; the owner task sees the status

    def _on_heartbeat_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "agent.session.task_failed",
                node_id=self.node_id,
                session_id=self.session_id,
                task=task.get_name(),
                cause=type(exc).__name__,
                error=str(exc),
            )

    # ----------------------------------------------------------------- logs

    def send_log(self, chunk: agent_pb2.LogChunk) -> bool:
        """Queue a live log chunk (never blocks). False = dropped: no stream, or
        the log queue is full (see module docstring)."""
        if self.state is not AgentState.ONLINE or self._gate is None:
            self.stats.log_chunks_dropped += 1
            return False
        try:
            self._log_queue.put_nowait(chunk)
        except asyncio.QueueFull:
            self.stats.log_chunks_dropped += 1
            return False
        return True

    async def _log_pump(self, gate: _StreamGate) -> None:
        await gate.ready.wait()  # nothing precedes the Reconcile (D21)
        if self._gate is not gate:
            return
        try:
            while True:
                chunk = await self._log_queue.get()
                await self._write(agent_pb2.AgentMessage(log_chunk=chunk))
                self.stats.log_chunks_sent += 1
        except (grpc.aio.AioRpcError, grpc.aio.UsageError, asyncio.InvalidStateError):
            return

    def _drop_queued_logs(self) -> None:
        while not self._log_queue.empty():
            self._log_queue.get_nowait()
            self.stats.log_chunks_dropped += 1

    # -------------------------------------------------------------- reports

    async def send_report(self, payload_name: str, payload: Any) -> str:
        """Buffer a report in the ring and, when ONLINE, write it now.

        "Now" means once the stream's Reconcile is out (D21): the call may wait
        for that, and returns without a direct write if the stream ends first.
        ``payload_name`` is the ``AgentMessage`` oneof field (``resource_update``...).
        Returns the ``message_id``. Callers arrive with G4 (ResourceUpdate) and
        V0.4 (AttemptStatus/LogChunk); until then tests use this.
        """
        if payload_name not in REPORT_PAYLOADS:
            raise ValueError(f"{payload_name!r} is not a report payload")
        self._report_sequence += 1
        if payload_name == "resource_update" and not payload.sequence:
            payload.sequence = self._report_sequence
        msg = agent_pb2.AgentMessage(message_id=new_command_id())
        getattr(msg, payload_name).CopyFrom(payload)
        if self.ring.append(self._report_sequence, msg):
            log.warning(
                "agent.session.replay_dropped",
                node_id=self.node_id,
                session_id=self.session_id or None,
                dropped_total=self.ring.dropped,
                capacity=self.ring.capacity,
            )
        sequence = self._report_sequence
        gate = self._gate
        if await self._wait_for_stream() is None:
            # Not ONLINE, or the stream ended while we waited: the ring has it and
            # the next stream's replay carries it (D21).
            return msg.message_id
        if gate is not None and sequence <= gate.replayed_through:
            return msg.message_id  # already on the wire, inside the replay
        with contextlib.suppress(
            grpc.aio.AioRpcError, grpc.aio.UsageError, asyncio.InvalidStateError
        ):
            await self._write(msg)
            self.stats.reports_sent += 1
        return msg.message_id

    async def resend_report(self, message_id: str) -> bool:
        """Write the buffered report ``message_id`` again, same ``message_id`` (the
        controller's handlers are idempotent). False if the ring no longer holds
        it (acked, or evicted - the caller then sends a new report); True if it
        is still buffered, written now or - not ONLINE - left for the replay."""
        entry = self.ring.get(message_id)
        if entry is None:
            return False
        if await self._wait_for_stream() is None:
            return True
        with contextlib.suppress(
            grpc.aio.AioRpcError, grpc.aio.UsageError, asyncio.InvalidStateError
        ):
            await self._write(entry.message)
            self.stats.reports_resent += 1
        return True

    # ---------------------------------------------------------------- ends

    async def _finish_stream(self, end: StreamEnd) -> _Outcome:
        lived = self._backoff_or_raise().stream_ended()
        self.stats.stream_ends.append(end)
        log.info(
            "agent.session.stream_ended",
            node_id=self.node_id,
            session_id=self.session_id or None,
            code=end.code.name if end.code is not None else None,
            details=end.details or None,
            action=end.action.value,
            lived_s=round(lived, 3),
        )
        if end.action is StreamEndAction.STOP_SUPERSEDED:
            self._transition(AgentEvent.FATAL, reason="superseded")
            raise SupersededError(self.node_id, self.session_id)
        if end.action is StreamEndAction.FATAL_CONFIG:
            self._transition(AgentEvent.FATAL, reason=end.code.name if end.code else "")
            raise AgentContractError(
                "the controller refused this agent's stream "
                f"({end.code.name if end.code else 'no status'}): {end.details}",
                details={"code": end.code.name if end.code else None},
            )
        self._transition(
            AgentEvent.STREAM_ENDED, reason=end.code.name if end.code else ""
        )
        return _Outcome.BACKOFF

    def _stream_end(self, code: grpc.StatusCode | None, details: str) -> StreamEnd:
        return StreamEnd(code, details, stream_end_action(code, details))

    async def _status_of(self, call: Any) -> StreamEnd:
        try:
            code = await call.code()
            details = await call.details()
        except (grpc.aio.AioRpcError, asyncio.CancelledError, grpc.aio.UsageError):
            return self._stream_end(None, "")
        return self._stream_end(code, details or "")

    async def _before_goodbye(self) -> None:
        """Let running attempts report ``agent_shutdown`` first (D52), bounded."""
        if self.before_goodbye is None:
            return
        try:
            await asyncio.wait_for(self.before_goodbye(), BEFORE_GOODBYE_BOUND_S)
        except TimeoutError:
            log.warning(
                "agent.session.before_goodbye_timeout",
                node_id=self.node_id,
                session_id=self.session_id or None,
                bound_s=BEFORE_GOODBYE_BOUND_S,
            )
        except Exception:  # the Goodbye must still go out
            log.exception("agent.session.before_goodbye_failed", node_id=self.node_id)

    async def _say_goodbye(self) -> None:
        """Write ``Goodbye{SHUTDOWN}`` as the stream's last message (D40).

        A stream that already died cannot carry it; that is logged, not raised:
        the controller then records ``stream_lost`` on its own. The write is
        bounded by ``_CLOSE_GRACE_S`` (D61-2): on a dead link whose transport has
        not noticed yet, a write (or the write lock behind a stuck heartbeat) can
        block until keepalive gives up - a SIGTERM must not wait that long.
        """
        goodbye = agent_pb2.Goodbye(
            reason=agent_pb2.Goodbye.REASON_SHUTDOWN, detail=GOODBYE_DETAIL
        )
        try:
            await asyncio.wait_for(
                self._write(agent_pb2.AgentMessage(goodbye=goodbye), last=True),
                _CLOSE_GRACE_S,
            )
        except (
            grpc.aio.AioRpcError,
            grpc.aio.UsageError,
            asyncio.InvalidStateError,
            TimeoutError,
        ) as exc:
            log.info(
                "agent.session.goodbye_not_sent",
                node_id=self.node_id,
                session_id=self.session_id or None,
                cause="timeout" if isinstance(exc, TimeoutError) else "stream_ended",
            )
            return
        log.info(
            "agent.session.goodbye_sent",
            node_id=self.node_id,
            session_id=self.session_id or None,
        )

    async def _close_call(self, call: Any) -> None:
        """Stop before the HelloAck: no session exists yet, so there is nothing to
        say Goodbye to. Half-close, wait a little for the final status, then
        cancel whatever is left."""
        with contextlib.suppress(Exception):
            await call.done_writing()
        with contextlib.suppress(TimeoutError):
            end = await asyncio.wait_for(self._status_of(call), _CLOSE_GRACE_S)
            self.stats.stream_ends.append(end)
            log.info(
                "agent.session.closed",
                node_id=self.node_id,
                session_id=self.session_id or None,
                code=end.code.name if end.code is not None else None,
            )
        if not call.done():
            call.cancel()

    # ------------------------------------------------------------- plumbing

    async def _write(self, msg: agent_pb2.AgentMessage, *, last: bool = False) -> None:
        """Write one message on the open call (one in-flight write at a time).

        ``last=True`` (the Goodbye) closes the call for writes once it went out;
        any later write raises :class:`asyncio.InvalidStateError`.
        """
        if not msg.message_id:
            msg.message_id = new_command_id()
        if self.session_id:
            msg.session_id = self.session_id
        msg.sent_at.FromDatetime(datetime.now(UTC))
        call = self._call
        if call is None:
            raise asyncio.InvalidStateError("no open call")
        async with self._write_lock:
            if self._writes_closed:
                raise asyncio.InvalidStateError("the stream already carried Goodbye")
            await call.write(msg)
            if last:
                self._writes_closed = True

    def _backoff_or_raise(self) -> Backoff:
        if self._backoff is None:
            raise RuntimeError("AgentSessionClient is not running")
        return self._backoff

    async def _until_stop[T](self, aw: Awaitable[T]) -> T | _Stopped:
        """Await ``aw`` unless a stop is requested first (then cancel it)."""
        if self._stop_waiter is None:
            raise RuntimeError("AgentSessionClient is not running")
        task: asyncio.Task[T] = asyncio.ensure_future(aw)
        done, _pending = await asyncio.wait(
            {task, self._stop_waiter}, return_when=asyncio.FIRST_COMPLETED
        )
        if task in done:
            return task.result()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return _STOPPED

    def _transition(self, event: AgentEvent, *, reason: str, **extra: Any) -> None:
        old = self.state
        new = next_state(old, event)
        self.state = new
        self._state_changed.set()
        fields: dict[str, Any] = {
            "node_id": self.node_id,
            "session_id": self.session_id or None,
            "from": old.value,
            "to": new.value,
            "trigger": event.value,
            "reason": reason,
            **extra,
        }
        log.info("agent.session.state_changed", **fields)
