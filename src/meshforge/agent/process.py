"""Child processes the agent can always get rid of: spawn, tree-kill, parent-death cleanup.

Shared by the hardware-score children (:mod:`meshforge.agent.benchmarks`) and the
subprocess execution runtime (:mod:`meshforge.agent.execution.subprocess_runtime`),
roadmap 0.4.17 items 1, 3 and 6. The guarantees, per platform:

**Process tree.** Every child is the root of its own group: on POSIX the leader of
a new session (``start_new_session``), so ``killpg`` reaches every descendant that
stayed in it; on Windows it is assigned to a private **job object** created with
``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``, so ``TerminateJobObject`` ends the whole
tree in one non-blocking call. If the job cannot be created or assigned (very old
Windows, a parent job that forbids nesting) the fallback is ``taskkill /F /T``,
run **off the event loop** in a thread (item 1): the kill is started synchronously
(so it is cancel-safe) and the task that runs it is kept and awaited by
:meth:`ProcessHandle.reap`; taskkill runs *before* ``proc.kill()`` because it
walks the tree from the live parent.

**Parent death (item 6).** A child must not outlive an agent that was SIGKILLed or
crashed:

* **Linux:** the child is started through a tiny exec shim (``python -I -S -c``)
  that sets ``PR_SET_PDEATHSIG = SIGKILL``, checks that the parent is still the one
  that spawned it (closing the race where the agent died before ``prctl``), lowers
  the priority if asked, and ``execve``\\s the real program - same pid, same argv,
  same environment. The death signal survives ``execve`` (not for set-uid
  binaries). Two documented limits: it fires when the *thread* that forked exits
  (asyncio forks from the event-loop thread, which lives as long as the agent's
  loop), and it covers the direct child only - grandchildren that the dead agent
  can no longer ``killpg`` are orphaned (the Docker runtime has no such gap: its
  containers are swept at the next start).
* **Windows:** the job object's handle is owned by the agent; when the agent dies
  for any reason the kernel closes it and ``KILL_ON_JOB_CLOSE`` ends every process
  in the job, descendants included. Limit: the child runs for a few microseconds
  before it is assigned; a process it starts in that window escapes the job.
* **macOS / other POSIX:** no parent-death signal exists; only the process group
  is used (``DEATH_SIGNAL_SUPPORTED`` says so).

**The spawn cancel window.** ``asyncio.create_subprocess_exec`` can be cancelled
after the fork but before it returns the ``Process``; a naive caller then leaks a
running child it has no handle for. :func:`spawn` shields the creation, and on
cancellation waits for it to finish, kills and reaps whatever it created, and only
then re-raises ``CancelledError``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import signal
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Final

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.logging import get_logger

__all__ = [
    "DEATH_SIGNAL_SUPPORTED",
    "NICE",
    "ProcessHandle",
    "SpawnError",
    "kill_tree",
    "resolve_executable",
    "spawn",
]

log = get_logger(__name__)

NICE: Final = 10
"""POSIX niceness for a lowered-priority child; Windows gets ``BELOW_NORMAL_PRIORITY_CLASS``."""

DEATH_SIGNAL_SUPPORTED: Final = (
    sys.platform.startswith("linux") or sys.platform == "win32"
)
"""Whether a child dies with a killed agent on this platform (pdeathsig / job object)."""

_REAP_S: Final = 2.0
_TASKKILL_TIMEOUT_S: Final = 5.0
"""Bound on the (threaded, off-loop) ``taskkill`` fallback on Windows."""

#: The Linux exec shim. argv: ppid, nice, keep_lc_ctype(0/1), executable, *argv.
#: Stdlib only, no site (-S) and isolated (-I): the user's PYTHON* variables and
#: site-packages never reach it. PEP 538 may have added ``LC_CTYPE=C.UTF-8`` to
#: this interpreter's environment; it is removed again unless the caller set it,
#: so the workload sees exactly the environment it was given.
_PDEATHSIG_SHIM: Final = r"""
import ctypes, os, signal, sys
ppid, nice, keep_lc = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3] == "1"
exe, argv = sys.argv[4], sys.argv[5:]
try:
    if ctypes.CDLL(None, use_errno=True).prctl(1, int(signal.SIGKILL), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
except (OSError, AttributeError) as exc:
    sys.stderr.write("meshforge: cannot arm the parent-death signal: %s\n" % exc)
    os._exit(125)
if os.getppid() != ppid:
    os._exit(137)
if nice > 0:
    try:
        os.nice(nice)
    except OSError:
        pass
env = dict(os.environ)
if not keep_lc:
    env.pop("LC_CTYPE", None)
try:
    os.execve(exe, argv, env)
except OSError as exc:
    sys.stderr.write("meshforge: cannot execute %s: %s\n" % (argv[0], exc.strerror))
    os._exit(127)
"""


class SpawnError(MeshForgeError):
    """A child process could not be started. ``reason`` is a stable code:
    ``executable_not_found`` or ``spawn_failed``."""

    default_failure_type = FailureType.INTERNAL_ERROR

    def __init__(
        self, message: str, *, reason: str, failure_type: FailureType | None = None
    ) -> None:
        super().__init__(message, failure_type=failure_type, details={"reason": reason})
        self.reason = reason


def resolve_executable(name: str, env: Mapping[str, str] | None = None) -> str:
    """Absolute path of ``name``, searched on the *child's* ``PATH``.

    A name with a directory part is used as given (made absolute). Raises
    :class:`SpawnError` (``executable_not_found``) when nothing is found, so a
    typo is a typed error at spawn time rather than an exit 127 from the shim.
    """
    if not name:
        raise SpawnError("empty executable name", reason="executable_not_found")
    if os.sep in name or (os.altsep and os.altsep in name):
        path = Path(name)
        if path.is_file():
            return str(path.resolve())
        raise SpawnError(
            f"executable {path.name!r} does not exist", reason="executable_not_found"
        )
    search = None if env is None else env.get("PATH", "")
    found = shutil.which(name, path=search)
    if found is None:
        raise SpawnError(
            f"executable {name!r} is not on the child's PATH",
            reason="executable_not_found",
        )
    return str(Path(found).resolve()) if not Path(found).is_absolute() else found


# ---------------------------------------------------------------- Windows job
if sys.platform == "win32":  # pragma: no cover - exercised on Windows CI only
    import ctypes
    from ctypes import wintypes

    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS: Final = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE: Final = 0x2000
    _PROCESS_SET_QUOTA: Final = 0x0100
    _PROCESS_TERMINATE: Final = 0x0001

    class _IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in (
            "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
            "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
        )]  # fmt: skip

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
    ]  # fmt: skip
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    def _job_for(pid: int) -> int | None:
        """A kill-on-close job holding ``pid``, or None (fallback: taskkill)."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _ExtendedLimits()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        handle = _kernel32.OpenProcess(
            _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
        )
        if ok and handle:
            ok = _kernel32.AssignProcessToJobObject(job, handle)
        if handle:
            _kernel32.CloseHandle(handle)
        if not ok or not handle:
            _kernel32.CloseHandle(job)
            return None
        return int(job)

    def _terminate_job(job: int) -> bool:
        return bool(_kernel32.TerminateJobObject(job, 1))

    def _close_job(job: int) -> None:
        _kernel32.CloseHandle(job)

