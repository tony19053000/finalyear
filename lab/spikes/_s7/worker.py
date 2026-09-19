"""Parent side of the benchmark child protocol: bounded, never hangs, never leaks.

One :class:`Worker` owns one :mod:`spikes._s7.child` process. Every request has
its own timeout; a child that does not answer in time (a hung CUDA call, a
swapping machine) is killed **with its whole process tree** and the failure is
recorded as data, exactly like ``_inventory.runner`` does for one-shot commands.
Reading is done by a background thread and a queue because a blocking pipe read
cannot be given a timeout portably (``select`` does not work on Windows pipes).

A :class:`WorkerGroup` drives several identical children at the same time. That
is how the pure-Python CPU fallback measures *all cores*: the GIL makes threads
useless, so K processes run the same kernel simultaneously and the rates are
summed (per-worker numbers are kept in the sample).
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Any, Final

#: A bounded call against one worker (``worker.rep``, ``worker.setup``, ...).
type Call = Callable[[], dict[str, Any]]

CHILD_SCRIPT: Final = Path(__file__).resolve().parent / "child.py"
#: Thread environment handed to a child (BLAS reads these at import time).
THREAD_VARS: Final = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
    "VECLIB_MAXIMUM_THREADS",
)
_STDERR_LIMIT: Final = 4000
_EXIT_GRACE_S: Final = 5.0
_REAP_S: Final = 2.0


class WorkerError(Exception):
    """A child could not do what it was asked. ``fatal`` means it is gone."""

    def __init__(self, failure_type: str, message: str, *, fatal: bool = False) -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.fatal = fatal


def thread_env(threads: int) -> dict[str, str]:
    """Environment variables that pin a child's BLAS/OpenMP thread count."""
    if threads < 1:
        raise ValueError("threads must be >= 1")
    return {var: str(threads) for var in THREAD_VARS}


def kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill ``proc`` and every descendant (same technique as
    ``_inventory.runner._kill_tree``, which is private to the inventory)."""
    if sys.platform == "win32":
        taskkill = shutil.which("taskkill") or os.path.join(
            os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe"
        )
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                [taskkill, "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
        proc.wait(timeout=_REAP_S)


class Worker:
    """One benchmark child process."""

    def __init__(
        self,
        *,
        threads: int = 1,
        python: str | None = None,
        script: Path = CHILD_SCRIPT,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self.threads = threads
        self.python = python or sys.executable
        self.script = script
        self.extra_env = dict(extra_env or {})
        self._proc: subprocess.Popen[str] | None = None
        self._failed = False
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr: list[str] = []
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ lifecycle
    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def start(self) -> None:
        env = dict(os.environ)
        env.update(thread_env(self.threads))
        env.update(self.extra_env)
        env["PYTHONUNBUFFERED"] = "1"
        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        try:
            self._proc = subprocess.Popen(
                [self.python, str(self.script)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                **kwargs,
            )
        except (OSError, ValueError) as exc:
            raise WorkerError(
                "child_not_started", f"cannot start {self.script}: {exc}", fatal=True
            ) from exc
        self._threads = [
            threading.Thread(target=self._read_stdout, daemon=True),
            threading.Thread(target=self._read_stderr, daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None  # noqa: S101 - invariant: started before reading
        assert proc.stdout is not None  # noqa: S101 - stdout=PIPE
        try:
            for line in proc.stdout:
                self._lines.put(line)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)

    def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None  # noqa: S101 - invariant: started before reading
        assert proc.stderr is not None  # noqa: S101 - stderr=PIPE
        try:
            for line in proc.stderr:
                if sum(map(len, self._stderr)) < _STDERR_LIMIT:
                    self._stderr.append(line)
        except (OSError, ValueError):
            pass

    @property
    def stderr_tail(self) -> str:
        return "".join(self._stderr)[-_STDERR_LIMIT:].strip()

    def close(self) -> None:
        """Ask the child to exit, then make sure it is gone and its pipes closed."""
        proc = self._proc
        if proc is None:
            return
        self._proc = None
        if not self._failed and proc.poll() is None:
            with contextlib.suppress(OSError, ValueError):
                if proc.stdin is not None:
                    proc.stdin.write(json.dumps({"op": "bye"}) + "\n")
                    proc.stdin.flush()
                    proc.stdin.close()
            try:
                proc.wait(timeout=_EXIT_GRACE_S)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
        # The child is gone: let the readers drain what is left (a diagnostic
        # printed just before exit must reach ``stderr_tail``), then close.
        for thread in self._threads:
            thread.join(timeout=_REAP_S)
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError, ValueError):
                    pipe.close()

    def __enter__(self) -> Worker:
        self.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -------------------------------------------------------------------- requests
    def request(self, payload: Mapping[str, Any], timeout_s: float) -> dict[str, Any]:
        """Send one request and return the child's answer. Raises :class:`WorkerError`."""
        proc = self._proc
        if proc is None or proc.stdin is None or self._failed:
            raise WorkerError("child_gone", "worker is not running", fatal=True)
        try:
            proc.stdin.write(json.dumps(payload, allow_nan=False) + "\n")
            proc.stdin.flush()
        except (OSError, ValueError) as exc:
            self._die()
            raise WorkerError(
                "child_gone", f"cannot talk to the child: {exc}", fatal=True
            ) from exc
        try:
            line = self._lines.get(timeout=max(0.1, timeout_s))
        except queue.Empty:
            self._die()
            raise WorkerError(
                "worker_timeout",
                f"no answer to {payload.get('op')!r} within {timeout_s:g} s; "
                "the child was killed",
                fatal=True,
            ) from None
        if line is None:
            code = proc.poll()
            self._die()
            raise WorkerError(
                "child_crashed",
                f"the child exited (code {code}) while answering "
                f"{payload.get('op')!r}: {self.stderr_tail or 'no stderr output'}",
                fatal=True,
            )
        try:
            answer = json.loads(line)
        except ValueError as exc:
            raise WorkerError(
                "bad_answer", f"unreadable answer {line[:200]!r}: {exc}", fatal=True
            ) from exc
        if not isinstance(answer, dict):
            raise WorkerError("bad_answer", f"answer is not an object: {line[:200]!r}")
        if not answer.get("ok"):
            raise WorkerError(
                str(answer.get("failure_type", "benchmark_error")),
                str(answer.get("message", "unknown benchmark error")),
            )
        return answer

    def _die(self) -> None:
        """Kill the child now; :meth:`close` still closes the pipes afterwards."""
        proc = self._proc
        if proc is not None and not self._failed:
            self._failed = True
            kill_tree(proc)

    def setup(self, spec: Mapping[str, Any], timeout_s: float) -> dict[str, Any]:
        answer = self.request({"op": "setup", "spec": dict(spec)}, timeout_s)
        info = answer.get("info")
        if not isinstance(info, dict):
            raise WorkerError("bad_answer", "setup answer has no info object")
        return info

    def rep(self, timeout_s: float) -> dict[str, Any]:
        answer = self.request({"op": "rep"}, timeout_s)
        sample = answer.get("sample")
        if not isinstance(sample, dict):
            raise WorkerError("bad_answer", "rep answer has no sample object")
        return sample

    def probe(self, timeout_s: float) -> dict[str, Any]:
        answer = self.request({"op": "probe"}, timeout_s)
        info = answer.get("info")
        if not isinstance(info, dict):
            raise WorkerError("bad_answer", "probe answer has no info object")
        return info


