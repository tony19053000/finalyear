#!/usr/bin/env python3
"""Run pytest and refuse to let a green summary hide a bad exit code.

pytest prints its summary line *before* the interpreter shuts down, so a crash
during shutdown (a C extension segfaulting while modules are torn down) leaves
``3214 passed ... in 51s`` on screen and exit status 139 underneath. That
exact bug survived three review rounds because everyone read the summary and
nobody read the exit code. This wrapper runs pytest, passes its output through
unchanged, and when pytest exits non-zero it says so loudly - with the signal
name when there is one - and exits with the same non-zero status.

Usage (every argument is passed to pytest)::

    uv run python scripts/pytest_exit_guard.py -m unit -q
    uv run python scripts/pytest_exit_guard.py -m component

Exit status: pytest's own, except that death by a signal is reported the way a
POSIX shell does (``128 + signal``, e.g. 139 for SIGSEGV).
"""

from __future__ import annotations

import collections
import re
import signal
import subprocess
import sys
from collections.abc import Sequence

#: pytest's final line: ``3 passed, 1 skipped in 0.12s`` (``-q``) or the same
#: wrapped in ``=`` rules; the duration may be ``(0:02:06)``-suffixed.
_SUMMARY = re.compile(r"^=*\s*((?:\d+ [a-z]+(?:, )?)+) in \d+(?:\.\d+)?s\b")
_RED_OUTCOMES = frozenset({"failed", "error", "errors"})
#: How much trailing output to keep for finding the summary line.
_TAIL_LINES = 50
#: Windows reports a native crash as an NTSTATUS exit code, not a signal.
_WINDOWS_CRASHES = {
    0xC0000005: "access violation",
    0xC00000FD: "stack overflow",
    0xC0000409: "stack buffer overrun",
}


def summary_line(lines: Sequence[str]) -> str | None:
    """The last pytest summary line in ``lines``, or None if there is none."""
    for line in reversed(lines):
        if _SUMMARY.match(line.strip()):
            return line.strip()
    return None


def is_green(summary: str) -> bool:
    """True if a summary line reports no failed test and no error."""
    match = _SUMMARY.match(summary)
    if match is None:
        return False
    outcomes = {part.split(" ", 1)[1] for part in match.group(1).split(", ") if part}
    return not outcomes & _RED_OUTCOMES


def exit_status(returncode: int) -> tuple[int, str]:
    """(status to exit with, human description) for a subprocess return code."""
    if returncode < 0:
        signum = -returncode
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = f"signal {signum}"
        return 128 + signum, f"killed by {name}"
    crash = _WINDOWS_CRASHES.get(returncode & 0xFFFFFFFF)
    if crash is not None:
        return returncode, f"crashed ({crash}, 0x{returncode & 0xFFFFFFFF:08X})"
    return returncode, f"exit status {returncode}"


def main(
    argv: Sequence[str] | None = None, *, command: Sequence[str] | None = None
) -> int:
    """Run pytest (or ``command``, for tests) and return the status to exit with."""
    args = list(sys.argv[1:] if argv is None else argv)
    cmd = (
        list(command)
        if command is not None
        else [sys.executable, "-m", "pytest", *args]
    )
    tail: collections.deque[str] = collections.deque(maxlen=_TAIL_LINES)
    with subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    ) as proc:
        for line in proc.stdout or ():
            sys.stdout.write(line)
            sys.stdout.flush()
            tail.append(line)
    if proc.returncode == 0:
        return 0
    status, how = exit_status(proc.returncode)
    summary = summary_line(list(tail))
    if summary is not None and is_green(summary):
        sys.stderr.write(
            "\n"
            "pytest-exit-guard: FAILED - the summary is green but pytest did not exit 0.\n"
            f"  summary: {summary}\n"
            f"  result:  {how} (exit {status})\n"
            "  Every test passed, then the process failed - usually a crash while the\n"
            "  interpreter shut down. A green summary is not a green run.\n"
        )
    else:
        sys.stderr.write(
            f"\npytest-exit-guard: pytest failed ({how}, exit {status}).\n"
        )
    return status


if __name__ == "__main__":
    sys.exit(main())
