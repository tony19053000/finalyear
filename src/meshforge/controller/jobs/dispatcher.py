"""The dispatcher: one task that turns queued jobs into running attempts (V0.4 G4).

**One owner (checklist item 5).** :meth:`Dispatcher.run` is the only task that
writes attempts after submission. Everything that changes an attempt reaches it as
a work item on one bounded FIFO - agent reports (``AttemptStatus``), Acks of
``StartAttempt``, ``Reconcile`` lists, a node going OFFLINE - put there by the
session layer's hooks, which never block (:mod:`..session.hooks`). Between items
it runs the periodic steps: dispatch (D65), the timeout backstop (D69) and the
orphan sweep. Items of one session are therefore applied in the order the agent
sent them, and a Reconcile is always applied before the reports replayed after it.

**Dispatch (D65).** On a submit (``wake``) or every ``dispatch_interval_s``: in one
transaction, queued jobs ``FOR UPDATE SKIP LOCKED``; a job whose placed node is
ONLINE with a live session, still advertises ``exec`` (and ``exec-gpu`` if the job
requires a GPU) and runs fewer than ``max_attempts_per_node`` attempts gets an
attempt (next fencing token), ``QUEUED -> PREPARING`` and a start snapshot, all
committed - **then** ``StartAttempt`` is sent. A SUSPECT/DRAINING node makes the
job wait; a node that is gone (no live or lingering session, after
``reconnect_grace_s`` of controller uptime) fails it ``node_lost`` (D65), a node
that stopped accepting jobs fails it ``no_feasible_plan``.

**Reports (D52).** Validated (:mod:`.reports`), then applied by
:meth:`~.execution_store.ExecutionStore.apply_status` and only **then** acked
(:meth:`~..session.manager.SessionManager.ack_message`): an Ack means "stored",
and a report lost to a crash before that is replayed by the agent. A stale token
or a report for an attempt the controller already ended is dropped, counted
``fenced`` and answered with ``CancelAttempt`` (the agent must stop it).

**Reconcile (D66).** For the node's live attempts: held by the agent -> continue
(re-sending a pending cancel); not held and ``StartAttempt`` never acked ->
re-send the same attempt (the agent dedupes on ``(attempt_id, fencing_token)``);
not held but acked -> ``LOST``, job ``FAILED(node_lost, agent_lost_attempt)`` -
re-running it could duplicate an execution. Attempts the agent holds that the
controller does not consider live get ``CancelAttempt``.

**OFFLINE (D66).** A node the *agent* took OFFLINE (lease expired, Goodbye) loses
its live attempts (``LOST``, job ``FAILED(node_lost)``; no retry until V0.6) and
their fetch tokens are revoked. SUSPECT never does. A controller shutdown never
does. After a controller restart, attempts of nodes that do not reconnect within
``reconnect_grace_s`` are lost by the orphan sweep.

**Backstop (D69).** An attempt still RUNNING ``backstop_grace_s`` past its job's
time limit is failed ``deadline_exceeded`` (``controller_backstop``) and cancelled.

A database failure while applying an item (``JobStoreError``, retryable) is retried
with backoff, keeping the item; anything else is a bug: logged with its traceback,
the item dropped (a report then stays un-acked and is replayed after a reconnect).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from meshforge.common.capabilities import EXEC_CAPABILITY, EXEC_GPU_CAPABILITY
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import Manifest
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import ATTEMPT_TERMINAL_STATES
from meshforge.controller.blobs import MAX_FETCH_TOKEN_TTL_S, FetchTokens
from meshforge.controller.jobs.execution_store import (
    Dispatch,
    DispatchDecision,
    DispatchFailure,
    ExecutionStore,
    LiveAttempt,
    NodeView,
    StatusKind,
    StatusOutcome,
    StatusReport,
)
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.jobs.spec import choose_gpus, execution_spec, start_snapshot
from meshforge.controller.jobs.store import AttemptRecord, JobRecord, JobStoreError
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.liveness import LivenessEvent, NodeState
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.live import LiveSession, PendingCommand
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.settings import JobApiSettings

__all__ = ["Dispatcher", "DispatcherStats"]

log = get_logger(__name__)

_RETRY_BASE_S: Final = 0.2
_RETRY_CAP_S: Final = 5.0


# ---------------------------------------------------------------- work items


@dataclass(frozen=True, slots=True)
class _Status:
    live: LiveSession
    message_id: str
    report: StatusReport


@dataclass(frozen=True, slots=True)
class _Acked:
    node_id: str
    attempt_id: str
    fencing_token: int


@dataclass(frozen=True, slots=True)
class _Reconcile:
    live: LiveSession
    held: dict[str, int]
    """attempt_id -> fencing_token the agent holds."""


@dataclass(frozen=True, slots=True)
class _Offline:
    node_id: str
    session_id: str
    event: LivenessEvent


type _Item = _Status | _Acked | _Reconcile | _Offline


@dataclass(slots=True)
class DispatcherStats:
    dispatched: int = 0
    start_sent: int = 0
    start_resent: int = 0
    statuses_applied: int = 0
    statuses_duplicate: int = 0
    statuses_fenced: int = 0
    statuses_rejected: int = 0
    statuses_dropped: int = 0
    attempts_lost: int = 0
    backstops: int = 0
    cancels_sent: int = 0
    db_retries: int = 0


class Dispatcher:
    """See module docstring. ``run()`` is one task owned by ``ControllerRuntime``."""

    def __init__(
        self,
        store: ExecutionStore,
        manager: SessionManager,
        fetch_tokens: FetchTokens,
        settings: JobApiSettings,
        *,
        clock: Callable[[], float],
        blob_port: Callable[[], int],
        blob_url: str | None = None,
        resources: ResourceTracker | None = None,
        wall_clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._store = store
        self._manager = manager
        self._tokens = fetch_tokens
        self._settings = settings
        self._clock = clock
        self._blob_port = blob_port
        self._blob_url = blob_url or ""
        self._resources = resources
        self._wall = wall_clock or (lambda: dt.datetime.now(dt.UTC))
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(settings.status_queue_size)
        self._wake = asyncio.Event()
        self._started_at = clock()
        self.stats = DispatcherStats()
        manager.dispatch.register("attempt_status", self._on_status_message)
        manager.defer_ack("attempt_status")
        hooks = manager.hooks
        hooks.on_command_acked = self._on_command_acked
        hooks.on_reconcile = self._on_reconcile
        hooks.on_node_offline = self._on_node_offline

    # ------------------------------------------------------------ public

    def wake(self) -> None:
        """A job was submitted (or cancelled): run a dispatch pass soon."""
        self._wake.set()

    async def run(self) -> None:
        """Forever: work items, then the periodic steps (see module docstring)."""
        interval = self._settings.dispatch_interval_s
        next_tick = self._clock()
        while True:
            timeout = max(0.0, next_tick - self._clock())
            if self._queue.empty() and not self._wake.is_set():
                await self._wait(timeout)
            while not self._queue.empty():
                await self._retrying(self._handle, self._queue.get_nowait())
            if self._wake.is_set() or self._clock() >= next_tick:
                self._wake.clear()
                await self._retrying(self._periodic, None)
                next_tick = self._clock() + interval

    async def cancel(self, job_id: str) -> JobRecord | None:
        """D69: cancel ``job_id`` (idempotent). ``None`` = no such job."""
        outcome = await self._store.request_cancel(job_id)
        if outcome is None:
            return None
        if outcome.attempt is not None:
            self._send_cancel(outcome.attempt, "cancelled by request")
        return outcome.job

    # ------------------------------------------------------------ hooks

    async def _on_status_message(
        self, live: LiveSession, msg: agent_pb2.AgentMessage
    ) -> None:
        """Reader task: validate and queue; the Ack follows once stored."""
        live.attempt_status_received += 1
        report = parse_status(msg.attempt_status)
        if isinstance(report, str):
            live.attempt_status_rejected += 1
            self.stats.statuses_rejected += 1
            log.warning(
                "controller.jobs.status_rejected",
                node_id=live.node_id,
                session_id=live.session_id,
                reason=report,
            )
            self._manager.ack_message(live, msg.message_id)  # never replay it
            return
        try:
            self._queue.put_nowait(_Status(live, msg.message_id, report))
        except asyncio.QueueFull:
            live.attempt_status_dropped += 1
            self.stats.statuses_dropped += 1
            log.warning(
                "controller.jobs.status_queue_full",
                node_id=live.node_id,
                session_id=live.session_id,
                attempt_id=report.attempt_id,
                capacity=self._queue.maxsize,
            )

    def _on_command_acked(
        self, live: LiveSession, _message_id: str, pending: PendingCommand
    ) -> None:
        if pending.payload == "start_attempt":
            self._put(_Acked(live.node_id, pending.attempt_id, pending.fencing_token))

    def _on_reconcile(self, live: LiveSession, reconcile: agent_pb2.Reconcile) -> None:
        held = {
            a.attempt_id: int(a.fencing_token)
            for a in reconcile.running_attempts[:1024]
        }
        self._put(_Reconcile(live, held))

    def _on_node_offline(self, live: LiveSession, event: LivenessEvent) -> None:
        self._put(_Offline(live.node_id, live.session_id, event))

    def _put(self, item: _Item) -> None:
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            # Not a report: nothing replays it. The periodic sweeps (orphans,
            # backstop) and the next Reconcile are the backstops; say so loudly.
            log.error(  # noqa: TRY400 - no exception to attach
                "controller.jobs.work_queue_full",
                item=type(item).__name__,
                capacity=self._queue.maxsize,
            )

    # ------------------------------------------------------------ the loop

    async def _wait(self, wait_s: float) -> None:
        getter = asyncio.ensure_future(self._queue.get())
        waker = asyncio.ensure_future(self._wake.wait())
        try:
            done, _ = await asyncio.wait(
                {getter, waker}, timeout=wait_s, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            waker.cancel()
            if not getter.done():
                getter.cancel()
            await asyncio.gather(waker, return_exceptions=True)
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.gather(getter, return_exceptions=True)
        if getter in done and not getter.cancelled():
            item = getter.result()
            await self._retrying(self._handle, item)

    async def _retrying(self, step: Callable[..., object], arg: object) -> None:
        delay = _RETRY_BASE_S
        while True:
            try:
                await step(arg)  # type: ignore[misc]
            except JobStoreError as exc:
                self.stats.db_retries += 1
                log.warning(
                    "controller.jobs.db_retry",
                    step=getattr(step, "__name__", "step"),
                    cause=exc.details.get("cause"),
                    delay_s=delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_CAP_S)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "controller.jobs.step_failed",
                    step=getattr(step, "__name__", "step"),
                    item=type(arg).__name__,
                )
                return
            else:
                return

    async def _handle(self, item: _Item) -> None:
        if isinstance(item, _Status):
            await self._apply_status(item)
        elif isinstance(item, _Acked):
            await self._store.mark_start_acked(item.attempt_id, item.fencing_token)
        elif isinstance(item, _Reconcile):
            await self._reconcile(item)
        else:
            await self._offline(item)

    async def _periodic(self, _: object) -> None:
        await self._dispatch()
        await self._backstop()
        await self._sweep_orphans()

    # ------------------------------------------------------------ reports

    async def _apply_status(self, item: _Status) -> None:
        report, live = item.report, item.live
        outcome = await self._store.apply_status(report)
        if outcome.kind is StatusKind.APPLIED:
            self.stats.statuses_applied += 1
        elif outcome.kind is StatusKind.DUPLICATE:
            self.stats.statuses_duplicate += 1
        else:
            self.stats.statuses_fenced += 1
            live.attempt_status_fenced += 1
            log.warning(
                "controller.jobs.status_fenced",
                node_id=live.node_id,
                session_id=live.session_id,
                attempt_id=report.attempt_id,
                fencing_token=report.fencing_token,
                reported=report.state.value,
                outcome=outcome.kind.value,
                stored=None if outcome.attempt is None else outcome.attempt.state.value,
            )
            if report.state not in ATTEMPT_TERMINAL_STATES:
                self._cancel_on(
                    live.node_id, report.attempt_id, report.fencing_token, "fenced"
                )
        self._manager.ack_message(live, item.message_id)
        self._after(outcome)

    def _after(self, outcome: StatusOutcome) -> None:
        """Token expiry at attempt end; a pending cancel re-sent to a live attempt."""
        attempt, job = outcome.attempt, outcome.job
        if attempt is None:
            return
        if outcome.ended:
            self._tokens.expire_after(
                attempt.id, self._settings.fetch_token_end_grace_s
            )
        elif (
            job is not None
            and job.cancel_requested_at is not None
            and attempt.state not in ATTEMPT_TERMINAL_STATES
            and outcome.kind is StatusKind.APPLIED
        ):
            self._send_cancel(attempt, "cancelled by request")

    # ------------------------------------------------------------ dispatch

    async def _dispatch(self) -> None:
        results = await self._store.dispatch_queued(
            self._decide,
            limit=self._settings.dispatch_batch,
            max_per_node=self._settings.max_attempts_per_node,
        )
        for result in results:
            if result.attempt is not None:
                self.stats.dispatched += 1
                self._send_start(result.job, result.attempt, resend=False)

    def _decide(self, job: JobRecord, node: NodeView | None) -> DispatchDecision:
        node_id = str(job.placement.get("node_id", ""))
        live = self._manager.get(node_id)
        if live is None:
            if self._manager.get_lingering(node_id) is not None:
                return None  # SUSPECT: a blip must not fail the job (D19)
            if self._clock() - self._started_at < self._settings.reconnect_grace_s:
                return None  # just restarted: agents are still reconnecting
            return DispatchFailure(FailureType.NODE_LOST, "placed_node_offline")
        if node is None:
            return DispatchFailure(FailureType.NODE_LOST, "placed_node_deleted")
        if live.state is not NodeState.ONLINE:
            return None  # SUSPECT / DRAINING: wait
        caps = node.hardware.get("capabilities")
        tags = (
            {c for c in caps if isinstance(c, str)} if isinstance(caps, list) else set()
        )
        if EXEC_CAPABILITY not in tags:
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN, "node_no_longer_accepts_jobs"
            )
        manifest = Manifest.model_validate(dict(job.manifest))
        if manifest.resources.gpu == "required" and EXEC_GPU_CAPABILITY not in tags:
            return DispatchFailure(
                FailureType.NO_FEASIBLE_PLAN, "node_can_no_longer_provide_a_gpu"
            )
        if node.active_attempts >= self._settings.max_attempts_per_node:
            return None
        gpus = choose_gpus(manifest, node.hardware)
        snapshot = start_snapshot(
            hardware=node.hardware,
            hardware_scores=node.hardware_scores,
            node_state=live.state.value,
            rtt_ms=live.rtt_ewma_ms,
            sample=None if self._resources is None else self._resources.latest(node_id),
            monotonic_now=self._clock(),
            wall_now=self._wall(),
            gpu_indices=gpus,
        )
        return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot=snapshot)

    def _send_start(
        self, job: JobRecord, attempt: AttemptRecord, *, resend: bool
    ) -> None:
        # One token per send: a re-send after a controller restart must work even
        # though the old token died with the old process (tokens live in memory).
        self._tokens.revoke(attempt.id)
        ttl = min(
            float(job.manifest.get("limits", {}).get("timeout", 0))
            + self._settings.fetch_token_grace_s,
            float(MAX_FETCH_TOKEN_TTL_S),
        )
        token = self._tokens.mint(
            attempt.id,
            [Digest.parse(job.bundle_digest)],
            ttl_s=ttl,
            allow_upload=True,  # G5 uploads outputs with it (D54)
        )
        spec = execution_spec(
            job,
            attempt,
            fetch_token=token.get_secret_value(),
            blob_url=self._blob_url,
            blob_port=self._blob_port(),
        )
        message_id = self._manager.send_command(
            attempt.node_id,
            "start_attempt",
            agent_pb2.StartAttempt(
                job_id=job.id,
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                spec=spec,
            ),
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        if message_id is None:
            # Committed, not sent: the next Reconcile of this node re-sends it,
            # or its OFFLINE / the orphan sweep ends it (D65 commit-before-send).
            log.warning(
                "controller.jobs.start_not_sent",
                job_id=job.id,
                attempt_id=attempt.id,
                node_id=attempt.node_id,
            )
            return
        if resend:
            self.stats.start_resent += 1
        else:
            self.stats.start_sent += 1

    # ------------------------------------------------------------ cancel

    def _send_cancel(self, attempt: AttemptRecord, reason: str) -> None:
        self._cancel_on(attempt.node_id, attempt.id, attempt.fencing_token, reason)

    def _cancel_on(
        self, node_id: str, attempt_id: str, fencing_token: int, reason: str
    ) -> None:
        sent = self._manager.send_command(
            node_id,
            "cancel_attempt",
            agent_pb2.CancelAttempt(
                attempt_id=attempt_id, fencing_token=fencing_token, reason=reason
            ),
            attempt_id=attempt_id,
            fencing_token=fencing_token,
        )
        if sent is not None:
            self.stats.cancels_sent += 1

    # ------------------------------------------------------------ D66

    async def _reconcile(self, item: _Reconcile) -> None:
        live = item.live
        if live.ended:
            return  # a newer session's Reconcile will do it
        node_id = live.node_id
        known: set[str] = set()
        for entry in await self._store.live_attempts(node_id):
            attempt, job = entry.attempt, entry.job
            known.add(attempt.id)
            if item.held.get(attempt.id) == attempt.fencing_token:
                if job.cancel_requested_at is not None:
                    self._send_cancel(attempt, "cancelled by request")
                continue
            if attempt.start_acked_at is None:
                log.info(
                    "controller.jobs.start_resent",
                    node_id=node_id,
                    session_id=live.session_id,
                    job_id=job.id,
                    attempt_id=attempt.id,
                )
                self._send_start(job, attempt, resend=True)
                continue
            await self._lose(entry, "agent_lost_attempt")
        for attempt_id, token in item.held.items():
            if attempt_id not in known:
                self._cancel_on(node_id, attempt_id, token, "not_live_on_controller")

    async def _offline(self, item: _Offline) -> None:
        for entry in await self._store.live_attempts(item.node_id):
            await self._lose(entry, "node_offline")

    async def _sweep_orphans(self) -> None:
        if self._clock() - self._started_at < self._settings.reconnect_grace_s:
            return
        for entry in await self._store.live_attempts():
            node_id = entry.attempt.node_id
            if (
                self._manager.get(node_id) is None
                and self._manager.get_lingering(node_id) is None
            ):
                await self._lose(entry, "node_not_connected")

    async def _lose(self, entry: LiveAttempt, detail: str) -> None:
        attempt = entry.attempt
        outcome = await self._store.end_attempt(
            attempt.id,
            attempt.fencing_token,
            event=AttemptEvent.LOSE,
            failure_type=FailureType.NODE_LOST,
            detail=detail,
        )
        self._tokens.revoke(attempt.id)
        if outcome.kind is StatusKind.APPLIED:
            self.stats.attempts_lost += 1
            log.warning(
                "controller.jobs.attempt_lost",
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                node_id=attempt.node_id,
                detail=detail,
            )

    # ------------------------------------------------------------ D69

    async def _backstop(self) -> None:
        for entry in await self._store.overdue_running(self._settings.backstop_grace_s):
            attempt = entry.attempt
            outcome = await self._store.end_attempt(
                attempt.id,
                attempt.fencing_token,
                event=AttemptEvent.FAIL,
                failure_type=FailureType.DEADLINE_EXCEEDED,
                detail="controller_backstop",
            )
            if outcome.kind is not StatusKind.APPLIED:
                continue
            self.stats.backstops += 1
            log.warning(
                "controller.jobs.backstop",
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                node_id=attempt.node_id,
            )
            self._tokens.expire_after(
                attempt.id, self._settings.fetch_token_end_grace_s
            )
            self._send_cancel(attempt, "controller_backstop")
