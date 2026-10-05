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
and a report lost to a crash before that is replayed by the agent (and a final
one re-sent until acked, D75). A stale token, or a report for an attempt the
controller already ended or does not know, is **acked**, counted ``fenced`` and
answered with ``CancelAttempt{release}``: the agent stops it if it still runs and
frees it (D75). A report whose handler fails with anything but a database error
is a *poison* report: after ``POISON_LIMIT`` failures of the same
``(attempt_id, state)`` it is acked and logged ``error`` with the exception type
only, so it cannot be replayed forever (D75). **Acked is not applied:** the
store was never changed, so a poisoned ``DONE`` never makes the job
``SUCCEEDED``. The agent frees the attempt once acked, and the attempt ends by
another path: the next Reconcile finds it acked-but-not-held -> ``LOST``
(``agent_lost_attempt``, job ``FAILED(node_lost)``); an agent OFFLINE -> ``LOST``;
or, if it is still ``RUNNING`` in the store, the backstop ->
``FAILED(deadline_exceeded, controller_backstop)``. The backstop only covers
``RUNNING``; an attempt stored as ``UPLOADING`` ends by the upload deadline
(D94) if no Reconcile or OFFLINE ends it first.

**Reconcile (D66).** For the node's live attempts: held by the agent -> continue
(re-sending a pending cancel); not held and ``StartAttempt`` never acked ->
re-send the same attempt (the agent dedupes on ``(attempt_id, fencing_token)``);
not held but acked -> ``LOST``, job ``FAILED(node_lost, agent_lost_attempt)`` -
re-running it could duplicate an execution. Attempts the agent holds that the
controller does not consider live (ended, or unknown) get
``CancelAttempt{release}`` (D75). A held attempt still ``ASSIGNED``/``STAGING``
for which this process has no live fetch token (a controller restart forgets
them all; they are never persisted) gets ``RefreshFetchToken`` with a new
download-only token of the same scope and the current blob location (D95).

**OFFLINE (D66).** A node the *agent* took OFFLINE (lease expired, Goodbye) loses
its live attempts (``LOST``, job ``FAILED(node_lost)``; no retry until V0.6) and
their fetch tokens are revoked. SUSPECT never does. A controller shutdown never
does. After a controller restart, attempts of nodes that do not reconnect within
``reconnect_grace_s`` are lost by the orphan sweep.

**Backstop (D69).** An attempt still RUNNING ``backstop_grace_s`` past its job's
time limit is failed ``deadline_exceeded`` (``controller_backstop``) and cancelled.

**Upload deadline (D94).** An attempt still UPLOADING
``upload_deadline_total_s`` after it entered UPLOADING is failed
``output_error`` (``upload_deadline_exceeded``), its upload grant and fetch
token are **revoked** (a late ``PUT`` is refused, a late ``DONE`` is fenced) and
it is cancelled. The start of UPLOADING is the job's persisted ``COLLECTING``
transition, so a controller restart keeps the deadline; it is compared with the
controller's wall clock (both are timestamps, not intervals measured here).

**Log GC (D94).** Every ``log_gc_interval_s`` the log files of attempts whose
job is terminal and unchanged for ``log_retention_s`` are deleted
(:meth:`~.logs.LogHub.collect_garbage`); only the database decides "terminal".

A database failure while applying an item (``JobStoreError``, retryable) is retried
with backoff, keeping the item; anything else is a bug: logged with its traceback,
the item dropped (a report then stays un-acked and is replayed after a reconnect).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime as dt
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from pydantic import SecretStr

from meshforge.common.capabilities import EXEC_CAPABILITY, EXEC_GPU_CAPABILITY
from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import Manifest
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    JOB_TERMINAL_STATES,
    AttemptState,
    JobState,
)
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
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.jobs.peers import PeerSources
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.jobs.spec import choose_gpus, execution_spec, start_snapshot
from meshforge.controller.jobs.store import AttemptRecord, JobRecord, JobStoreError
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.liveness import LivenessEvent, NodeState
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.live import LiveSession, PendingCommand
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.settings import JobApiSettings
from meshforge.controller.upload_grants import UploadGrants

