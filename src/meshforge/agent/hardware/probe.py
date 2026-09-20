"""Probe primitives: a monotonic budget, a typed outcome, and a safe command runner.

Every hardware probe in this package is *individually guarded* (D10): it returns a
:class:`ProbeOutcome` whose :class:`ProbeStatus` says whether the value is real,
and one probe failing costs one field of the Hello, never the agent. Nothing here
raises for a peer- or machine-controlled reason.

**Time.** :class:`Budget` is the one clock in this package: ``time.monotonic`` (never
the wall clock), so a clock jump during start-up cannot extend or cut discovery.
Every probe asks the budget for its slice; when the budget is spent the remaining
probes return ``TIMEOUT`` immediately instead of starting work.

**Subprocesses.** :func:`run_command` is the technique lifted from
``lab/_inventory/runner.py`` (which this package must not import - ``src`` never
imports ``lab``): argument lists only (never a shell), its own process group, and on
timeout the whole tree is killed and reaped, so ``nvidia-smi`` hanging on a broken
driver leaves nothing behind on Linux or Windows.
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

__all__ = [
    "Budget",
    "CmdResult",
    "ProbeOutcome",
    "ProbeStatus",
    "clip",
    "guarded",
    "module_installed",
    "run_command",
    "which",
]

_MAX_OUTPUT_CHARS: Final = 200_000
_REAP_S: Final = 2.0
_MIN_SLICE_S: Final = 0.05
"""A probe is not started with less budget left than this."""


class ProbeStatus(StrEnum):
    """Why a probed field is (not) there. Persisted in ``HardwareFacts`` only."""

    OK = "ok"
    UNAVAILABLE = "unavailable"
    """The source does not exist on this machine (no NVIDIA GPU, no docker)."""
    TIMEOUT = "timeout"
    """The source exists but did not answer inside its slice of the budget."""
    ERROR = "error"
    """The source answered with something unusable (driver error, bad CSV, OSError)."""


@dataclass(frozen=True, slots=True)
class ProbeOutcome[T]:
    """One probe's result: ``value`` is trustworthy only when ``status is OK``."""

    name: str
    status: ProbeStatus
    value: T | None = None
    reason: str = ""
    """Short, human-readable, never a secret and never a full user path."""
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.status is ProbeStatus.OK


class Budget:
    """A monotonic time budget shared by every probe of one ``discover()`` call."""

    __slots__ = ("_clock", "_deadline", "_total_s")

    def __init__(
        self, total_s: float, *, clock: Callable[[], float] = time.monotonic
    ) -> None:
        if total_s <= 0:
            raise ValueError("total_s must be > 0")
        self._clock = clock
        self._total_s = total_s
        self._deadline = clock() + total_s

    def now(self) -> float:
        return self._clock()

    @property
    def remaining_s(self) -> float:
        return max(self._deadline - self._clock(), 0.0)

    @property
    def expired(self) -> bool:
        return self.remaining_s < _MIN_SLICE_S

    def slice_s(self, cap_s: float) -> float:
        """This probe's timeout: its own cap, shortened by what is left overall."""
        return min(cap_s, self.remaining_s)


def guarded[T](
    name: str, budget: Budget, cap_s: float, fn: Callable[[float], T | None]
) -> ProbeOutcome[T]:
    """Run ``fn(timeout_s)`` under the budget and turn anything it does into an outcome.

    ``fn`` returns the value, or ``None`` when the source is simply not present on
    this machine (``UNAVAILABLE``). Any exception becomes ``ERROR`` with the
    exception type and message as the reason - a probe never propagates.
    """
    if budget.expired:
        return ProbeOutcome(name, ProbeStatus.TIMEOUT, reason="discovery budget spent")
    started = budget.now()
    try:
        value = fn(budget.slice_s(cap_s))
    except TimeoutError as exc:
        return ProbeOutcome(
            name,
            ProbeStatus.TIMEOUT,
            reason=str(exc) or "timed out",
            duration_s=budget.now() - started,
        )
    except Exception as exc:  # noqa: BLE001 - a broken probe must cost one field only
        return ProbeOutcome(
            name,
            ProbeStatus.ERROR,
            reason=f"{type(exc).__name__}: {exc}"[:200],
            duration_s=budget.now() - started,
        )
    duration = budget.now() - started
    if value is None:
        return ProbeOutcome(
            name, ProbeStatus.UNAVAILABLE, reason="not present", duration_s=duration
        )
    return ProbeOutcome(name, ProbeStatus.OK, value=value, duration_s=duration)


def clip(value: str, limit: int) -> str:
    """``value`` truncated to ``limit`` characters (the agent never sends more)."""
    return value if len(value) <= limit else value[:limit]


def module_installed(name: str) -> bool:
    """True if ``name`` can be imported, **without importing it** (torch costs seconds)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def which(name: str) -> str | None:
    """Full path of an executable on PATH, or None."""
    return shutil.which(name)


@dataclass(frozen=True, slots=True)
class CmdResult:
    """Outcome of one command; ``error`` is set when it could not run or timed out."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0

    def describe_failure(self) -> str:
        if self.error:
            return self.error
        text = (self.stderr.strip() or self.stdout.strip())[:200]
        return text or f"{self.argv[0]} exited with code {self.returncode}"


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` and every descendant still in its process group / tree."""
    if sys.platform == "win32":
        taskkill = shutil.which("taskkill") or os.path.join(
            os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe"
        )
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                [taskkill, "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=10,
                check=False,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
    else:
        # start_new_session=True made the child a group leader: pgid == pid.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Collect a killed child; draining its pipes is bounded, the wait is not skipped."""
    try:
        proc.communicate(timeout=_REAP_S)
    except subprocess.TimeoutExpired:
        for pipe in (proc.stdout, proc.stderr):
            if pipe is not None:
                with contextlib.suppress(OSError):
                    pipe.close()
    except (OSError, ValueError):
        pass
    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
        proc.wait(timeout=_REAP_S)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Parsers expect untranslated output.
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _spawn(argv: list[str]) -> subprocess.Popen[bytes]:
    if sys.platform == "win32":
        return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            env=_env(),
            creationflags=subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=_env(),
        start_new_session=True,
    )


def _decode(data: bytes) -> str:
    if len(data) >= 2 and data[1:2] == b"\x00":  # PowerShell's UTF-16-LE
        text = data.decode("utf-16-le", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\ufeff", "").replace("\x00", "")[:_MAX_OUTPUT_CHARS]


def run_command(name: str, args: list[str], *, timeout_s: float) -> CmdResult:
    """Run ``name args`` with a hard timeout. Never raises; see module docstring."""
    exe = name if os.path.isabs(name) else which(name)
    argv = (name, *args)
    if exe is None:
        return CmdResult(argv, None, "", "", error=f"{name}: not found on PATH")
    if timeout_s < _MIN_SLICE_S:
        return CmdResult(argv, None, "", "", error=f"{name}: no time budget left")
    try:
        proc = _spawn([exe, *args])
    except (OSError, ValueError) as exc:
        return CmdResult(argv, None, "", "", error=f"{name}: could not run: {exc}")
    try:
        out, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        _reap(proc)
        return CmdResult(
            argv, None, "", "", error=f"{name}: timed out after {timeout_s:.3g} s"
        )
    return CmdResult(argv, proc.returncode, _decode(out), _decode(err))