else:

    def _job_for(pid: int) -> int | None:
        return None

    def _terminate_job(job: int) -> bool:
        return False

    def _close_job(job: int) -> None:
        return None


def _taskkill(pid: int) -> None:  # pragma: no cover - Windows only
    taskkill = shutil.which("taskkill") or os.path.join(
        os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32", "taskkill.exe"
    )
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [taskkill, "/F", "/T", "/PID", str(pid)],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT_S,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


# -------------------------------------------------------------------- kill
def kill_tree(proc: asyncio.subprocess.Process) -> None:
    """Kill a POSIX child and its process group now (synchronous, cancel-safe).

    Kept for callers holding a bare ``Process`` that :func:`spawn` started in its
    own session. On Windows prefer :meth:`ProcessHandle.kill_now`, which uses the
    job object; here Windows only gets ``proc.kill()`` (the direct child).
    """
    if proc.returncode is not None:
        return
    if sys.platform != "win32":
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.kill()


class ProcessHandle:
    """A spawned child and everything needed to end its whole tree. One owner task.

    ``kill_now`` is synchronous and safe from ``except BaseException`` blocks; the
    Windows taskkill fallback it may start is a kept task that :meth:`reap` awaits.
    """

    def __init__(self, proc: asyncio.subprocess.Process, job: int | None) -> None:
        self.proc = proc
        self._job = job
        self._fallback: asyncio.Task[None] | None = None
        self._released = False
        self.killed = False
        """Set once :meth:`kill_now` ran - the exit was forced, not the workload's own."""

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    @property
    def uses_job_object(self) -> bool:
        return self._job is not None

    def terminate_group(self) -> None:
        """Ask the whole tree to stop (graceful). POSIX: ``SIGTERM`` to the group.

        Windows has no reliable graceful signal for a console-less child: a
        ``CTRL_BREAK_EVENT`` is attempted (it reaches the group only when the agent
        shares a console with it) and the caller's forced kill after its grace
        period is the real stop - documented in ``docs/OPERATIONS.md``.
        """
        if self.proc.returncode is not None:
            return
        if sys.platform == "win32":  # pragma: no cover - Windows only
            with contextlib.suppress(OSError, ValueError, AttributeError):
                os.kill(self.proc.pid, signal.CTRL_BREAK_EVENT)
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.proc.pid, signal.SIGTERM)

    def kill_now(self) -> None:
        """Force-kill the whole tree, now. Never blocks the loop, never raises."""
        self.killed = True
        if sys.platform != "win32":
            # killpg even after the leader exited - its descendants may live on, and
            # Linux keeps a pid reserved while it is still some group's id. Not
            # after release(): the group may be gone and its id reused by then.
            if not self._released:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(self.proc.pid, signal.SIGKILL)
        elif self._job is not None:  # pragma: no cover - Windows only
            if _terminate_job(self._job):
                return
        elif (
            self._fallback is None and self.proc.returncode is None
        ):  # pragma: no cover
            # taskkill walks the tree from the live parent, so it runs before
            # proc.kill(); both happen in the kept task, off the event loop.
            self._fallback = asyncio.get_running_loop().create_task(
                self._taskkill_then_kill(), name=f"taskkill:{self.proc.pid}"
            )
            return
        if self.proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, OSError):
                self.proc.kill()

    async def _taskkill_then_kill(self) -> None:  # pragma: no cover - Windows only
        try:
            await asyncio.to_thread(_taskkill, self.proc.pid)
        finally:
            with contextlib.suppress(ProcessLookupError, OSError):
                self.proc.kill()

    async def kill(self) -> None:
        self.kill_now()
        await self.reap()

    async def wait(self, timeout_s: float) -> int | None:
        """The exit code, or None if the child is still running after ``timeout_s``."""
        with contextlib.suppress(TimeoutError):
            return await asyncio.wait_for(self.proc.wait(), max(0.0, timeout_s))
        return self.proc.returncode

    async def reap(self, timeout_s: float = _REAP_S) -> None:
        """Wait (bounded) for the exit and for a pending taskkill; release the job.

        Cancel-safe: whatever happens, the job handle is closed on the way out,
        which on Windows kills anything still in it.
        """
        try:
            if self.proc.stdin is not None:
                with contextlib.suppress(OSError, RuntimeError):
                    self.proc.stdin.close()
            fallback = self._fallback
            if fallback is not None:
                with contextlib.suppress(TimeoutError, OSError):
                    await asyncio.wait_for(
                        asyncio.shield(fallback), _TASKKILL_TIMEOUT_S
                    )
            await self.wait(timeout_s)
        finally:
            self.release()

    def release(self) -> None:
        """Close the job handle (Windows: kills anything left in the job).

        After this the handle no longer signals the process group (its id may be
        reused); call :meth:`kill_now` first to end leftover descendants.
        """
        self._released = True
        job, self._job = self._job, None
        if job is not None:  # pragma: no cover - Windows only
            _close_job(job)


