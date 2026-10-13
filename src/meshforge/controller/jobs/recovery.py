"""Recovery: everything that ends or repairs attempts the agent cannot report
on (V0.4 D66/D69, V0.5 D94/D95; split out of :mod:`.dispatcher` in V0.6 G1,
D119).

:class:`RecoverySteps` adds to :class:`~.dispatch.DispatchSteps`:

* **Reconcile (D66)** - for the node's live attempts: held -> continue
  (re-sending a pending cancel, refreshing a fetch token this process lacks,
  D95, and an upload grant it lacks, V0.6 D114); not held and
  ``StartAttempt`` never acked -> re-send it; not held but acked -> ``LOST``.
  Attempts the agent holds that the controller does not consider live get
  ``CancelAttempt{release}`` (D75).
* **OFFLINE** caused by the agent -> its live attempts ``LOST``.
* **Orphan sweep** - after ``reconnect_grace_s`` of controller uptime, live
  attempts of nodes with no live or lingering session are ``LOST``.
* Every ``LOST`` goes through :meth:`~.execution_store.ExecutionStore.
  end_attempt`, which retries the job in the same transaction (V0.6 G3,
  D110) - unless it opted out (``on_lost: false``) or used its attempts.
* **Overlaps (D117)** - a Reconcile (or a fenced report) naming an attempt the
  controller ended ``LOST`` while its job already has a newer attempt: the
  partitioned agent kept running it. Counted (``execution_overlaps``) and
  logged ``controller.jobs.execution_overlap`` once per attempt; the agent
  is told ``CancelAttempt{release}`` and nothing it reports is committed.
* **Backstop (D69)** - RUNNING past the job's limit + ``backstop_grace_s``.
* **Upload deadline (D94)** - UPLOADING past ``upload_deadline_total_s``.
* **Log GC (D94)**.

Like :class:`~.dispatch.DispatchSteps` it has no state of its own and runs only
inside the dispatcher's one task.
"""

from __future__ import annotations

import datetime as dt
from collections import OrderedDict
from dataclasses import dataclass
from typing import Final

from meshforge.common.errors import FailureType
from meshforge.common.logging import get_logger
from meshforge.common.states import (
    ATTEMPT_TERMINAL_STATES,
    JOB_TERMINAL_STATES,
    AttemptState,
    JobState,
)
from meshforge.controller.jobs.dispatch import DispatchSteps
from meshforge.controller.jobs.execution_store import LiveAttempt, StatusKind
from meshforge.controller.jobs.transitions import AttemptEvent
from meshforge.controller.liveness import LivenessEvent
from meshforge.controller.session.live import LiveSession

__all__ = ["RecoverySteps"]

log = get_logger(__name__)

_OVERLAPS_TRACKED: Final = 4096
_MAY_FETCH: Final = frozenset({AttemptState.ASSIGNED, AttemptState.STAGING})
"""Attempt states (as stored) in which the agent may still fetch from the
controller; the store is never ahead of the agent, so this is a superset (D95)."""


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


class RecoverySteps(DispatchSteps):
    """See module docstring."""

    _overlaps_seen: OrderedDict[str, None]
    """D117: attempts already counted as an overlapping execution."""
    _uploading_since: dict[str, dt.datetime]
    """attempt_id -> when it entered UPLOADING (from the job history)."""
    _offline_pending: dict[str, int]
    """node_id -> queued, not yet handled ``_Offline`` items (see
    :meth:`_sweep_orphans`)."""

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
                    known_log = self._logs.live_state(attempt.id)
                    self._logs.open(
                        attempt.id,
                        job_id=job.id,
                        node_id=node_id,
                        token=attempt.fencing_token,
                        secret_values=(
                            await self._log_secrets(job)
                            if known_log is None or not known_log.redactors
                            else None
                        ),
                    )
                if job.cancel_requested_at is not None:
                    self._send_cancel(attempt, "cancelled by request")
                    continue
                if attempt.state in _MAY_FETCH and not self._tokens.has(attempt.id):
                    await self._refresh_fetch_token(job, attempt)
                if self._grants is not None and (
                    not self._grants.has(attempt.id)
                    or attempt.id in self._grant_unacked
                ):
                    # D114: any live stored state - reports may still wait for
                    # their replay, so the agent can be further than the store.
                    # A grant whose message was never acked (a re-sent start,
                    # a refresh) may have died with the old stream.
                    await self._refresh_upload_grant(job, attempt)
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
                await self._check_overlap(node_id, attempt_id, token, "reconcile")
                self._cancel_on(
                    node_id, attempt_id, token, "not_live_on_controller", release=True
                )

    async def _check_overlap(
        self, node_id: str, attempt_id: str, token: int, source: str
    ) -> None:
        """D117 / ADR-0015: an agent still holds (or reports on) an attempt the
        controller ended ``LOST`` while the job has a newer attempt - the
        partitioned laptop kept running it while the retry ran elsewhere.
        Never committed (fenced) and now released, but the overlap is real:
        count it and log it, once per attempt."""
        if attempt_id in self._overlaps_seen:
            return
        attempt = await self._store.get_attempt(attempt_id)
        if (
            attempt is None
            or attempt.fencing_token != token
            or attempt.state is not AttemptState.LOST
        ):
            return
        job = await self._store.get_job(attempt.job_id)
        if job is None or job.attempt_epoch <= attempt.fencing_token:
            return
        self._overlaps_seen[attempt_id] = None
        while len(self._overlaps_seen) > _OVERLAPS_TRACKED:
            self._overlaps_seen.popitem(last=False)
        self.stats.execution_overlaps += 1
        log.warning(
            "controller.jobs.execution_overlap",
            job_id=attempt.job_id,
            attempt_id=attempt_id,
            node_id=node_id,
            fencing_token=token,
            current_attempt=job.attempt_epoch,
            job_state=job.state.value,
            source=source,
        )

    async def _offline(self, item: _Offline) -> None:
        # V0.7 G3 (D138): a revoked node loses its attempts like a node that
        # went OFFLINE (LOST, retried elsewhere); the detail says why.
        detail = (
            "node_revoked" if item.event is LivenessEvent.REVOKE else "node_offline"
        )
        for entry in await self._store.live_attempts(item.node_id):
            await self._lose(entry, detail)

    async def _sweep_orphans(self, live: list[LiveAttempt] | None = None) -> None:
        if self._clock() - self._started_at < self._settings.reconnect_grace_s:
            return
        if live is None:
            live = await self._store.live_attempts()
        for entry in live:
            node_id = entry.attempt.node_id
            if node_id in self._offline_pending:
                # The node's session just ended (lease expiry, Goodbye,
                # revocation) and its ``_Offline`` item waits in the queue
                # behind this tick: that item loses the attempts and names
                # the real cause (``node_offline`` / ``node_revoked``). Without
                # this, a tick already running when the lease expired labelled
                # them ``node_not_connected`` instead (seen under load, V0.7 G6
                # T-11). An item that was dropped (queue full) is not pending,
                # so this sweep stays the backstop.
                continue
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
        self._grant_unacked.pop(attempt.id, None)
        if self._grants is not None:
            self._grants.close(attempt.id)
        if self._logs is not None and outcome.kind is StatusKind.APPLIED:
            self._logs.finalize(attempt.id, None)
        self._note_retry(outcome)
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
            self._note_retry(outcome)
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
            self._note_retry(outcome)
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
            self._grant_unacked.pop(attempt.id, None)
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
