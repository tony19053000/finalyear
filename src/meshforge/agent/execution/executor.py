"""The agent's attempt executor (V0.4 G4, D45/D52/D67).

One :class:`Executor` per agent process. The session client hands it every new
``StartAttempt`` / ``CancelAttempt`` **synchronously, before the Ack** (so an Ack
means "held", D66); each accepted attempt then runs as **one task** that owns it
from start to its terminal report:

``ASSIGNED`` (the command) -> ``STAGING`` (a tree job, V0.5: fetch the tree's
missing blobs into the agent's CAS and copy them into ``<work root>/<attempt_id>``
hashing while copying, :func:`.fetch.stage_tree`; a V0.4 bundle job: fetch the
bundle with the attempt's fetch token, verify its digest, unpack it
hostile-input-safe) -> ``ENV_READY`` + ``RUNNING`` (reported when the
runtime starts the workload - image pull and requirements install happen before,
in the runtime's env phase) -> ``UPLOADING`` (a successful run's declared outputs
are collected, packed and uploaded with the attempt's upload grant,
:mod:`.outputs`, D74) -> ``DONE`` (with ``output_digest`` and
``missing_outputs``) or ``FAILED(failure_type)`` - ``output_error`` when the
outputs could not be collected or uploaded.

**Uploads across a controller restart (V0.6 G1, D114).** Logs and outputs go up
resumably (:mod:`.uploader`: parts the controller already holds are not sent
again). A refused grant (``401``: a restarted controller forgot it) waits for
``RefreshUploadGrant``, which replaces the attempt's grant and blob location
(only for the same ``(attempt_id, fencing_token)`` while it has not ended); an
unreachable controller is retried with backoff. Both stop after
``upload_retry_s`` from the first failure -> ``output_error`` (retryable).

Every change is an ``AttemptStatus`` sent through the session's ``send_report``
(buffered, replayed after a reconnect, acked); the terminal one carries the exit
code and the phase durations (``stage``/``env``/``run``/``collect``, monotonic
clock; a phase that never ran is absent, never 0 - D68).

Rules:

* **Opt-in (D45).** With ``[agent.execution] enabled = false`` a ``StartAttempt``
  is answered ``FAILED(unauthorized)`` and nothing runs.
* **Dedupe on (attempt_id, fencing_token)** (D66): the controller re-sends the same
  attempt with a new ``message_id``; a copy of one this process holds or has
  finished recently is ignored.
* **Cancel** (``CancelAttempt`` with the attempt's token) stops the current step:
  the runtime's graceful-then-forced stop, or the fetch. The attempt ends
  ``FAILED(cancelled)``.
* **Agent stop (D52).** :meth:`Executor.shutdown` cancels every running attempt;
  each reports ``FAILED(node_lost, detail=agent_shutdown)`` - retryable in V0.6 -
  and the session sends its Goodbye only afterwards.
* **Held until acked (D75).** An attempt stays listed for ``Reconcile`` until its
  terminal report is acked; then its workdir is deleted (unless
  ``keep_workdirs``). Until then the terminal report is **re-sent** every
  ``terminal_resend_s`` and after every Reconcile (:meth:`Executor.after_reconcile`):
  the same buffered message while the session's ring still holds it, a new one if
  the ring evicted it. A report the controller dropped (full queue) or never got
  (evicted before a reconnect) therefore still lands - long before the
  controller's timeout backstop could mislabel a finished attempt.
* **Staging retries (D95).** A fetch that could not reach the controller
  (``transfer_error``/``timeout``) is retried with backoff; one whose token was
  refused (``unauthorized``) waits for a ``RefreshFetchToken`` (a restarted
  controller sends one after Reconcile) and retries with it - a tree resumes
  the chunks it already verified. Past ``staging_retry_s`` the attempt fails
  ``transfer_error``; ``staging_retry_s = 0`` fails at the first error, typed.
* **Release (D75).** ``CancelAttempt{release}`` means the controller already ended
  (or never knew) the attempt: it is stopped if it runs, then forgotten and its
  workdir freed without waiting for an Ack.
* **Logs (D71).** Output bytes go to ``.meshforge/{stdout,stderr}.log`` in the
  workdir (capped per stream, :mod:`.logs`) and, batched (64 KiB / 100 ms), to
  the session's live log queue as ``LogChunk``. After a run that was not stopped
  the two files are uploaded with the upload grant and named in the terminal
  status (``stdout_log`` / ``stderr_log``) - best effort: a failed log upload
  leaves the controller's live copy, it never fails the attempt. A stopped
  (cancelled / agent-stop) attempt skips the upload so it ends within its grace.

A failure anywhere is a typed ``failure_type`` in a ``FAILED`` report - never an
exception out of the task (a bug is reported ``internal_error`` and logged with
its traceback).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import stat
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol

from meshforge.agent.cas import AgentCas
from meshforge.agent.cas.fetcher import FetchTuning
from meshforge.agent.envs.fsutil import make_read_only, remove_tree
from meshforge.agent.execution.failure_evidence import (
    STDERR_TAIL_BYTES,
    OutputTail,
    disk_full_cause,
    refine_result,
)
from meshforge.agent.execution.fetch import (
    FetchError,
    StageReport,
    UploadError,
    blob_base_url,
    blob_url_ok,
    fetch_bundle,
    stage_tree,
    token_ok,
    tree_sources,
)
from meshforge.agent.execution.logs import AttemptLogs, Chunk, file_sha256
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.outputs import Collected, OutputError, collect_outputs
from meshforge.agent.execution.plan import (
    MAX_INPUTS,
    AttemptPlan,
    PlanError,
    plan_attempt,
)
from meshforge.agent.execution.runtime import RunObserver, RunResult, Stream
from meshforge.agent.execution.uploader import UploadProgress, upload_parts
from meshforge.agent.gc.leases import AttemptLeases, LeaseError
from meshforge.agent.settings import AgentSettings
from meshforge.common.bundle import BundleError, BundleLimits, unpack
from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.hashing import Digest
from meshforge.common.ids import validate_attempt_id, validate_job_id
from meshforge.common.logging import get_logger
from meshforge.common.manifest import check_mount
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import attempt_state_to_proto, failure_type_to_proto
from meshforge.common.states import AttemptState

if TYPE_CHECKING:
    from meshforge.agent.gc.service import GcService

__all__ = ["Executor", "LogSink", "Reporter", "Resender", "work_root_for"]

log = get_logger(__name__)

_RECENT_FINISHED: Final = 4096
_MISSING_MAX: Final = 64
_MISSING_MAX_CHARS: Final = 512
_SHUTDOWN_SLACK_S: Final = 3.0
_WORK_DIR_NAME: Final = "work"
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
_STAGE_RETRYABLE: Final = frozenset(
    {FailureType.TRANSFER_ERROR, FailureType.TIMEOUT, FailureType.UNAUTHORIZED}
)
"""Staging failures that a controller restart causes (connection refused/reset,
a timeout, the old fetch token refused): retried until ``staging_retry_s`` (D95)."""
_STAGE_RETRY_BASE_S: Final = 0.25
_STAGE_RETRY_CAP_S: Final = 5.0
_RETRYABLE: Final = frozenset(
    {
        FailureType.NODE_LOST,
        FailureType.TRANSFER_ERROR,
        FailureType.INTEGRITY_ERROR,
        FailureType.TIMEOUT,
        FailureType.NETWORK_ERROR,
        FailureType.RESOURCE_BUSY,
        FailureType.VRAM_OOM,
    }
)


class Reporter(Protocol):
    """``AgentSessionClient.send_report``'s shape."""

    async def __call__(self, payload_name: str, payload: Any) -> str: ...