__all__ = ["POISON_LIMIT", "Dispatcher", "DispatcherStats"]

log = get_logger(__name__)

_RETRY_BASE_S: Final = 0.2
_RETRY_CAP_S: Final = 5.0
POISON_LIMIT: Final = 3
"""Failures of one ``(attempt_id, state)`` report before it is acked anyway (D75)."""
_POISON_TRACKED: Final = 1024
_MAY_FETCH: Final = frozenset({AttemptState.ASSIGNED, AttemptState.STAGING})
"""Attempt states (as stored) in which the agent may still fetch from the
controller; the store is never ahead of the agent, so this is a superset (D95)."""


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
    fetch_tokens_refreshed: int = 0
    statuses_applied: int = 0
    statuses_duplicate: int = 0
    statuses_fenced: int = 0
    statuses_rejected: int = 0
    statuses_dropped: int = 0
    attempts_lost: int = 0
    backstops: int = 0
    cancels_sent: int = 0
    releases_sent: int = 0
    statuses_poisoned: int = 0
    db_retries: int = 0
    upload_deadlines: int = 0
    log_files_deleted: int = 0


def fetch_scope(job: JobRecord) -> list[Digest]:
    """What an attempt's controller fetch token may read: the tree (its manifest
    plus every member, :class:`~meshforge.controller.blobs.FetchTokens`) for a
    tree job, the bundle for a V0.4 bundle job. The same for StartAttempt and
    every RefreshFetchToken (D95)."""
    if job.tree_id:
        return [Digest.parse(job.tree_id)]
    if not job.bundle_digest:
        raise ValueError(f"job {job.id} has neither a tree nor a bundle")
    return [Digest.parse(job.bundle_digest)]


