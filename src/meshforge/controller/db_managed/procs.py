"""Finding and stopping PostgreSQL processes the controller does not own (Linux).

After a controller crash the postmaster may still run (no death signal: host-level
kill of the whole session, a start from a thread that exited, a bug) and
``postmaster.pid`` may point at a pid that is gone or was **reused** by an unrelated
process. PostgreSQL itself refuses to start when the pid in the lock file is alive,
whatever that process is.

Identification does not trust the lock file alone: every postmaster and backend
``chdir``s into its data dir, so a process whose ``/proc/<pid>/cwd`` is our data dir
and whose executable is ``postgres`` *is* one of ours (only same-user processes are
visible - exactly the ones that can hold our cluster). A reused pid fails that test.

Stopping a process that is not our child: signal, then poll ``kill(pid, 0)`` and
``/proc/<pid>/stat`` (we cannot ``waitpid`` it; its new parent reaps it).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = [
    "FOREIGN_STOP_STEPS",
    "PROC_SCAN_SUPPORTED",
    "SIG_IMMEDIATE",
    "SIG_KILL",
    "ForeignStop",
    "data_dir_holders",
    "is_postgres_of",
    "pid_alive",
    "stop_foreign",
]

PROC_SCAN_SUPPORTED: Final = sys.platform.startswith("linux")
_PROC: Final = Path("/proc")

# PostgreSQL's "immediate shutdown" and the last-resort kill. Windows has neither
# SIGQUIT nor SIGKILL; there os.kill()/send_signal() with SIGTERM is
# TerminateProcess, the nearest equivalent. Defined per platform so this module
# (and everything importing it) loads on Windows too; the managed cluster itself
# is Linux-first (D398).
if sys.platform == "win32":
    SIG_IMMEDIATE: Final = signal.SIGTERM
    SIG_KILL: Final = signal.SIGTERM
else:
    SIG_IMMEDIATE: Final = signal.SIGQUIT
    SIG_KILL: Final = signal.SIGKILL

FOREIGN_STOP_STEPS: Final[tuple[tuple[signal.Signals, float], ...]] = (
    (signal.SIGINT, 30.0),  # fast shutdown: its clients died with the controller
    (SIG_IMMEDIATE, 10.0),  # immediate: crash recovery on the next start
    (SIG_KILL, 5.0),  # last resort; backends exit on postmaster death
)


def pid_alive(pid: int) -> bool:
    """True while ``pid`` exists and is not a zombie (POSIX)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, another user's - never ours to stop
    if not PROC_SCAN_SUPPORTED:
        return True
    try:
        stat = (_PROC / str(pid) / "stat").read_bytes()
    except OSError:
        return False
    state = stat.rsplit(b")", 1)[-1].split()[:1]
    return state not in ([b"Z"], [b"X"])


def _exe_name(pid: int) -> str | None:
    try:
        target = os.readlink(_PROC / str(pid) / "exe")
    except OSError:
        return None
    return Path(target.removesuffix(" (deleted)")).name


def _cwd(pid: int) -> str | None:
    try:
        return os.readlink(_PROC / str(pid) / "cwd")
    except OSError:
        return None


def is_postgres_of(pid: int, data_dir: Path) -> bool:
    """True when ``pid`` is a live ``postgres`` process working in ``data_dir``."""
    if not PROC_SCAN_SUPPORTED or not pid_alive(pid):
        return False
    return _exe_name(pid) == "postgres" and _cwd(pid) == str(data_dir.resolve())


def data_dir_holders(data_dir: Path) -> list[int]:
    """Pids of live ``postgres`` processes (postmaster or backends) in ``data_dir``.

    Empty when ``/proc`` scanning is unsupported (non-Linux).
    """
    if not PROC_SCAN_SUPPORTED:
        return []
    wanted = str(data_dir.resolve())
    found: list[int] = []
    for entry in _PROC.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if _cwd(pid) == wanted and _exe_name(pid) == "postgres" and pid_alive(pid):
            found.append(pid)
    return sorted(found)


@dataclass(frozen=True, slots=True)
class ForeignStop:
    """How a non-child process was stopped."""

    pid: int
    stopped: bool
    signal: str | None
    """Name of the signal after which it exited (None: it was already gone)."""
    duration_s: float


async def stop_foreign(
    pid: int,
    *,
    steps: Sequence[tuple[signal.Signals, float]] = FOREIGN_STOP_STEPS,
    poll_s: float = 0.05,
) -> ForeignStop:
    """Escalate through ``steps`` until ``pid`` is gone; never raises for ESRCH."""
    started = time.monotonic()
    for signum, timeout_s in steps:
        if not pid_alive(pid):
            return ForeignStop(pid, True, None, time.monotonic() - started)
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signum)
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if not pid_alive(pid):
                return ForeignStop(pid, True, signum.name, time.monotonic() - started)
            await asyncio.sleep(poll_s)
    alive = pid_alive(pid)
    return ForeignStop(pid, not alive, None, time.monotonic() - started)
