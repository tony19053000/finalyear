"""Subprocess runner for probes: argument lists only, bounded time, never raises.

Three guarantees (tester defects 0.2.2 D-2/D-3):

* **No leaked processes.** Every command runs in its own process group (POSIX: a
  new session; Windows: a new process group). On timeout the *whole tree* is
  killed (``killpg(SIGKILL)`` / ``taskkill /F /T``), so a wrapper script's
  children (``sh`` -> ``sleep``) die with it, and the child is always reaped.
* **A run deadline.** :func:`start_budget` sets one wall-clock budget for the
  whole inventory run. Each command gets ``min(its own cap, time left)``; once
  the budget is spent, further commands are not started and return an explicit
  ``skipped: run deadline exceeded`` error instead.
* **A hung tool is tried once.** An executable that timed out is remembered for
  the rest of the run; later calls return ``skipped: did not respond earlier``
  instead of paying the same timeout again.

Without :func:`start_budget` (unit tests, library use) there is no deadline.

PowerShell scripts run from a temporary ``.ps1`` file (:func:`powershell_script`),
never via ``-EncodedCommand``: on a real Windows 11 laptop (lab L3, R1-3/R1-5) a
~6000-character encoded command hung for more than 40 s (most likely Defender /
AMSI scanning it) while the same script run with ``-File`` answered in ~3 s.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final

DEFAULT_TIMEOUT_S: Final = 15.0
#: Default wall-clock budget of one inventory run (``--timeout``).
DEFAULT_TOTAL_S: Final = 60.0
#: Share of the budget kept back for sanitising, writing and the summary, so the
#: whole process ends within ``--timeout`` (clamped to 1..10 s).
_RESERVE_FRACTION: Final = 0.15
#: A command is not started with less than this much budget left.
_MIN_SLICE_S: Final = 0.5
#: How long to wait for a killed tree's pipes to drain before giving up on them.
_REAP_S: Final = 2.0
_MAX_OUTPUT_CHARS: Final = 1_000_000

SKIPPED_DEADLINE: Final = "skipped: run deadline exceeded"
SKIPPED_HUNG: Final = "skipped: did not respond earlier in this run"

#: Options for every ``powershell`` call; ``-File <script>`` is appended last.
POWERSHELL_OPTIONS: Final = (
    "-NoProfile",
    "-NonInteractive",
    "-ExecutionPolicy",
    "Bypass",
)
_PS_DIR_PREFIX: Final = "meshforge-ps-"
_PS_FILE_NAME: Final = "script.ps1"
#: Deleting the script can briefly fail on Windows while a just-killed
#: PowerShell still holds it open; retried this often, this far apart.
_CLEANUP_TRIES: Final = 5
_CLEANUP_PAUSE_S: Final = 0.2


@dataclass(frozen=True, slots=True)
class CmdResult:
    """Outcome of one command. ``error`` is set when it could not run or timed out."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.returncode == 0

    def describe_failure(self) -> str:
        """Short text for a failed command (error, else stderr/stdout, else code)."""
        if self.error:
            return self.error
        text = (self.stderr.strip() or self.stdout.strip())[:2000]
        return text or f"{self.argv[0]} exited with code {self.returncode}"


class _RunState:
    """Per-run state: the deadline and the executables that already hung.

    One instance per process (an inventory run is one process); :func:`reset`
    restores it for tests.
    """

    def __init__(self) -> None:
        self.deadline: float | None = None
        self.hung: set[str] = set()


_STATE: Final = _RunState()


def _clock() -> float:
    """Monotonic seconds (a seam so tests can drive the deadline)."""
    return time.monotonic()


def start_budget(total_s: float) -> None:
    """Start the run deadline: all commands together get about ``total_s`` seconds."""
    reserve = min(10.0, max(1.0, total_s * _RESERVE_FRACTION))
    _STATE.deadline = _clock() + max(0.0, total_s - reserve)


def reset() -> None:
    """Forget the deadline and the hung-executable cache (tests)."""
    _STATE.deadline = None
    _STATE.hung.clear()


def remaining() -> float | None:
    """Seconds left in the run budget, or None when there is no deadline."""
    if _STATE.deadline is None:
        return None
    return _STATE.deadline - _clock()


def which(name: str) -> str | None:
    """Full path of an executable on PATH, or None."""
    return shutil.which(name)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    # Parsers expect untranslated output (lscpu, ip, mokutil ...).
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return env


