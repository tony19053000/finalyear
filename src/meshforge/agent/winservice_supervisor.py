"""Supervise the agent child for the Windows service host (V0.17 D386, SP1).

Synchronous on purpose: the service host has no event loop, and the SCM calls
``SvcStop`` from its own thread. Cross-platform so the stop protocol is tested with
real processes on Linux as well (there the process group plays the job object).

One run = spawn -> wait (child exit or stop) -> graceful stop (close stdin, wait up
to ``grace_s``) -> kill the whole tree -> :class:`ExitDecision`. The tree is
**always** swept after the child exits, even after a graceful exit, so nothing a
job started can outlive the service.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Final

from meshforge.agent.process import _close_job, _job_for, _taskkill, _terminate_job
from meshforge.agent.winservice import ExitDecision, StopKind, classify_exit
from meshforge.common.logging import get_logger

__all__ = ["Supervisor"]

log = get_logger(__name__)

_POLL_S: Final = 0.5
_KILL_WAIT_S: Final = 10.0


class Supervisor:
    """Run ``argv`` once under supervision; :meth:`request_stop` is thread-safe."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        log_path: Path,
        grace_s: float,
        env: Mapping[str, str] | None = None,
        cwd: Path | None = None,
        on_progress: Callable[[], None] | None = None,
    ) -> None:
        self._argv = list(argv)
        self._log_path = log_path
        self._grace_s = grace_s
        self._env = dict(env) if env is not None else None
        self._cwd = cwd
        self._on_progress = on_progress
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.child_pid: int | None = None
        self.uses_job_object = False

    def request_stop(self, grace_s: float | None = None) -> None:
        with self._lock:
            if grace_s is not None:
                self._grace_s = min(self._grace_s, grace_s)
        if not self._stop.is_set():
            log.info("agent.service.stop_requested", child_pid=self.child_pid)
            self._stop.set()

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------ run
    def run(self) -> ExitDecision:
        started = time.monotonic()
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("ab") as out:
            try:
                proc = self._spawn(out)
            except OSError as exc:
                log.error(  # noqa: TRY400 - typed outcome, the message says it all
                    "agent.service.spawn_failed",
                    argv0=self._argv[0],
                    error=str(exc),
                    failure_type="env_error",
                )
                return ExitDecision(
                    StopKind.RESTART, "spawn_failed", process_exit_code=1
                )
            job = _job_for(proc.pid) if sys.platform == "win32" else None
            self.uses_job_object = job is not None
            log.info(
                "agent.service.child_started",
                child_pid=proc.pid,
                job_object=self.uses_job_object,
                spawn_s=round(time.monotonic() - started, 3),
            )
            if sys.platform == "win32" and job is None:
                log.warning("agent.service.job_object_unavailable", child_pid=proc.pid)
            try:
                return self._supervise(proc, job, started)
            finally:
                if job is not None:
                    _close_job(job)

    def _spawn(self, out: IO[bytes]) -> subprocess.Popen[bytes]:
        flags = 0
        if sys.platform == "win32":
            flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        proc = subprocess.Popen(  # noqa: S603 - fixed argv from the service config
            self._argv,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=self._env,
            cwd=self._cwd,
            creationflags=flags,
            start_new_session=sys.platform != "win32",
        )
        self.child_pid = proc.pid
        return proc

    def _supervise(
        self, proc: subprocess.Popen[bytes], job: int | None, started: float
    ) -> ExitDecision:
        while proc.poll() is None:
            if self._stop.wait(_POLL_S):
                break
        stop_at = time.monotonic()
        killed = False
        if proc.poll() is None:  # stop requested while the child runs
            with contextlib.suppress(OSError):
                if proc.stdin is not None:
                    proc.stdin.close()
            with self._lock:
                deadline = stop_at + self._grace_s
            while proc.poll() is None and time.monotonic() < deadline:
                if self._on_progress is not None:
                    self._on_progress()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    proc.wait(timeout=min(1.0, max(0.0, deadline - time.monotonic())))
            if proc.poll() is None:
                killed = True
                log.warning(
                    "agent.service.grace_expired",
                    child_pid=proc.pid,
                    grace_s=self._grace_s,
                )
        else:
            with contextlib.suppress(OSError):
                if proc.stdin is not None:
                    proc.stdin.close()
        self._kill_tree(proc, job)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_KILL_WAIT_S)
        returncode = proc.returncode if proc.returncode is not None else -1
        decision = classify_exit(returncode, stop_requested=self._stop.is_set())
        log.info(
            "agent.service.child_exited",
            child_pid=proc.pid,
            exit_code=returncode,
            stop_requested=self._stop.is_set(),
            killed=killed,
            decision=decision.kind.value,
            reason=decision.reason,
            stop_s=round(time.monotonic() - stop_at, 3),
            run_s=round(time.monotonic() - started, 3),
        )
        return decision

    def _kill_tree(self, proc: subprocess.Popen[bytes], job: int | None) -> None:
        """Every descendant, whether the child is still alive or not."""
        if sys.platform == "win32":
            if job is not None:
                _terminate_job(job)
            elif proc.poll() is None:
                _taskkill(proc.pid)  # fallback: walks the tree from the live parent
            with contextlib.suppress(OSError):
                proc.kill()
        else:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