# ------------------------------------------------------------------- spawn
def _platform_argv(
    argv: Sequence[str],
    env: Mapping[str, str],
    *,
    lower_priority: bool,
    die_with_parent: bool,
) -> tuple[list[str], dict[str, Any]]:
    exe = resolve_executable(argv[0], env)
    kwargs: dict[str, Any] = {}
    if sys.platform == "win32":  # pragma: no cover - Windows only
        flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
        if lower_priority:
            flags |= subprocess.BELOW_NORMAL_PRIORITY_CLASS
        kwargs["creationflags"] = flags
        return [exe, *argv[1:]], kwargs
    kwargs["start_new_session"] = True
    if die_with_parent and sys.platform.startswith("linux"):
        shim = [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _PDEATHSIG_SHIM,
            str(os.getpid()),
            str(NICE if lower_priority else 0),
            "1" if "LC_CTYPE" in env else "0",
            exe,
            *argv,
        ]
        return shim, kwargs
    return [exe, *argv[1:]], kwargs


async def spawn(
    argv: Sequence[str],
    *,
    env: Mapping[str, str],
    cwd: str | Path | None = None,
    stdin: int | None = asyncio.subprocess.DEVNULL,
    stdout: int | None = asyncio.subprocess.PIPE,
    stderr: int | None = asyncio.subprocess.PIPE,
    limit: int = 1 << 16,
    lower_priority: bool = False,
    die_with_parent: bool = True,
    on_spawn: Callable[[int], None] | None = None,
) -> ProcessHandle:
    """Start ``argv`` as the root of its own killable tree (see the module docstring).

    ``env`` is the child's **whole** environment (nothing is inherited) and its
    ``PATH`` is where ``argv[0]`` is looked up. ``lower_priority``: nice 10 on
    Linux (applied by the shim), ``BELOW_NORMAL_PRIORITY_CLASS`` on Windows,
    nothing elsewhere. Raises :class:`SpawnError`; on cancellation the half-started
    child is killed and reaped before ``CancelledError`` propagates.
    """
    if not argv:
        raise SpawnError("empty argv", reason="executable_not_found")
    real_argv, kwargs = _platform_argv(
        argv, env, lower_priority=lower_priority, die_with_parent=die_with_parent
    )
    creating = asyncio.ensure_future(
        asyncio.create_subprocess_exec(
            *real_argv,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            env=dict(env),
            cwd=None if cwd is None else str(cwd),
            limit=limit,
            **kwargs,
        )
    )
    try:
        proc = await asyncio.shield(creating)
    except asyncio.CancelledError:
        await _abandon(creating)
        raise
    except (OSError, ValueError) as exc:
        why = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
        raise SpawnError(
            f"cannot start {Path(argv[0]).name!r}: {type(exc).__name__}: {why}",
            reason="spawn_failed",
        ) from exc
    handle = ProcessHandle(proc, _job_for(proc.pid))
    if on_spawn is not None:
        on_spawn(proc.pid)
    return handle


async def _abandon(creating: asyncio.Future[asyncio.subprocess.Process]) -> None:
    """The spawning task was cancelled: kill and reap whatever the spawn created."""
    try:
        proc = await asyncio.shield(creating)
    except asyncio.CancelledError:
        # Cancelled again while waiting: finish the job from a callback instead.
        creating.add_done_callback(_kill_created)
        raise
    except (OSError, ValueError):
        return  # nothing was started
    handle = ProcessHandle(proc, _job_for(proc.pid))
    handle.kill_now()
    await handle.reap()
    log.info("agent.process.spawn_cancelled", pid=proc.pid)


def _kill_created(fut: asyncio.Future[asyncio.subprocess.Process]) -> None:
    if fut.cancelled() or fut.exception() is not None:
        return
    proc = fut.result()
    ProcessHandle(proc, _job_for(proc.pid)).kill_now()
