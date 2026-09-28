"""The agent's attempt executor (V0.4 G4, D45/D52/D67).

One :class:`Executor` per agent process. The session client hands it every new
``StartAttempt`` / ``CancelAttempt`` **synchronously, before the Ack** (so an Ack
means "held", D66); each accepted attempt then runs as **one task** that owns it
from start to its terminal report:

``ASSIGNED`` (the command) -> ``STAGING`` (fetch the bundle with the attempt's
fetch token, verify its digest, unpack it hostile-input-safe into
``<work root>/<attempt_id>``) -> ``ENV_READY`` + ``RUNNING`` (reported when the
runtime starts the workload - image pull and requirements install happen before,
in the runtime's env phase) -> ``UPLOADING`` (nothing to upload until G5) ->
``DONE`` or ``FAILED(failure_type)``.

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
* **Held until acked.** An attempt stays listed for ``Reconcile`` until its
  terminal report is acked; then its workdir is deleted (unless
  ``keep_workdirs``). Output bytes go to ``.meshforge/{stdout,stderr}.log`` in the
  workdir (LogChunk streaming is G5).

A failure anywhere is a typed ``failure_type`` in a ``FAILED`` report - never an
exception out of the task (a bug is reported ``internal_error`` and logged with
its traceback).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Final, Protocol

from meshforge.agent.execution.fetch import (
    FetchError,
    blob_base_url,
    fetch_bundle,
)
from meshforge.agent.execution.offer import ExecutionSetup
from meshforge.agent.execution.plan import AttemptPlan, PlanError, plan_attempt
from meshforge.agent.execution.runtime import RunObserver, RunResult, Stream
from meshforge.agent.settings import AgentSettings
from meshforge.common.bundle import BundleError, BundleLimits, unpack
from meshforge.common.errors import FailureType, InvalidIdError
from meshforge.common.ids import validate_attempt_id, validate_job_id
from meshforge.common.logging import get_logger
from meshforge.common.proto.meshforge.v1 import agent_pb2, common_pb2
from meshforge.common.proto_codec import attempt_state_to_proto, failure_type_to_proto
from meshforge.common.states import AttemptState

__all__ = ["MAX_LOG_BYTES", "Executor", "Reporter", "work_root_for"]

log = get_logger(__name__)

MAX_LOG_BYTES: Final = 512 * 1024 * 1024
"""Per stream: the local log file stops growing here (LogChunk streaming and its
own budget are G5)."""
_RECENT_FINISHED: Final = 4096
_SHUTDOWN_SLACK_S: Final = 3.0
_WORK_DIR_NAME: Final = "work"
_NOFOLLOW: Final = getattr(os, "O_NOFOLLOW", 0)
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
    workdir: Path | None = None
    phases: dict[str, float] = field(default_factory=dict)

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


class _Logs:
    """The two local log files of one attempt (bounded, never following links)."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(exist_ok=True)
        self._files: dict[Stream, IO[bytes]] = {}
        self._sizes = {Stream.STDOUT: 0, Stream.STDERR: 0}
        for stream in Stream:
            fd = os.open(
                directory / f"{stream.value}.log",
                os.O_WRONLY | os.O_CREAT | os.O_APPEND | _NOFOLLOW,
                0o600,
            )
            self._files[stream] = os.fdopen(fd, "ab", buffering=0)

    async def write(self, stream: Stream, data: bytes) -> None:
        size = self._sizes[stream]
        if size >= MAX_LOG_BYTES:
            return
        room = MAX_LOG_BYTES - size
        self._files[stream].write(data[:room])
        self._sizes[stream] = size + min(len(data), room)

    def close(self) -> None:
        for fh in self._files.values():
            with contextlib.suppress(OSError):
                fh.close()


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
    ) -> None:
        self._settings = settings
        self._cfg = settings.agent.execution
        self._setup = setup
        self._node_id = node_id
        self._report = report
        self._controller_host = controller_host
        self._declared_gpus = frozenset(declared_gpus)
        self._clock = clock
        self._fetch = fetch
        self.work_root = work_root_for(settings)
        self._attempts: dict[str, _Attempt] = {}
        self._finished: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._by_terminal_message: dict[str, str] = {}
        self._closing = False
        self._cleanup: set[asyncio.Task[None]] = set()

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
                shutil.rmtree(leftover, ignore_errors=True)
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
        attempt = self._attempts.pop(attempt_id, None)
        if attempt is None:
            return
        self._remember_finished(attempt)
        if attempt.workdir is not None and not self._cfg.keep_workdirs:
            task = asyncio.create_task(
                asyncio.to_thread(shutil.rmtree, attempt.workdir, True),
                name=f"attempt-cleanup:{attempt_id}",
            )
            self._cleanup.add(task)
            task.add_done_callback(self._cleanup.discard)

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
        if attempt is None or attempt.token != msg.fencing_token or attempt.ended:
            log.info(
                "agent.execution.cancel_ignored",
                node_id=self._node_id,
                attempt_id=msg.attempt_id[:40] or None,
                known=attempt is not None,
            )
            return
        log.info(
            "agent.execution.cancel",
            node_id=self._node_id,
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            reason=msg.reason[:64] or None,
        )
        self._stop(attempt, "cancelled")

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
        try:
            await self._run(attempt, msg)
        except _StoppedError:
            await self._stopped(attempt, exit_code=None, signal=None)
        except (PlanError, FetchError, BundleError) as exc:
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
        except Exception:
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
        await self._stage(attempt, plan, workdir)
        attempt.phases["stage_s"] = self._clock() - stage_started
        logs = await asyncio.to_thread(_Logs, workdir / ".meshforge")
        try:
            await self._execute(attempt, plan, logs)
        finally:
            logs.close()

    async def _stage(self, attempt: _Attempt, plan: AttemptPlan, workdir: Path) -> None:
        await asyncio.to_thread(self._prepare_work_root_only)
        bundle = self.work_root / f"{attempt.attempt_id}.bundle"
        base = blob_base_url(plan.blob_url, self._controller_host(), plan.blob_port)
        try:
            await self._step(
                attempt,
                self._fetch(base, plan.digest, plan.fetch_token, bundle),
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

    def _prepare_work_root_only(self) -> None:
        self.work_root.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            self.work_root.chmod(0o700)

    async def _execute(self, attempt: _Attempt, plan: AttemptPlan, logs: _Logs) -> None:
        runtime = self._setup.runtimes[plan.runtime]
        observer = _Observer(self._clock)
        run: asyncio.Task[RunResult] = asyncio.ensure_future(
            runtime.run(plan.spec, logs.write, observer=observer)
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
        exited = self._clock()
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
            attempt.phases["collect_s"] = self._clock() - exited
            await self._send(attempt, AttemptState.DONE, exit_code=result.exit_code)
            return
        attempt.phases["collect_s"] = self._clock() - exited
        failure = result.failure_type or FailureType.INTERNAL_ERROR
        await self._send(
            attempt,
            AttemptState.FAILED,
            failure=(failure, _failure_text(result), failure in _RETRYABLE),
            exit_code=result.exit_code,
            signal=result.signal,
        )

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
    ) -> None:
        status = agent_pb2.AttemptStatus(
            job_id=attempt.job_id,
            attempt_id=attempt.attempt_id,
            fencing_token=attempt.token,
            state=attempt_state_to_proto(state),
            runtime=attempt.runtime,
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
        if signal is not None and 1 <= signal <= 64:
            status.signal = signal
        terminal = state in (AttemptState.DONE, AttemptState.FAILED)
        if terminal:
            for name, value in attempt.phases.items():
                setattr(status.phases, name, max(0.0, value))
        attempt.state = state
        message_id = await self._report("attempt_status", status)
        if terminal:
            attempt.terminal_message_id = message_id
            self._by_terminal_message[message_id] = attempt.attempt_id
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