LogSink = Callable[[agent_pb2.LogChunk], bool]
"""``AgentSessionClient.send_log``: queue a live chunk, False = dropped."""


class Resender(Protocol):
    """``AgentSessionClient.resend_report``'s shape (False = no longer buffered)."""

    async def __call__(self, message_id: str) -> bool: ...


def work_root_for(settings: AgentSettings) -> Path:
    """``[agent.execution] workdir_root`` or ``<data_dir>/work``."""
    configured = settings.agent.execution.workdir_root
    return configured if configured is not None else settings.data_dir / _WORK_DIR_NAME


class _StoppedError(Exception):
    """The current step was cancelled on purpose (cancel or agent stop)."""


@dataclass(slots=True)
class _Attempt:
    job_id: str
    attempt_id: str
    token: int
    state: AttemptState = AttemptState.ASSIGNED
    runtime: str = ""
    task: asyncio.Task[None] | None = None
    step: asyncio.Future[Any] | None = None
    stop_reason: str | None = None
    """``cancelled`` or ``agent_shutdown`` once a stop was asked for."""
    terminal_message_id: str | None = None
    terminal_status: agent_pb2.AttemptStatus | None = None
    """The final report, kept until acked so it can be re-sent (D75)."""
    terminal_sent_at: float = 0.0
    release_requested: bool = False
    workdir: Path | None = None
    phases: dict[str, float] = field(default_factory=dict)
    fetch_token: str = ""
    """The current fetch token (StartAttempt's, or a RefreshFetchToken's). A
    secret: never logged."""
    blob_url: str | None = None
    blob_port: int | None = None
    """Set by a RefreshFetchToken: where the (restarted) controller serves
    blobs now; None = the StartAttempt's values."""
    sources: list[agent_pb2.BlobSource] | None = None
    """Set by a RefreshFetchToken that carries a source list (V0.5 G4, D98):
    the controller with the new token plus re-minted peers. Secrets inside."""
    token_changed: asyncio.Event = field(default_factory=asyncio.Event)
    stage_retries: int = 0
    leases: AttemptLeases | None = None
    """What this attempt holds against GC (V0.5 G5, D91): its trees and env."""
    inputs: list[agent_pb2.InputTree] | None = None
    """Set by a RefreshFetchToken that carries inputs (V0.5 G5): their source
    lists re-minted. Secrets inside."""
    stages: list[StageReport] = field(default_factory=list)
    """Every tree staged for this attempt (project first, then inputs): the
    transfer telemetry of its terminal report (V0.5 G5, D93)."""
    env_hash: str = ""
    """The environment the runtime resolved or tried to build (V0.6 D116);
    sent in the terminal report."""
    upload_token: str = ""
    """The current upload grant (StartAttempt's, or a RefreshUploadGrant's,
    V0.6 D114). A secret: never logged."""
    upload_url: str | None = None
    upload_port: int | None = None
    grant_changed: asyncio.Event = field(default_factory=asyncio.Event)
    upload_deadline: float | None = None
    """When the upload retries give up: ``upload_retry_s`` after the first
    failed upload (one budget for logs and outputs)."""
    upload_retries: int = 0
    uploads: UploadProgress = field(default_factory=UploadProgress)
    """What the resumable uploads of this attempt sent (D114)."""

    @property
    def ended(self) -> bool:
        return self.state in (AttemptState.DONE, AttemptState.FAILED)


class _Observer(RunObserver):
    """A :class:`RunObserver` that also signals when the workload starts."""

    __slots__ = ("started",)

    def __init__(self, clock: Callable[[], float]) -> None:
        super().__init__(clock=clock)
        self.started = asyncio.Event()

    def enter_run(self) -> None:
        super().enter_run()
        self.started.set()