class Dispatcher:
    """See module docstring. ``run()`` is one task owned by ``ControllerRuntime``."""

    def __init__(
        self,
        store: ExecutionStore,
        manager: SessionManager,
        fetch_tokens: FetchTokens,
        settings: JobApiSettings,
        *,
        upload_grants: UploadGrants | None = None,
        logs: LogHub | None = None,
        clock: Callable[[], float],
        blob_port: Callable[[], int],
        blob_url: str | None = None,
        resources: ResourceTracker | None = None,
        wall_clock: Callable[[], dt.datetime] | None = None,
        peers: PeerSources | None = None,
        on_integrity_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._on_integrity_failure = on_integrity_failure
        """V0.5 G5: a tree attempt failed integrity_error -> the scrubber
        re-hashes that tree's blobs on the controller first."""
        self._peers = peers
        """V0.5 D89/D100: peer holders listed next to the controller (tree jobs)."""
        self._manager = manager
        self._tokens = fetch_tokens
        self._grants = upload_grants
        self._logs = logs
        self._settings = settings
        self._clock = clock
        self._blob_port = blob_port
        self._blob_url = blob_url or ""
        self._resources = resources
        self._wall = wall_clock or (lambda: dt.datetime.now(dt.UTC))
        self._queue: asyncio.Queue[_Item] = asyncio.Queue(settings.status_queue_size)
        self._wake = asyncio.Event()
        self._stopping = False
        self._started_at = clock()
        self.stats = DispatcherStats()
        self._poison: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._uploading_since: dict[str, dt.datetime] = {}
        """attempt_id -> when it entered UPLOADING (from the job history)."""
        self._next_log_gc = self._started_at + settings.log_gc_interval_s
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

    def request_stop(self) -> None:
        """Finish the current step and return from :meth:`run` (no mid-query
        cancel: a cancelled database call can leave driver tasks behind). Items
        still queued are reports not yet acked - the agents replay them."""
        self._stopping = True
        self._wake.set()

    @property
    def stopping(self) -> bool:
        return self._stopping

    async def run(self) -> None:
        """Until :meth:`request_stop`: work items, then the periodic steps."""
        interval = self._settings.dispatch_interval_s
        next_tick = self._clock()
        while not self._stopping:
            timeout = max(0.0, next_tick - self._clock())
            if self._queue.empty() and not self._wake.is_set():
                await self._wait(timeout)
            while not self._queue.empty() and not self.stopping:
                await self._retrying(self._handle, self._queue.get_nowait())
            if self.stopping:
                return
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
            if not self._stopping:
                await self._retrying(self._handle, item)

    async def _retrying(self, step: Callable[..., object], arg: object) -> None:
        delay = _RETRY_BASE_S
        while True:
            try:
                await step(arg)  # type: ignore[misc]
            except JobStoreError as exc:
                if self._stopping:
                    return
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
            try:
                await self._apply_status(item)
            except (JobStoreError, asyncio.CancelledError):
                raise
            except Exception as exc:
                if not self._poisoned(item, exc):
                    raise
        elif isinstance(item, _Acked):
            await self._store.mark_start_acked(item.attempt_id, item.fencing_token)
        elif isinstance(item, _Reconcile):
            await self._reconcile(item)
        else:
            await self._offline(item)

    async def _periodic(self, _: object) -> None:
        await self._dispatch()
        await self._backstop()
        # One read of the live attempts for both sweeps. An attempt the first
        # one ends is still listed for the second: ending it again is a no-op
        # (end_attempt answers DUPLICATE).
        live = await self._store.live_attempts()
        await self._upload_deadline(live)
        await self._sweep_orphans(live)
        if self._logs is not None and self._clock() >= self._next_log_gc:
            self._next_log_gc = self._clock() + self._settings.log_gc_interval_s
            await self._log_gc()

    # ------------------------------------------------------------ reports

    def _poisoned(self, item: _Status, exc: Exception) -> bool:
        """Count a handler failure; at ``POISON_LIMIT`` ack the report (D75).
        True = it was acked (the caller swallows the exception).

        The report is acked *without* being applied - a poisoned ``DONE`` never
        yields ``SUCCEEDED``; the attempt ends ``LOST`` (Reconcile/OFFLINE) or
        via the backstop (see the module docstring)."""
        key = (item.report.attempt_id, item.report.state.value)
        count = self._poison.pop(key, 0) + 1
        if count < POISON_LIMIT:
            self._poison[key] = count
            while len(self._poison) > _POISON_TRACKED:
                self._poison.popitem(last=False)
            return False
        self.stats.statuses_poisoned += 1
        log.error(  # the type only: no user bytes, no traceback
            "controller.jobs.status_poisoned",
            node_id=item.live.node_id,
            session_id=item.live.session_id,
            attempt_id=item.report.attempt_id,
            reported=item.report.state.value,
            failures=count,
            cause=type(exc).__name__,
        )
        self._manager.ack_message(item.live, item.message_id)
        return True

    async def _apply_status(self, item: _Status) -> None:
        report, live = self._verified(item.report, item.live), item.live
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
            # D75: the controller is done with this (attempt, token): the agent
            # must stop it if it still runs and free it either way.
            self._cancel_on(
                live.node_id,
                report.attempt_id,
                report.fencing_token,
                "fenced",
                release=True,
            )
        self._manager.ack_message(live, item.message_id)
        self._after(outcome, report)

    def _verified(self, report: StatusReport, live: LiveSession) -> StatusReport:
        """D73/D74: a DONE's ``output_digest`` counts only if this attempt's upload
        grant stored and verified that blob; otherwise the attempt did not deliver
        its outputs - it ends ``FAILED(output_error)``, never ``DONE`` with a
        digest nobody checked."""
        report = self._verified_logs(report, live)
        digest = report.output_digest
        if report.state is not AttemptState.DONE or digest is None:
            return report
        if self._grants is not None and self._grants.uploaded(
            report.attempt_id, digest
        ):
            return report
        log.warning(
            "controller.jobs.output_not_uploaded",
            node_id=live.node_id,
            session_id=live.session_id,
            attempt_id=report.attempt_id,
            output_digest=digest,
        )
        return dataclasses.replace(
            report,
            state=AttemptState.FAILED,
            failure_type=FailureType.OUTPUT_ERROR,
            detail="the reported output digest was not uploaded under this "
            "attempt's upload grant",
            output_digest=None,
        )

    def _verified_logs(self, report: StatusReport, live: LiveSession) -> StatusReport:
        """D71/D73: a final log digest counts only if this attempt's grant stored
        it; an unverified one is dropped (the live copy stays), never trusted."""
        if not report.logs:
            return report
        kept: dict[str, dict[str, object]] = {}
        for stream, info in report.logs.items():
            entry = dict(info)
            digest = entry.get("digest")
            if digest and not (
                self._grants is not None
                and self._grants.uploaded(report.attempt_id, str(digest))
            ):
                log.warning(
                    "controller.jobs.log_not_uploaded",
                    node_id=live.node_id,
                    attempt_id=report.attempt_id,
                    stream=stream,
                )
                entry["digest"] = None
            kept[stream] = entry
        return dataclasses.replace(report, logs=kept)

    def _after(
        self, outcome: StatusOutcome, report: StatusReport | None = None
    ) -> None:
        """Token expiry at attempt end; a pending cancel re-sent to a live attempt."""
        attempt, job = outcome.attempt, outcome.job
        if attempt is None:
            return
        if outcome.ended:
            self._end_tokens(attempt.id)
            if self._logs is not None and outcome.kind is StatusKind.APPLIED:
                self._logs.finalize(attempt.id, None if report is None else report.logs)
            if (
                self._on_integrity_failure is not None
                and outcome.kind is StatusKind.APPLIED
                and report is not None
                and report.failure_type is FailureType.INTEGRITY_ERROR
                and job is not None
                and job.tree_id
            ):
                # The bytes may have come from the controller's own disk.
                self._on_integrity_failure(job.tree_id)
        elif (
            job is not None
            and job.cancel_requested_at is not None
            and attempt.state not in ATTEMPT_TERMINAL_STATES
            and outcome.kind is StatusKind.APPLIED
        ):
            self._send_cancel(attempt, "cancelled by request")

    def _end_tokens(self, attempt_id: str) -> None:
        """The attempt ended: its fetch token lives ``fetch_token_end_grace_s``
        more, its upload grant takes no new upload (D60/D73)."""
        grace = self._settings.fetch_token_end_grace_s
        self._tokens.expire_after(attempt_id, grace)
        if self._grants is not None:
            self._grants.close(attempt_id, grace)

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
                await self._send_start(result.job, result.attempt, resend=False)

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

    async def _peer_sources(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> list[agent_pb2.BlobSource]:
        if self._peers is None:
            return []
        return await self._peers.for_attempt(job, attempt)

    async def _send_start(
        self, job: JobRecord, attempt: AttemptRecord, *, resend: bool
    ) -> None:
        peers = await self._peer_sources(job, attempt)
        token, ttl = self._mint_fetch_token(job, attempt)
        spec = execution_spec(
            job,
            attempt,
            fetch_token=token.get_secret_value(),
            blob_url=self._blob_url,
            blob_port=self._blob_port(),
        )
        spec.sources.extend(peers)
        if self._grants is not None:
            # D73: the only credential that may upload for this attempt; a
            # re-send keeps what the previous grant already stored.
            grant = self._grants.mint(
                attempt.id,
                ttl_s=ttl,
                max_blobs=self._settings.max_output_blobs,
                max_bytes=self._settings.max_output_bytes,
            )
            spec.upload_token = grant.get_secret_value()
            spec.upload_max_bytes = self._settings.max_output_bytes
            spec.upload_max_blobs = self._settings.max_output_blobs
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
        if self._logs is not None:
            self._logs.open(
                attempt.id,
                job_id=job.id,
                node_id=attempt.node_id,
                token=attempt.fencing_token,
            )
        if resend:
            self.stats.start_resent += 1
        else:
            self.stats.start_sent += 1

    def _mint_fetch_token(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> tuple[SecretStr, float]:
        """A new download-only token for ``attempt`` (D73) and its TTL; every
        older token of the attempt is revoked. One token per send: a re-send or
        a refresh after a controller restart must work although the old token
        died with the old process (tokens live in memory, never persisted).
        Scope (:func:`fetch_scope`) = the tree for a tree job (which grants
        exactly that tree's blobs), the bundle for a V0.4 bundle job."""
        self._tokens.revoke(attempt.id)
        ttl = min(
            float(job.manifest.get("limits", {}).get("timeout", 0))
            + self._settings.fetch_token_grace_s,
            float(MAX_FETCH_TOKEN_TTL_S),
        )
        token = self._tokens.mint(attempt.id, fetch_scope(job), ttl_s=ttl)
        return token, ttl

    async def _refresh_fetch_token(
        self, job: JobRecord, attempt: AttemptRecord
    ) -> None:
        """D95: a held attempt that may still be fetching has no live token in
        this process (the controller restarted): mint one, send it - with the
        whole source list re-minted (D98: fresh peer tokens; the controller
        first, carrying the new fetch token)."""
        peers = await self._peer_sources(job, attempt)
        token, _ttl = self._mint_fetch_token(job, attempt)
        secret = token.get_secret_value()
        sources = (
            [agent_pb2.BlobSource(url=self._blob_url, token=secret, node_id="")]
            if job.tree_id
            else []
        )
        sent = self._manager.send_command(
            attempt.node_id,
            "refresh_fetch_token",
            agent_pb2.RefreshFetchToken(
                attempt_id=attempt.id,
                fencing_token=attempt.fencing_token,
                fetch_token=secret,
                blob_url=self._blob_url,
                blob_port=self._blob_port(),
                sources=[*sources, *peers],
            ),
            attempt_id=attempt.id,
            fencing_token=attempt.fencing_token,
        )
        if sent is None:
            # No live session: nobody holds this token; drop it so the next
            # Reconcile sees "no live token" and refreshes again.
            self._tokens.revoke(attempt.id)
            return
        self.stats.fetch_tokens_refreshed += 1
        log.info(
            "controller.jobs.fetch_token_refreshed",
            job_id=job.id,
            attempt_id=attempt.id,
            node_id=attempt.node_id,
        )

    # ------------------------------------------------------------ cancel

    def _send_cancel(self, attempt: AttemptRecord, reason: str) -> None:
        self._cancel_on(attempt.node_id, attempt.id, attempt.fencing_token, reason)

    def _cancel_on(
        self,
        node_id: str,
        attempt_id: str,
        fencing_token: int,
        reason: str,
        *,
        release: bool = False,
    ) -> None:
        sent = self._manager.send_command(
            node_id,
            "cancel_attempt",
            agent_pb2.CancelAttempt(
                attempt_id=attempt_id,
                fencing_token=fencing_token,
                reason=reason,
                release=release,
            ),
            attempt_id=attempt_id,
            fencing_token=fencing_token,
        )
        if sent is not None:
            self.stats.cancels_sent += 1
            self.stats.releases_sent += int(release)

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
                if self._logs is not None:  # after a controller restart, too
                    self._logs.open(
                        attempt.id,
                        job_id=job.id,
                        node_id=node_id,
                        token=attempt.fencing_token,
                    )
                if job.cancel_requested_at is not None:
                    self._send_cancel(attempt, "cancelled by request")
                elif attempt.state in _MAY_FETCH and not self._tokens.has(attempt.id):
                    await self._refresh_fetch_token(job, attempt)
                continue
            if attempt.start_acked_at is None:
                log.info(
                    "controller.jobs.start_resent",
                    node_id=node_id,
                    session_id=live.session_id,
                    job_id=job.id,
                    attempt_id=attempt.id,
                )
                await self._send_start(job, attempt, resend=True)
                continue
            await self._lose(entry, "agent_lost_attempt")
        for attempt_id, token in item.held.items():
            if attempt_id not in known:
                self._cancel_on(
                    node_id, attempt_id, token, "not_live_on_controller", release=True
                )

    async def _offline(self, item: _Offline) -> None:
        for entry in await self._store.live_attempts(item.node_id):
            await self._lose(entry, "node_offline")

    async def _sweep_orphans(self, live: list[LiveAttempt] | None = None) -> None:
        if self._clock() - self._started_at < self._settings.reconnect_grace_s:
            return
        if live is None:
            live = await self._store.live_attempts()
        for entry in live:
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
        if self._grants is not None:
            self._grants.close(attempt.id)
        if self._logs is not None and outcome.kind is StatusKind.APPLIED:
            self._logs.finalize(attempt.id, None)
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
            self._end_tokens(attempt.id)
            if self._logs is not None:
                self._logs.finalize(attempt.id, None)
            self._send_cancel(attempt, "controller_backstop")

    # ------------------------------------------------------------ D94

    async def _upload_deadline(self, live: list[LiveAttempt] | None = None) -> None:
        """End attempts UPLOADING longer than ``upload_deadline_total_s``
        (``live`` = the store's live attempts, read once per tick; None = read
        them here)."""
        if live is None:
            live = await self._store.live_attempts()
        uploading = [
            entry for entry in live if entry.attempt.state is AttemptState.UPLOADING
        ]
        ids = {entry.attempt.id for entry in uploading}
        for gone in set(self._uploading_since) - ids:
            del self._uploading_since[gone]
        if not uploading:
            return
        deadline = dt.timedelta(seconds=self._settings.upload_deadline_total_s)
        now = self._wall()
        for entry in uploading:
            attempt = entry.attempt
            since = self._uploading_since.get(attempt.id)
            if since is None:
                since = await self._upload_start(entry)
                self._uploading_since[attempt.id] = since
            if now - since < deadline:
                continue
            outcome = await self._store.end_attempt(
                attempt.id,
                attempt.fencing_token,
                event=AttemptEvent.FAIL,
                failure_type=FailureType.OUTPUT_ERROR,
                detail="upload_deadline_exceeded",
            )
            self._uploading_since.pop(attempt.id, None)
            if outcome.kind is not StatusKind.APPLIED:
                continue
            self.stats.upload_deadlines += 1
            log.warning(
                "controller.jobs.upload_deadline",
                job_id=attempt.job_id,
                attempt_id=attempt.id,
                node_id=attempt.node_id,
                uploading_s=round((now - since).total_seconds(), 1),
            )
            # Revoked, not closed: nothing this attempt uploads or reports from
            # now on is accepted (a late PUT is refused, a late DONE is fenced).
            self._tokens.revoke(attempt.id)
            if self._grants is not None:
                self._grants.revoke(attempt.id)
            if self._logs is not None:
                self._logs.finalize(attempt.id, None)
            self._send_cancel(attempt, "upload_deadline_exceeded")

    async def _upload_start(self, entry: LiveAttempt) -> dt.datetime:
        """When the attempt entered UPLOADING: the job's last ``COLLECTING``
        transition (written in the same transaction). Without one (the job arrow
        was skipped) the earliest bound known - the attempt's start - so the
        deadline can only come sooner, never later."""
        history = await self._store.history(entry.job.id)
        for _from, to_state, at in reversed(history):
            if to_state is JobState.COLLECTING:
                return at
        return entry.attempt.started_at or entry.attempt.created_at

    async def _log_gc(self) -> None:
        """Delete the log files of terminal jobs past ``log_retention_s``."""
        if self._logs is None:
            return
        retention = dt.timedelta(seconds=self._settings.log_retention_s)
        now = self._wall()

        async def expired(attempt_id: str) -> bool:
            # The terminal guard: only the database decides a job is over.
            attempt = await self._store.get_attempt(attempt_id)
            if attempt is None:
                return True  # no such attempt (its job was deleted): orphan file
            if attempt.state not in ATTEMPT_TERMINAL_STATES:
                return False
            job = await self._store.get_job(attempt.job_id)
            if job is None:
                return True
            return (
                job.state in JOB_TERMINAL_STATES and now - job.updated_at >= retention
            )

        deleted = await self._logs.collect_garbage(
            expired,
            older_than=now - retention,
            limit=self._settings.log_gc_batch,
        )
        self.stats.log_files_deleted += deleted