def _decode(data: bytes, encoding: str) -> str:
    if encoding == "utf-16-le" or (len(data) >= 2 and data[1:2] == b"\x00"):
        text = data.decode("utf-16-le", errors="replace")
    else:
        text = data.decode("utf-8", errors="replace")
    return text.replace("\ufeff", "").replace("\x00", "")[:_MAX_OUTPUT_CHARS]


def _fmt_s(seconds: float) -> str:
    return f"{seconds:.3g}"


def _kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Kill ``proc`` and every descendant still in its process group / tree."""
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
        # start_new_session=True made the child a group leader: pgid == pid.
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        proc.kill()


def _reap(proc: subprocess.Popen[bytes]) -> None:
    """Collect a killed child. A descendant that escaped the group could still
    hold the pipes open, so draining them is bounded; the child itself is always
    waited for (it was SIGKILLed, so that returns promptly)."""
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


def _spawn(argv: list[str], cwd: Path | None) -> subprocess.Popen[bytes]:
    if sys.platform == "win32":
        return subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            cwd=cwd,
            env=_env(),
            creationflags=subprocess.CREATE_NO_WINDOW
            | subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    return subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        cwd=cwd,
        env=_env(),
        start_new_session=True,
    )


def run(
    name: str,
    args: list[str],
    *,
    timeout: float = DEFAULT_TIMEOUT_S,
    cwd: Path | None = None,
    encoding: str = "utf-8",
    remember_hang: bool = True,
) -> CmdResult:
    """Run ``name`` (looked up on PATH) with ``args``. Never raises.

    ``name`` may also be an absolute path (e.g. ``sys.executable``). ``timeout`` is
    this command's cap; the run budget may shorten it. ``remember_hang=False`` for
    executables that run unrelated work per call (the Python helper scripts), so
    one hung helper does not skip the next.
    """
    exe = name if os.path.isabs(name) else which(name)
    argv = (name, *args)
    if exe is None:
        return CmdResult(argv, None, "", "", error=f"{name}: not found on PATH")
    if exe in _STATE.hung:
        return CmdResult(argv, None, "", "", error=f"{name}: {SKIPPED_HUNG}")
    limit, clipped = timeout, False
    left = remaining()
    if left is not None:
        if left < _MIN_SLICE_S:
            return CmdResult(argv, None, "", "", error=f"{name}: {SKIPPED_DEADLINE}")
        if left < timeout:
            limit, clipped = left, True
    try:
        proc = _spawn([exe, *args], cwd)
    except (OSError, ValueError) as exc:
        return CmdResult(argv, None, "", "", error=f"{name}: could not run: {exc}")
    try:
        out, err = proc.communicate(timeout=limit)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        _reap(proc)
        if remember_hang:
            _STATE.hung.add(exe)
        why = " (run deadline reached)" if clipped else ""
        return CmdResult(
            argv, None, "", "", error=f"{name}: timed out after {_fmt_s(limit)} s{why}"
        )
    except BaseException:  # Ctrl-C etc.: never leave the tree behind, then re-raise
        _kill_tree(proc)
        _reap(proc)
        raise
    return CmdResult(
        argv,
        proc.returncode,
        _decode(out, encoding),
        _decode(err, encoding),
    )


def _remove_tree(path: Path) -> None:
    """Delete ``path`` (a script directory); retried briefly, never raises."""
    for attempt in range(_CLEANUP_TRIES):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        if attempt + 1 < _CLEANUP_TRIES:
            time.sleep(_CLEANUP_PAUSE_S)


@contextlib.contextmanager
def powershell_script(script: str) -> Iterator[list[str]]:
    """Arguments for ``powershell`` that run ``script`` from a temporary file.

    The script is written as UTF-8 *with* BOM (Windows PowerShell 5.1 reads a
    BOM-less file in the ANSI code page and would garble non-ASCII text) into a
    fresh private directory (``mkdtemp``: mode 0700 on POSIX, the user's own
    ``%TEMP%`` on Windows). The directory is deleted when the block ends -
    normally, on a timeout result, or on any exception (Ctrl-C included)::

        with powershell_script(SCRIPT) as args:
            result = run("powershell", args, timeout=20.0)
    """
    directory = Path(tempfile.mkdtemp(prefix=_PS_DIR_PREFIX))
    try:
        path = directory / _PS_FILE_NAME
        with path.open("x", encoding="utf-8-sig", newline="") as handle:
            handle.write(script)
        yield [*POWERSHELL_OPTIONS, "-File", str(path)]
    finally:
        _remove_tree(directory)