def probe_capabilities(
    timeout_s: float = 300.0, python: str | None = None, script: Path = CHILD_SCRIPT
) -> dict[str, Any]:
    """Backends and CUDA state of this machine, measured in a child process.

    Never raises: a broken torch/CUDA install comes back as
    ``{"error": ..., "failure_type": ...}``.
    """
    worker = Worker(threads=1, python=python, script=script)
    try:
        worker.start()
        return worker.probe(timeout_s)
    except WorkerError as exc:
        return {
            "torch": None,
            "numpy": None,
            "cuda": {
                "available": False,
                "failure_type": exc.failure_type,
                "error": str(exc),
            },
        }
    finally:
        worker.close()


def merge_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate one repetition of K identical workers into one sample.

    Rates (``*_gb_s``, ``*flops``) are **summed** - the machine really delivered
    their sum at the same time - while durations (``*_s``) take the maximum,
    because the repetition lasted as long as its slowest worker.
    """
    if not samples:
        raise ValueError("no samples to merge")
    if len(samples) == 1:
        return dict(samples[0])
    merged: dict[str, Any] = {}
    keys = sorted({k for s in samples for k in s})
    for key in keys:
        values = [s[key] for s in samples if isinstance(s.get(key), int | float)]
        if len(values) != len(samples):
            continue
        merged[key] = max(values) if key.endswith("_s") else sum(values)
    merged["workers"] = len(samples)
    merged["per_worker"] = samples
    return merged


def run_parallel(
    calls: Sequence[Call], timeout_s: float
) -> tuple[list[dict[str, Any]], list[WorkerError]]:
    """Run ``calls`` (zero-argument callables) in threads and collect results."""
    results: list[dict[str, Any] | None] = [None] * len(calls)
    errors: list[WorkerError] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        try:
            results[index] = calls[index]()
        except WorkerError as exc:
            with lock:
                errors.append(exc)

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True)
        for i in range(len(calls))
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=timeout_s + _EXIT_GRACE_S)
    return [r for r in results if r is not None], errors


class WorkerGroup:
    """K identical children driven as one measurement case."""

    def __init__(
        self,
        count: int,
        *,
        threads: int = 1,
        python: str | None = None,
        script: Path = CHILD_SCRIPT,
    ) -> None:
        if count < 1:
            raise ValueError("count must be >= 1")
        self.workers = [
            Worker(threads=threads, python=python, script=script) for _ in range(count)
        ]

    def __enter__(self) -> WorkerGroup:
        for worker in self.workers:
            worker.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        for worker in self.workers:
            worker.close()

    def setup(self, spec: Mapping[str, Any], timeout_s: float) -> list[dict[str, Any]]:
        infos, errors = run_parallel(
            [partial(w.setup, spec, timeout_s) for w in self.workers], timeout_s
        )
        if errors:
            raise errors[0]
        if len(infos) != len(self.workers):
            raise WorkerError(
                "worker_timeout", "a worker did not finish setup in time", fatal=True
            )
        return infos

    def rep(self, timeout_s: float) -> dict[str, Any]:
        samples, errors = run_parallel(
            [partial(w.rep, timeout_s) for w in self.workers], timeout_s
        )
        if errors:
            raise errors[0]
        if len(samples) != len(self.workers):
            raise WorkerError(
                "worker_timeout", "a worker did not answer in time", fatal=True
            )
        return merge_samples(samples)