class Executor:
    """See module docstring."""

    def __init__(
        self,
        settings: AgentSettings,
        setup: ExecutionSetup,
        *,
        node_id: str,
        report: Reporter,
        controller_host: Callable[[], str | None],
        declared_gpus: Collection[int] = (),
        clock: Callable[[], float] = time.monotonic,
        fetch: Callable[..., Awaitable[int]] = fetch_bundle,
        upload: Callable[..., Awaitable[int]] | None = None,
        resend: Resender | None = None,
        log_sink: LogSink | None = None,
        cas: AgentCas | None = None,
        gc: GcService | None = None,
    ) -> None:
        self._settings = settings
        self._cfg = settings.agent.execution
        self._fetch_cfg = settings.agent.fetch
        self._setup = setup
        self._node_id = node_id
        self._report = report
        self._controller_host = controller_host
        self._declared_gpus = frozenset(declared_gpus)
        self._clock = clock
        self._fetch = fetch
        self._upload: Callable[..., Awaitable[int]] = upload or self._upload_parts
        self._resumable = upload is None
        """D114: the default uploader resumes parts (tests may inject one)."""
        self._resend = resend
        self._log_sink = log_sink
        self.log_chunks_dropped = 0
        self.work_root = work_root_for(settings)
        self.cas = cas if cas is not None else AgentCas.at(settings.data_dir)
        """V0.5 G1: the blob cache tree jobs are staged from (D84)."""
        self.gc = gc
        """V0.5 G5 (D91): leases per attempt; a pass is asked for after a
        staging (the CAS may be over its quota now)."""
        self.last_stage: StageReport | None = None
        """The latest tree staging's numbers (logged; tests read it)."""
        self._attempts: dict[str, _Attempt] = {}
        self._finished: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._by_terminal_message: dict[str, str] = {}
        self._closing = False
        self._cleanup: set[asyncio.Task[None]] = set()
        self._resend_task: asyncio.Task[None] | None = None
        self._resend_wake = asyncio.Event()
        self.terminal_resends = 0
        """Final reports re-sent because no Ack came (D75; tests read it)."""

    # ------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        """Once at agent start: work root 0o700, stale leftovers removed, Docker
        orphans with this node's label swept (D63-6, D67)."""
        await asyncio.to_thread(self._prepare_work_root)
        docker = self._setup.docker
        if docker is not None:
            try:
                await docker.sweep_orphans()
            except Exception as exc:  # noqa: BLE001 - a sweep failure is not fatal
                log.warning(
                    "agent.execution.orphan_sweep_failed",
                    node_id=self._node_id,
                    cause=type(exc).__name__,
                )

    def _prepare_work_root(self) -> None:
        root = self.work_root
        root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            root.chmod(0o700)
        if self._cfg.keep_workdirs:
            return
        for leftover in root.iterdir():
            # A previous process's attempts: the controller has lost them (they
            # were not in this process's Reconcile), nothing will collect them.
            if leftover.is_dir() and not leftover.is_symlink():
                # remove_tree: read-only named inputs (V0.5 G5) included.
                with contextlib.suppress(OSError):
                    remove_tree(leftover)
            else:
                with contextlib.suppress(OSError):
                    leftover.unlink()

    async def shutdown(self) -> None:
        """Stop every running attempt; each reports ``node_lost/agent_shutdown``."""
        self._closing = True
        running = [a for a in self._attempts.values() if not a.ended]
        for attempt in running:
            self._stop(attempt, "agent_shutdown")
        tasks = [a.task for a in running if a.task is not None]
        if tasks:
            bound = self._cfg.grace_s + _SHUTDOWN_SLACK_S
            _done, pending = await asyncio.wait(tasks, timeout=bound)
            if pending:
                log.warning(
                    "agent.execution.shutdown_slow",
                    node_id=self._node_id,
                    pending=len(pending),
                    bound_s=bound,
                )

    async def close(self) -> None:
        """Final teardown (agent exit): stop, then cancel anything left."""
        await self.shutdown()
        leftovers = [
            a.task for a in self._attempts.values() if a.task and not a.task.done()
        ]
        if self._resend_task is not None:
            leftovers.append(self._resend_task)
        for task in [*leftovers, *self._cleanup]:
            task.cancel()
        await asyncio.gather(*leftovers, *self._cleanup, return_exceptions=True)

    # ------------------------------------------------------------ commands

    def handle(self, msg: agent_pb2.ControllerMessage) -> None:
        """A new command (synchronous; see module docstring)."""
        which = msg.WhichOneof("payload")
        if which == "start_attempt":
            self._start(msg.start_attempt)
        elif which == "cancel_attempt":
            self._cancel(msg.cancel_attempt)
        elif which == "refresh_fetch_token":
            self._refresh_token(msg.refresh_fetch_token)
        elif which == "refresh_upload_grant":
            self._refresh_grant(msg.refresh_upload_grant)

    def held(self) -> list[agent_pb2.AttemptStatus]:
        """Every attempt this process holds, for ``Reconcile`` (D66): running ones
        and ended ones whose terminal report is not acked yet."""
        return [
            agent_pb2.AttemptStatus(
                job_id=a.job_id,
                attempt_id=a.attempt_id,
                fencing_token=a.token,
                state=attempt_state_to_proto(a.state),
            )
            for a in self._attempts.values()
        ]

    def report_acked(self, message_id: str) -> None:
        """A report was acked; a terminal one releases its attempt."""
        attempt_id = self._by_terminal_message.pop(message_id, None)
        if attempt_id is None:
            return
        attempt = self._attempts.get(attempt_id)
        if attempt is not None:
            self._release(attempt, cause="acked")

    def after_reconcile(self) -> None:
        """A stream's Reconcile + replay went out: re-send every held final
        report now (one evicted from the ring is not in that replay, D75)."""
        for attempt in self._attempts.values():
            if attempt.terminal_status is not None:
                attempt.terminal_sent_at = float("-inf")
        self._resend_wake.set()

    def _release(self, attempt: _Attempt, *, cause: str) -> None:
        """Forget ``attempt`` and free its workdir (terminal Ack or release)."""
        if self._attempts.pop(attempt.attempt_id, None) is None:
            return
        for message_id in [
            m for m, a in self._by_terminal_message.items() if a == attempt.attempt_id
        ]:
            del self._by_terminal_message[message_id]
        self._remember_finished(attempt)
        log.info(
            "agent.execution.attempt_released",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            cause=cause,
        )
        if attempt.workdir is not None and not self._cfg.keep_workdirs:
            task = asyncio.create_task(
                asyncio.to_thread(_remove_quietly, attempt.workdir),
                name=f"attempt-cleanup:{attempt.attempt_id}",
            )
            self._cleanup.add(task)
            task.add_done_callback(self._cleanup.discard)

    # ------------------------------------------------------------ D75 re-send

    def _ensure_resend_loop(self) -> None:
        if self._resend_task is None or self._resend_task.done():
            self._resend_task = asyncio.create_task(
                self._resend_loop(), name=f"attempt-resend:{self._node_id}"
            )
            self._resend_task.add_done_callback(self._task_done)

    async def _resend_loop(self) -> None:
        """Re-send unacked final reports every ``terminal_resend_s`` (D75)."""
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._resend_wake.wait(), self._cfg.terminal_resend_s / 2
                )
            self._resend_wake.clear()
            await self.resend_due()

    async def resend_due(self) -> int:
        """Re-send every held final report last sent ``terminal_resend_s`` ago or
        more (the loop calls this; tests call it with an injected clock)."""
        now = self._clock()
        due = [
            a
            for a in self._attempts.values()
            if a.terminal_status is not None
            and now - a.terminal_sent_at >= self._cfg.terminal_resend_s
        ]
        for attempt in due:
            await self._resend_terminal(attempt)
        return len(due)

    async def _resend_terminal(self, attempt: _Attempt) -> None:
        status = attempt.terminal_status
        if status is None or attempt.attempt_id not in self._attempts:
            return
        attempt.terminal_sent_at = self._clock()
        self.terminal_resends += 1
        kept = attempt.terminal_message_id
        if kept is not None and self._resend is not None and await self._resend(kept):
            same = True
        else:
            message_id = await self._report("attempt_status", status)
            if attempt.attempt_id in self._attempts:  # not released meanwhile
                attempt.terminal_message_id = message_id
                self._by_terminal_message[message_id] = attempt.attempt_id
            same = False
        log.info(
            "agent.execution.terminal_resent",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.token,
            same_message=same,
        )

    def _remember_finished(self, attempt: _Attempt) -> None:
        self._finished[(attempt.attempt_id, attempt.token)] = None
        while len(self._finished) > _RECENT_FINISHED:
            self._finished.popitem(last=False)

    def _start(self, msg: agent_pb2.StartAttempt) -> None:
        try:
            attempt_id = str(validate_attempt_id(msg.attempt_id))
            job_id = str(validate_job_id(msg.job_id))
        except InvalidIdError:
            log.warning("agent.execution.start_malformed", node_id=self._node_id)
            return  # nothing to report against: the controller's ids are broken
        token = int(msg.fencing_token)
        existing = self._attempts.get(attempt_id)
        if existing is not None or (attempt_id, token) in self._finished:
            log.info(
                "agent.execution.start_duplicate",
                node_id=self._node_id,
                job_id=job_id,
                attempt_id=attempt_id,
                fencing_token=token,
                held_token=None if existing is None else existing.token,
            )
            return
        attempt = _Attempt(job_id=job_id, attempt_id=attempt_id, token=token)
        self._attempts[attempt_id] = attempt
        running = sum(1 for a in self._attempts.values() if not a.ended)
        if self._closing:
            coro = self._refuse(
                attempt, FailureType.NODE_LOST, "agent_shutdown", retryable=True
            )
        elif not self._setup.enabled:
            coro = self._refuse(
                attempt,
                FailureType.UNAUTHORIZED,
                "execution is disabled on this node ([agent.execution] enabled = false)",
            )
        elif running > self._cfg.max_concurrent_attempts:
            coro = self._refuse(
                attempt,
                FailureType.RESOURCE_BUSY,
                "this node already runs its maximum number of attempts",
                retryable=True,
            )
        else:
            coro = self._lifecycle(attempt, msg)
        attempt.task = asyncio.create_task(coro, name=f"attempt:{attempt_id}")
        attempt.task.add_done_callback(self._task_done)
        log.info(
            "agent.execution.attempt_accepted",
            node_id=self._node_id,
            job_id=job_id,
            attempt_id=attempt_id,
            fencing_token=token,
        )

    def _cancel(self, msg: agent_pb2.CancelAttempt) -> None:
        attempt = self._attempts.get(msg.attempt_id)
        if (
            attempt is None
            or attempt.token != msg.fencing_token
            or (attempt.ended and not msg.release)
        ):
            log.info(
                "agent.execution.cancel_ignored",
                node_id=self._node_id,
                attempt_id=msg.attempt_id[:40] or None,
                known=attempt is not None,
                release=msg.release,
            )
            return
        log.info(
            "agent.execution.cancel",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            reason=msg.reason[:64] or None,
            release=msg.release,
        )
        if msg.release:
            attempt.release_requested = True
            if attempt.ended:
                self._release(attempt, cause="released")
                return
        self._stop(attempt, "cancelled")

    def _refresh_token(self, msg: agent_pb2.RefreshFetchToken) -> None:
        """D95: a fresh fetch token for a held attempt (after a controller
        restart). Applied only to that (attempt_id, fencing_token) while it has
        not ended; a staging waiting for it retries at once."""
        attempt = self._attempts.get(msg.attempt_id)
        applied = (
            attempt is not None
            and attempt.token == msg.fencing_token
            and not attempt.ended
            and token_ok(msg.fetch_token)
            and 0 < msg.blob_port < 65536
            and blob_url_ok(msg.blob_url)
        )
        log.info(
            "agent.execution.fetch_token_refreshed"
            if applied
            else "agent.execution.fetch_token_refresh_ignored",
            node_id=self._node_id,
            attempt_id=msg.attempt_id[:40] or None,
            known=attempt is not None,
        )
        if attempt is None or not applied:
            return
        attempt.fetch_token = msg.fetch_token
        attempt.blob_url = msg.blob_url
        attempt.blob_port = int(msg.blob_port)
        if msg.sources:
            # Validated like StartAttempt's sources when staging uses them.
            attempt.sources = list(msg.sources)
        if msg.inputs:
            attempt.inputs = list(msg.inputs)
        attempt.token_changed.set()

    def _refresh_grant(self, msg: agent_pb2.RefreshUploadGrant) -> None:
        """D114: a fresh upload grant for a held attempt (after a controller
        restart). Applied only to that (attempt_id, fencing_token) while it has
        not ended; an upload waiting for it retries at once."""
        attempt = self._attempts.get(msg.attempt_id)
        applied = (
            attempt is not None
            and attempt.token == msg.fencing_token
            and not attempt.ended
            and token_ok(msg.upload_token)
            and 0 < msg.blob_port < 65536
            and blob_url_ok(msg.blob_url)
        )
        log.info(
            "agent.execution.upload_grant_refreshed"
            if applied
            else "agent.execution.upload_grant_refresh_ignored",
            node_id=self._node_id,
            attempt_id=msg.attempt_id[:40] or None,
            known=attempt is not None,
        )
        if attempt is None or not applied:
            return
        attempt.upload_token = msg.upload_token
        attempt.upload_url = msg.blob_url
        attempt.upload_port = int(msg.blob_port)
        attempt.grant_changed.set()

    async def _upload_parts(
        self, base: str, digest: Digest, token: str, path: Path, *, attempt: _Attempt
    ) -> int:
        return await upload_parts(
            base,
            digest,
            token,
            path,
            part_bytes=self._cfg.upload_part_bytes,
            progress=attempt.uploads,
        )

    async def _upload_retrying(
        self, attempt: _Attempt, plan: AttemptPlan, digest: Digest, path: Path
    ) -> None:
        """One blob up with the attempt's grant (D114): a refused grant waits
        for ``RefreshUploadGrant``, an unreachable controller is retried with
        backoff (0.25 s doubling to 5 s) - parts already stored are not sent
        again - for at most ``upload_retry_s`` counted from the attempt's first
        failed upload (one budget for its logs and outputs together), then
        :class:`UploadError` (retryable -> ``output_error``). The controller's
        upload deadline (D94) still bounds the whole upload."""
        if not attempt.upload_token:
            attempt.upload_token = plan.upload_token
        delay = _STAGE_RETRY_BASE_S
        while True:
            used = attempt.upload_token
            attempt.grant_changed.clear()
            refused = False
            try:
                base = blob_base_url(
                    plan.blob_url if attempt.upload_url is None else attempt.upload_url,
                    self._controller_host(),
                    plan.blob_port
                    if attempt.upload_port is None
                    else attempt.upload_port,
                )
                kwargs = {"attempt": attempt} if self._resumable else {}
                await self._step(
                    attempt, self._upload(base, digest, used, path, **kwargs)
                )
            except FetchError as exc:  # no host to upload to
                last = UploadError(exc.message, retryable=True)
            except UploadError as exc:
                if not exc.retryable or self._cfg.upload_retry_s <= 0:
                    raise
                last, refused = exc, bool(exc.details.get("refused"))
            else:
                return
            if attempt.upload_deadline is None:
                attempt.upload_deadline = self._clock() + self._cfg.upload_retry_s
            deadline = attempt.upload_deadline
            self._check_upload_deadline(deadline, last)
            if attempt.upload_token == used:
                remaining = max(0.0, deadline - self._clock())
                wait = remaining if refused else min(delay, remaining)
                delay = min(delay * 2, _STAGE_RETRY_CAP_S)
                await self._step(attempt, self._grant_wait(attempt, wait))
                if attempt.upload_token == used:
                    # Checked again after the wait: no refresh came in time.
                    self._check_upload_deadline(deadline, last)
            attempt.upload_retries += 1
            log.info(
                "agent.execution.upload_retry",
                node_id=self._node_id,
                job_id=attempt.job_id,
                attempt_id=attempt.attempt_id,
                cause=last.message[:120],
                refused=refused,
                refreshed=attempt.upload_token != used,
                retries=attempt.upload_retries,
            )

    def _check_upload_deadline(self, deadline: float, last: UploadError) -> None:
        if self._clock() >= deadline:
            raise UploadError(
                f"the upload did not succeed within {self._cfg.upload_retry_s:g} s "
                f"of retries (last: {last.message})",
                retryable=True,
            )

    @staticmethod
    async def _grant_wait(attempt: _Attempt, wait_s: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(attempt.grant_changed.wait(), wait_s)

    def _stop(self, attempt: _Attempt, reason: str) -> None:
        if attempt.stop_reason is None:
            attempt.stop_reason = reason
        if attempt.step is not None and not attempt.step.done():
            attempt.step.cancel()

    def _task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.error(
                "agent.execution.task_failed",
                node_id=self._node_id,
                task=task.get_name(),
                cause=type(exc).__name__,
            )

    # ------------------------------------------------------------ one attempt

    async def _refuse(
        self,
        attempt: _Attempt,
        failure_type: FailureType,
        detail: str,
        *,
        retryable: bool = False,
    ) -> None:
        await self._send(
            attempt,
            AttemptState.FAILED,
            failure=(failure_type, detail, retryable),
        )

    async def _lifecycle(self, attempt: _Attempt, msg: agent_pb2.StartAttempt) -> None:
        leases = attempt.leases = AttemptLeases(
            self.gc.index if self.gc is not None else None, attempt.attempt_id
        )
        try:
            with leases.current():
                await self._run(attempt, msg)
        except _StoppedError:
            await self._stopped(attempt, exit_code=None, signal=None)
        except (PlanError, FetchError, BundleError, LeaseError) as exc:
            await self._send(
                attempt,
                AttemptState.FAILED,
                failure=(
                    exc.failure_type,
                    exc.message,
                    exc.failure_type in _RETRYABLE,
                ),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            full = disk_full_cause(exc)
            if full is not None:
                # D116: ENOSPC anywhere (workdir, logs, staging) is disk_full.
                log.warning(
                    "agent.execution.disk_full",
                    node_id=self._node_id,
                    job_id=attempt.job_id,
                    attempt_id=attempt.attempt_id,
                    state=attempt.state.value,
                )
                await self._send(
                    attempt,
                    AttemptState.FAILED,
                    failure=(
                        FailureType.DISK_FULL,
                        f"no disk space on this node ({full.strerror})",
                        False,
                    ),
                )
                return
            log.exception(
                "agent.execution.attempt_crashed",
                node_id=self._node_id,
                attempt_id=attempt.attempt_id,
            )
            await self._send(
                attempt,
                AttemptState.FAILED,
                failure=(FailureType.INTERNAL_ERROR, "the executor failed", False),
            )
        finally:
            await leases.release()

    async def _step[T](self, attempt: _Attempt, awaitable: Awaitable[T]) -> T:
        """Run one cancellable step; a purposeful stop becomes :class:`_StoppedError`."""
        if attempt.stop_reason is not None:
            raise _StoppedError
        step: asyncio.Future[T] = asyncio.ensure_future(awaitable)
        attempt.step = step
        try:
            return await step
        except asyncio.CancelledError:
            if step.cancelled() and attempt.stop_reason is not None:
                raise _StoppedError from None
            raise
        finally:
            attempt.step = None

    async def _run(self, attempt: _Attempt, msg: agent_pb2.StartAttempt) -> None:
        workdir = self.work_root / attempt.attempt_id
        attempt.workdir = workdir
        plan = plan_attempt(
            msg,
            settings=self._cfg,
            offer=self._setup.offer,
            declared_gpus=self._declared_gpus,
            workdir=workdir,
        )
        attempt.runtime = plan.runtime
        await self._send(attempt, AttemptState.STAGING)
        stage_started = self._clock()
        await self._stage(attempt, plan, workdir, msg.spec)
        attempt.phases["stage_s"] = self._clock() - stage_started
        logs = await asyncio.to_thread(
            AttemptLogs,
            workdir / ".meshforge",
            max_bytes=self._cfg.max_log_bytes,
            clock=self._clock,
        )
        try:
            await self._execute(attempt, plan, logs)
        finally:
            logs.close()

    async def _stage(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        workdir: Path,
        spec: agent_pb2.ExecutionSpec,
    ) -> None:
        await asyncio.to_thread(self._prepare_work_root_only)
        if not attempt.fetch_token:
            attempt.fetch_token = plan.fetch_token
        deadline = self._clock() + self._cfg.staging_retry_s
        delay = _STAGE_RETRY_BASE_S
        while True:
            used = attempt.fetch_token
            attempt.token_changed.clear()
            try:
                await self._stage_once(attempt, plan, workdir, spec, used)
            except FetchError as exc:
                if (
                    exc.failure_type not in _STAGE_RETRYABLE
                    or self._cfg.staging_retry_s <= 0
                ):
                    raise
                last = exc
            else:
                return
            self._check_stage_deadline(deadline, last)
            # D95: a controller restart refuses the old token (401) or is not
            # reachable for a while; the controller re-mints after Reconcile.
            # Only the CONTROLLER's refusal waits for that refresh: a peer's
            # 401/403 never reaches here (the fetcher drops the peer, V0.5 G4).
            if attempt.fetch_token == used:
                remaining = deadline - self._clock()
                wait = remaining if _controller_refused(last) else min(delay, remaining)
                delay = min(delay * 2, _STAGE_RETRY_CAP_S)
                await self._step(attempt, self._token_wait(attempt, wait))
            # Checked before EVERY retry, a refreshed token included.
            self._check_stage_deadline(deadline, last)
            attempt.stage_retries += 1
            log.info(
                "agent.execution.stage_retry",
                node_id=self._node_id,
                job_id=attempt.job_id,
                attempt_id=attempt.attempt_id,
                cause=last.failure_type.value,
                source=last.source or None,
                refreshed=attempt.fetch_token != used,
                retries=attempt.stage_retries,
            )

    def _check_stage_deadline(self, deadline: float, last: FetchError) -> None:
        if self._clock() >= deadline:
            raise FetchError(
                f"staging did not succeed within {self._cfg.staging_retry_s:g} s "
                f"of retries (last: {last.message})",
                failure_type=FailureType.TRANSFER_ERROR,
            )

    @staticmethod
    async def _token_wait(attempt: _Attempt, wait_s: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(attempt.token_changed.wait(), wait_s)

    async def _stage_once(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        workdir: Path,
        spec: agent_pb2.ExecutionSpec,
        token: str,
    ) -> None:
        """One staging try with ``token`` (a tree resumes what it verified)."""
        if plan.tree_id is not None:
            await self._stage_tree(attempt, workdir, _refreshed(spec, attempt, token))
            return
        if plan.digest is None:  # pragma: no cover - plan_attempt guarantees one
            raise FetchError("nothing to fetch", failure_type=FailureType.INVALID_INPUT)
        bundle = self.work_root / f"{attempt.attempt_id}.bundle"
        base = blob_base_url(
            plan.blob_url if attempt.blob_url is None else attempt.blob_url,
            self._controller_host(),
            plan.blob_port if attempt.blob_port is None else attempt.blob_port,
        )
        try:
            await self._step(
                attempt,
                self._fetch(base, plan.digest, token, bundle),
            )
            # Unpacking is not interruptible: it runs to completion in its thread
            # (its caps bound how long) and a stop asked meanwhile is honoured
            # right after - the workdir is never deleted under a running unpack.
            await asyncio.to_thread(unpack, bundle, workdir, limits=BundleLimits())
            if attempt.stop_reason is not None:
                raise _StoppedError
        finally:
            with contextlib.suppress(OSError):
                bundle.unlink()
        if os.name == "posix":
            await asyncio.to_thread(workdir.chmod, 0o700)

    async def _stage_tree(
        self, attempt: _Attempt, workdir: Path, spec: agent_pb2.ExecutionSpec
    ) -> None:
        """V0.5 D84: fetch into the CAS, then copy into the workdir (verified).

        The copy is not interruptible (like an unpack: it runs to completion in
        its thread); a stop asked meanwhile is honoured right after. A staging
        retry (D95) skips the trees already in the workdir (V0.5 G5: the
        project, then each named input in order).
        """
        if attempt.stages:
            await self._stage_inputs(attempt, workdir, spec)
            return
        sources = tree_sources(spec, self._controller_host())
        leases = attempt.leases
        tree_hex = spec.tree_id.removeprefix("sha256:")
        if leases is not None:
            # Held BEFORE the staging looks at the cache (V0.5 G5, D91).
            await leases.add("tree", tree_hex)
        report = await self._step(
            attempt,
            stage_tree(
                self.cas,
                tree_id=spec.tree_id,
                sources=sources,
                dest=workdir,
                timeout_s=self._fetch_cfg.request_timeout_s,
                tuning=FetchTuning(
                    per_source=self._fetch_cfg.per_source,
                    slow_source_factor=self._fetch_cfg.slow_source_factor,
                    slow_min_units=self._fetch_cfg.slow_min_units,
                    endgame_after_s=self._fetch_cfg.endgame_after_s,
                ),
                on_tree=leases.tree if leases is not None else None,
            ),
        )
        self.last_stage = report
        if leases is not None:
            await leases.touch_tree(tree_hex)
        if self.gc is not None:
            self.gc.request("staged")
        log.info(
            "agent.cas.staged",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            tree_id=spec.tree_id,
            blobs_hit=report.blobs_hit,
            blobs_fetched=report.blobs_fetched,
            bytes_fetched=report.bytes_fetched,
            chunks_resumed=report.chunks_resumed,
            duration_s=round(report.duration_s, 3),
            # Per source (V0.5 D89): bytes received, units won, why dropped.
            sources={
                name: {
                    "bytes": src.bytes,
                    "units": src.units,
                    "dropped": src.dropped,
                }
                for name, src in report.sources.items()
            },
        )
        if attempt.stop_reason is not None:
            raise _StoppedError
        attempt.stages = [report]
        await self._stage_inputs(attempt, workdir, spec)
        if os.name == "posix":
            await asyncio.to_thread(workdir.chmod, 0o700)

    async def _stage_inputs(
        self, attempt: _Attempt, workdir: Path, spec: agent_pb2.ExecutionSpec
    ) -> None:
        """V0.5 G5 (D92): each named input tree is fetched like the project
        (cache, peers, verified chunks) and copied to ``workdir/<mount>``, then
        made read-only. A mount that meets anything the project put there - a
        file, a link, or a folder - is refused (``invalid_input``): an input
        never hides or mixes with project files."""
        if not spec.inputs:
            return
        leases = attempt.leases
        done = max(0, len(attempt.stages) - 1)  # stages[0] is the project
        for item in list(spec.inputs)[done:]:
            mount = check_mount(item.mount)
            dest = await asyncio.to_thread(_free_mount, workdir, mount)
            tree_hex = item.tree_id.removeprefix("sha256:")
            if leases is not None:
                await leases.add("tree", tree_hex)
            report = await self._step(
                attempt,
                stage_tree(
                    self.cas,
                    tree_id=item.tree_id,
                    sources=tree_sources(
                        spec, self._controller_host(), sources=item.sources
                    ),
                    dest=dest,
                    timeout_s=self._fetch_cfg.request_timeout_s,
                    tuning=self._tuning(),
                    on_tree=leases.tree if leases is not None else None,
                ),
            )
            if leases is not None:
                await leases.touch_tree(tree_hex)
            await asyncio.to_thread(make_read_only, dest)
            attempt.stages.append(report)
            log.info(
                "agent.cas.input_staged",
                node_id=self._node_id,
                job_id=attempt.job_id,
                attempt_id=attempt.attempt_id,
                tree_id=item.tree_id,
                ref=item.ref,
                mount=mount,
                blobs_hit=report.blobs_hit,
                blobs_fetched=report.blobs_fetched,
                bytes_fetched=report.bytes_fetched,
            )
            if attempt.stop_reason is not None:
                raise _StoppedError

    def _tuning(self) -> FetchTuning:
        return FetchTuning(
            per_source=self._fetch_cfg.per_source,
            slow_source_factor=self._fetch_cfg.slow_source_factor,
            slow_min_units=self._fetch_cfg.slow_min_units,
            endgame_after_s=self._fetch_cfg.endgame_after_s,
        )

    def _prepare_work_root_only(self) -> None:
        self.work_root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self.work_root.chmod(0o700)

    async def _execute(
        self, attempt: _Attempt, plan: AttemptPlan, logs: AttemptLogs
    ) -> None:
        runtime = self._setup.runtimes[plan.runtime]
        observer = _Observer(self._clock)
        wake = asyncio.Event()
        stderr_tail = OutputTail(STDERR_TAIL_BYTES)

        async def on_output(stream: Stream, data: bytes) -> None:
            if stream == Stream.STDERR:
                stderr_tail.add(data)  # D116: classification evidence
            pending = logs.batcher.next_deadline() is not None
            for chunk in logs.write(Stream(stream), data):
                self._send_log(attempt, chunk)
            if not pending:
                wake.set()

        flusher = asyncio.create_task(
            self._flush_logs(attempt, logs, wake),
            name=f"attempt-logs:{attempt.attempt_id}",
        )
        flusher.add_done_callback(self._task_done)
        try:
            await self._run_workload(
                attempt, plan, logs, runtime, observer, on_output, stderr_tail
            )
        finally:
            flusher.cancel()
            await asyncio.gather(flusher, return_exceptions=True)

    async def _flush_logs(
        self, attempt: _Attempt, logs: AttemptLogs, wake: asyncio.Event
    ) -> None:
        """Send a batch once its oldest byte waited 100 ms (D71)."""
        while True:
            deadline = logs.batcher.next_deadline()
            if deadline is None:
                wake.clear()
                await wake.wait()
                continue
            await asyncio.sleep(max(0.0, deadline - self._clock()))
            for chunk in logs.batcher.due():
                self._send_log(attempt, chunk)

    def _send_log(self, attempt: _Attempt, chunk: Chunk) -> None:
        msg = agent_pb2.LogChunk(
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            stream=agent_pb2.LogChunk.STREAM_STDOUT
            if chunk.stream is Stream.STDOUT
            else agent_pb2.LogChunk.STREAM_STDERR,
            offset=chunk.offset,
            data=chunk.data,
            fencing_token=attempt.token,
        )
        if self._log_sink is None or not self._log_sink(msg):
            self.log_chunks_dropped += 1

    async def _final_logs(
        self, attempt: _Attempt, plan: AttemptPlan, logs: AttemptLogs
    ) -> dict[Stream, agent_pb2.FinalLog]:
        """Upload both local logs with the grant (best effort, D71)."""
        result: dict[Stream, agent_pb2.FinalLog] = {}
        for stream in Stream:
            final = agent_pb2.FinalLog(
                bytes=logs.kept[stream], truncated=logs.truncated[stream]
            )
            result[stream] = final
            path = logs.path(stream)
            if not plan.upload_token:
                continue
            try:
                digest, size = await asyncio.to_thread(file_sha256, path)
                if size == 0:
                    continue
                await self._upload_retrying(attempt, plan, Digest.parse(digest), path)
            except (UploadError, FetchError, OSError) as exc:
                log.warning(
                    "agent.execution.log_upload_failed",
                    node_id=self._node_id,
                    job_id=attempt.job_id,
                    attempt_id=attempt.attempt_id,
                    stream=stream.value,
                    cause=type(exc).__name__,
                )
                continue
            final.digest = digest
        return result

    async def _run_workload(
        self,
        attempt: _Attempt,
        plan: AttemptPlan,
        logs: AttemptLogs,
        runtime: Any,
        observer: _Observer,
        on_output: Callable[[Stream, bytes], Awaitable[None]],
        stderr_tail: OutputTail | None = None,
    ) -> None:
        run: asyncio.Task[RunResult] = asyncio.ensure_future(
            runtime.run(plan.spec, on_output, observer=observer)
        )
        attempt.step = run
        started = asyncio.ensure_future(observer.started.wait())
        try:
            await asyncio.wait({run, started}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            started.cancel()
        if observer.started.is_set():
            await self._announce_run(attempt)
        try:
            result = await run
        except asyncio.CancelledError:
            if not (run.cancelled() and attempt.stop_reason is not None):
                raise
            result = observer.cancelled_result()
        finally:
            attempt.step = None
        if stderr_tail is not None:
            result = refine_result(result, stderr_tail.data)  # D116
        attempt.env_hash = result.env_hash or attempt.env_hash
        exited = self._clock()
        for chunk in logs.batcher.flush():
            self._send_log(attempt, chunk)
        if observer.run_started is not None:
            await self._announce_run(attempt)
        if observer.env_started is not None:
            attempt.phases["env_s"] = observer.env_s
        if observer.run_started is not None:
            attempt.phases["run_s"] = observer.run_s
        if result.cancelled:
            await self._stopped(
                attempt, exit_code=result.exit_code, signal=result.signal
            )
            return
        if result.ok:
            await self._send(attempt, AttemptState.UPLOADING)
        final_logs = await self._final_logs(attempt, plan, logs)
        if result.ok:
            try:
                collected = await self._collect(attempt, plan)
            except (OutputError, UploadError) as exc:
                attempt.phases["collect_s"] = self._clock() - exited
                await self._send(
                    attempt,
                    AttemptState.FAILED,
                    # output_error, or disk_full when packing hit ENOSPC (D116)
                    failure=(exc.failure_type, exc.message, exc.retryable),
                    exit_code=result.exit_code,
                    logs=final_logs,
                )
                return
            attempt.phases["collect_s"] = self._clock() - exited
            await self._send(
                attempt,
                AttemptState.DONE,
                exit_code=result.exit_code,
                output_digest=str(collected.digest) if collected.digest else "",
                missing=collected.missing,
                logs=final_logs,
            )
            return
        attempt.phases["collect_s"] = self._clock() - exited
        failure = result.failure_type or FailureType.INTERNAL_ERROR
        await self._send(
            attempt,
            AttemptState.FAILED,
            failure=(failure, _failure_text(result), failure in _RETRYABLE),
            exit_code=result.exit_code,
            signal=result.signal,
            logs=final_logs,
        )

    async def _collect(self, attempt: _Attempt, plan: AttemptPlan) -> Collected:
        """D74: expand, pack and upload the declared outputs (cancellable)."""
        patterns = tuple(plan.manifest.outputs)
        if not patterns or attempt.workdir is None:
            return Collected(None, None, 0, 0, ())
        dest = self.work_root / f"{attempt.attempt_id}.outputs"
        max_bytes = plan.upload_max_bytes or 0
        try:
            collected = await self._step(
                attempt,
                asyncio.to_thread(
                    collect_outputs,
                    attempt.workdir,
                    patterns,
                    dest,
                    max_bytes=max_bytes if max_bytes > 0 else 2**62,
                ),
            )
            if collected.archive is None or collected.digest is None:
                return collected
            if not plan.upload_token:
                raise OutputError(
                    "the controller gave this attempt no upload grant",
                    retryable=False,
                )
            started = self._clock()
            await self._upload_retrying(attempt, plan, collected.digest, dest)
            log.info(
                "agent.execution.outputs_uploaded",
                node_id=self._node_id,
                job_id=attempt.job_id,
                attempt_id=attempt.attempt_id,
                files=collected.files,
                bytes=collected.compressed_bytes,
                upload_s=round(self._clock() - started, 3),
                missing=len(collected.missing),
            )
            return collected
        finally:
            with contextlib.suppress(OSError):
                dest.unlink()

    async def _announce_run(self, attempt: _Attempt) -> None:
        """``ENV_READY`` then ``RUNNING``, once (the env phase is inside run())."""
        if attempt.state is AttemptState.STAGING:
            await self._send(attempt, AttemptState.ENV_READY)
        if attempt.state is AttemptState.ENV_READY:
            await self._send(attempt, AttemptState.RUNNING)

    async def _stopped(
        self, attempt: _Attempt, *, exit_code: int | None, signal: int | None
    ) -> None:
        if attempt.stop_reason == "agent_shutdown":
            failure = (FailureType.NODE_LOST, "agent_shutdown", True)
        else:
            failure = (FailureType.CANCELLED, "cancelled", False)
        await self._send(
            attempt,
            AttemptState.FAILED,
            failure=failure,
            exit_code=exit_code,
            signal=signal,
        )

    # ------------------------------------------------------------ reporting

    async def _send(
        self,
        attempt: _Attempt,
        state: AttemptState,
        *,
        failure: tuple[FailureType, str, bool] | None = None,
        exit_code: int | None = None,
        signal: int | None = None,
        output_digest: str = "",
        missing: Sequence[str] = (),
        logs: Mapping[Stream, agent_pb2.FinalLog] | None = None,
    ) -> None:
        status = agent_pb2.AttemptStatus(
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.token,
            state=attempt_state_to_proto(state),
            runtime=attempt.runtime,
            output_digest=output_digest,
            missing_outputs=[m[:_MISSING_MAX_CHARS] for m in missing[:_MISSING_MAX]],
        )
        status.observed_at.FromDatetime(datetime.now(UTC))
        if failure is not None:
            failure_type, detail, retryable = failure
            status.failure.CopyFrom(
                common_pb2.Failure(
                    failure_type=failure_type_to_proto(failure_type),
                    message=_bounded(detail),
                    retryable=retryable,
                )
            )
        if exit_code is not None:
            status.exit_code = exit_code
        if logs:
            status.stdout_log.CopyFrom(logs[Stream.STDOUT])
            status.stderr_log.CopyFrom(logs[Stream.STDERR])
        if signal is not None and 1 <= signal <= 64:
            status.signal = signal
        terminal = state in (AttemptState.DONE, AttemptState.FAILED)
        if terminal:
            for name, value in attempt.phases.items():
                setattr(status.phases, name, max(0.0, value))
            # V0.5 G5 (D93): what staging each tree cost (project, then inputs).
            status.transfers.extend(
                stage.to_proto() for stage in attempt.stages[: 1 + MAX_INPUTS]
            )
            status.env_hash = attempt.env_hash  # D116 (empty = none computed)
        attempt.state = state
        if terminal:
            attempt.terminal_status = status
            attempt.terminal_sent_at = self._clock()
            self._ensure_resend_loop()
        message_id = await self._report("attempt_status", status)
        if terminal:
            attempt.terminal_message_id = message_id
            self._by_terminal_message[message_id] = attempt.attempt_id
            if attempt.release_requested:
                self._release(attempt, cause="released")
        log.info(
            "agent.execution.status",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.token,
            state=state.value,
            failure_type=None if failure is None else failure[0].value,
            exit_code=exit_code,
        )


def _failure_text(result: RunResult) -> str:
    return f"{result.reason}: {result.detail}" if result.reason else result.detail


def _bounded(text: str) -> str:
    cleaned = "".join(ch if ch.isprintable() else "?" for ch in text)
    return cleaned[:1024]


def _controller_refused(error: FetchError) -> bool:
    """A 401/403 from the controller (a bundle fetch names no source: it is
    always the controller). Only this waits for RefreshFetchToken (D95)."""
    return error.failure_type is FailureType.UNAUTHORIZED and error.source in (
        "",
        "controller",
    )


def _refreshed(
    spec: agent_pb2.ExecutionSpec, attempt: _Attempt, token: str
) -> agent_pb2.ExecutionSpec:
    """``spec`` with the controller's fetch token and blob location replaced by
    a RefreshFetchToken's (D95): the spec's own fields and every controller
    source (``node_id`` empty); the source list itself when the refresh carried
    one (V0.5 G4, D98)."""
    if token == spec.fetch_token and attempt.blob_port is None:
        return spec
    copy = agent_pb2.ExecutionSpec()
    copy.CopyFrom(spec)
    copy.fetch_token = token
    if attempt.blob_port is not None:
        copy.blob_port = attempt.blob_port
        copy.blob_url = attempt.blob_url or ""
    if attempt.sources is not None:
        # V0.5 G4 (D98): the refresh re-minted the whole list (fresh peer tokens).
        del copy.sources[:]
        copy.sources.extend(attempt.sources)
    if attempt.inputs is not None:
        # V0.5 G5: the inputs with their sources re-minted too.
        del copy.inputs[:]
        copy.inputs.extend(attempt.inputs)
    listed = [*copy.sources, *(s for i in copy.inputs for s in i.sources)]
    for source in listed:
        if not source.node_id:
            source.token = token
            if attempt.blob_port is not None:
                source.url = attempt.blob_url or ""
    return copy


def _free_mount(workdir: Path, mount: str) -> Path:
    """``workdir/mount`` after checking that nothing the project staged is in
    the way: every parent is absent or a real folder (never a link), the
    mount itself does not exist. Creates the parents. Blocking."""
    current = workdir
    parts = mount.split("/")
    for part in parts[:-1]:
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise FetchError(
                f"input mount {mount!r} goes through a project file or link",
                failure_type=FailureType.INVALID_INPUT,
            )
    dest = workdir / mount
    if os.path.lexists(dest):
        raise FetchError(
            f"input mount {mount!r} overlaps project files",
            failure_type=FailureType.INVALID_INPUT,
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _remove_quietly(path: Path) -> None:
    with contextlib.suppress(OSError):
        remove_tree(path)
