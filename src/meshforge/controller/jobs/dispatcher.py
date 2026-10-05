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
Any held attempt without a live upload grant in this process gets
``RefreshUploadGrant`` with a new grant of the StartAttempt's quota (V0.6 G1,
D114) - so does one whose latest grant-carrying message (a re-sent
StartAttempt, or a refresh) was never acked: the agent may not have it.

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
**Layout (V0.6 G1, D119).** This module is the loop, the work queue and the
report path; :mod:`.dispatch` places jobs and sends attempt commands
(``StartAttempt``, ``RefreshFetchToken``, ``RefreshUploadGrant``,
``CancelAttempt``); :mod:`.recovery`
holds Reconcile, OFFLINE, the orphan sweep, the backstop, the upload deadline
and log GC. They are one class (:class:`Dispatcher` <- ``RecoverySteps`` <-
``DispatchSteps``) on purpose: one object, one task, one owner of attempts.
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

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import ATTEMPT_TERMINAL_STATES, AttemptState
from meshforge.controller.blobs import FetchTokens
from meshforge.controller.jobs.dispatch import DispatcherStats, fetch_scope
from meshforge.controller.jobs.env_health import KnownBadEnvs, env_key
from meshforge.controller.jobs.execution_store import (
    ExecutionStore,
    StatusKind,
    StatusOutcome,
    StatusReport,
)
from meshforge.controller.jobs.logs import LogHub
from meshforge.controller.jobs.peers import PeerSources
from meshforge.controller.jobs.recovery import RecoverySteps, _Offline, _Reconcile
from meshforge.controller.jobs.reports import parse_status
from meshforge.controller.jobs.store import JobRecord, JobStoreError
from meshforge.controller.liveness import LivenessEvent
from meshforge.controller.named_inputs import NamedInputStore
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.live import LiveSession, PendingCommand
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.settings import JobApiSettings
from meshforge.controller.upload_grants import UploadGrants

__all__ = [
    "POISON_LIMIT",
    "Dispatcher",
    "DispatcherStats",
    "fetch_scope",
]

log = get_logger(__name__)

_RETRY_BASE_S: Final = 0.2
_RETRY_CAP_S: Final = 5.0
POISON_LIMIT: Final = 3
"""Failures of one ``(attempt_id, state)`` report before it is acked anyway (D75)."""
_POISON_TRACKED: Final = 1024


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
    payload: str = "start_attempt"
    """``start_attempt`` or ``refresh_upload_grant`` (V0.6 G1, D114)."""
    message_id: str = ""


type _Item = _Status | _Acked | _Reconcile | _Offline


class Dispatcher(RecoverySteps):
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
        inputs: NamedInputStore | None = None,
        known_bad_envs: KnownBadEnvs | None = None,
    ) -> None:
        self._store = store
        self.known_bad_envs = known_bad_envs
        """V0.6 G1 (D116): (node, env) pairs that failed env_error recently."""
        self._inputs = inputs
        """V0.5 G5 (D92): the job's resolved named inputs (job_input rows)."""
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
        self._grant_unacked: dict[str, str] = {}
        """D114: attempt_id -> message_id of the last message that carried the
        attempt's upload grant (StartAttempt or RefreshUploadGrant) while the
        agent has not acked it: a broken stream may have taken it along, so a
        Reconcile that finds the attempt held mints and sends another."""
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
        self, live: LiveSession, message_id: str, pending: PendingCommand
    ) -> None:
        if pending.payload in ("start_attempt", "refresh_upload_grant"):
            self._put(
                _Acked(
                    live.node_id,
                    pending.attempt_id,
                    pending.fencing_token,
                    pending.payload,
                    message_id,
                )
            )

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
            if self._grant_unacked.get(item.attempt_id) == item.message_id:
                del self._grant_unacked[item.attempt_id]
            if item.payload == "start_attempt":
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
        self._remember_bad_env(outcome, report)
        await self._suspect_trees(outcome, report)

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
        elif (
            job is not None
            and job.cancel_requested_at is not None
            and attempt.state not in ATTEMPT_TERMINAL_STATES
            and outcome.kind is StatusKind.APPLIED
        ):
            self._send_cancel(attempt, "cancelled by request")

    def _remember_bad_env(self, outcome: StatusOutcome, report: StatusReport) -> None:
        """D116: an attempt that ended env_error makes (node, env) known-bad."""
        attempt, job = outcome.attempt, outcome.job
        if (
            self.known_bad_envs is None
            or not outcome.ended
            or outcome.kind is not StatusKind.APPLIED
            or report.failure_type is not FailureType.ENV_ERROR
            or attempt is None
            or job is None
        ):
            return
        self.known_bad_envs.record(
            attempt.node_id,
            env_key(job.manifest, tree_id=job.tree_id, bundle_digest=job.bundle_digest),
            env_hash=report.env_hash,
            job_id=job.id,
            attempt_id=attempt.id,
        )

    async def _suspect_trees(
        self, outcome: StatusOutcome, report: StatusReport
    ) -> None:
        """A tree attempt ended ``integrity_error``: the bytes may have come
        from the controller's own disk, so the scrubber re-hashes the project
        tree **and every named-input tree** of the job first (G5-T3)."""
        job = outcome.job
        if (
            self._on_integrity_failure is None
            or not outcome.ended
            or outcome.kind is not StatusKind.APPLIED
            or report.failure_type is not FailureType.INTEGRITY_ERROR
            or job is None
            or not job.tree_id
        ):
            return
        trees = [job.tree_id]
        try:
            trees += [i.tree_id for i in await self._job_inputs(job)]
        except Exception as exc:  # noqa: BLE001 - the project tree is still scrubbed
            log.warning(
                "controller.jobs.scrub_inputs_unlisted",
                job_id=job.id,
                attempt_id=report.attempt_id,
                cause=type(exc).__name__,
            )
        for tree_id in dict.fromkeys(trees):
            self._on_integrity_failure(tree_id)
