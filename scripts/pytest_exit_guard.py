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

import re
import signal
import subprocess
import sys
from collections.abc import Iterable, Sequence

#: pytest's final line: ``3 passed, 1 skipped in 0.12s`` (``-q``) or the same
#: wrapped in ``=`` rules; the duration may be ``(0:02:06)``-suffixed.
_SUMMARY = re.compile(r"^=*\s*((?:\d+ [a-z]+(?:, )?)+) in \d+(?:\.\d+)?s\b")
_RED_OUTCOMES = frozenset({"failed", "error", "errors"})
#: ANSI SGR colour codes: CI sets FORCE_COLOR, and pytest colours the summary.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
#: tests/conftest.py's report when it fails a run whose tests all passed: this
#: separator (which pytest may append to the progress-dots line), then a line
#: starting with the mark. Both are required - the phrase alone also appears in
#: test sources and assertion diffs.
_HALF_IMPORT_SEP = re.compile(r"=+ half-imported packages =+$")
_HALF_IMPORT_MARK = "half-imported package(s) left in the pytest process"
#: After Ctrl-C, how long pytest gets to finish on its own before the guard
#: forwards the interrupt (a terminal Ctrl-C already reached it; a signal sent
#: to the guard's PID alone did not).
_INTERRUPT_GRACE_S = 5.0
#: Windows reports a native crash as an NTSTATUS exit code, not a signal.
_WINDOWS_CRASHES = {
    0xC0000005: "access violation",
    0xC00000FD: "stack overflow",
    0xC0000409: "stack buffer overrun",
}


def as_summary(line: str) -> str | None:
    """``line`` without colour codes if it is a pytest summary line, else None."""
    plain = _ANSI.sub("", line).strip()
    return plain if _SUMMARY.match(plain) else None


def summary_line(lines: Iterable[str]) -> str | None:
    """The last pytest summary line in ``lines``, or None if there is none."""
    found = None
    for line in lines:
        found = as_summary(line) or found
    return found


def is_green(summary: str) -> bool:
    """True if a summary line reports no failed test and no error."""
    match = _SUMMARY.match(_ANSI.sub("", summary).strip())
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
    # An NTSTATUS code such as 0xC0000005 is >= 2**31, which sys.exit cannot
    # take on Windows; its signed 32-bit form exits with the same code.
    status = returncode - 2**32 if returncode >= 2**31 else returncode
    crash = _WINDOWS_CRASHES.get(returncode & 0xFFFFFFFF)
    if crash is not None:
        return status, f"crashed ({crash}, 0x{returncode & 0xFFFFFFFF:08X})"
    return status, f"exit status {returncode}"


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
    # Remember the last summary as it streams past: a crash dump printed after
    # it can be arbitrarily long, so a fixed tail buffer could lose it.
    summary = None
    half_imported = False
    after_sep = False
    out = getattr(sys.stdout, "buffer", None)
    with subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    ) as proc:
        try:
            for raw in proc.stdout or ():
                # Pass bytes through untouched: re-encoding decoded text would
                # fail on a Windows console that is not UTF-8.
                text = raw.decode("utf-8", errors="replace")
                if out is not None:
                    out.write(raw)
                else:
                    sys.stdout.write(text)
                sys.stdout.flush()
                summary = as_summary(text) or summary
                plain = _ANSI.sub("", text)
                if after_sep and plain.startswith(_HALF_IMPORT_MARK):
                    half_imported = True
                after_sep = bool(_HALF_IMPORT_SEP.search(plain.rstrip()))
        except KeyboardInterrupt:
            try:
                proc.wait(timeout=_INTERRUPT_GRACE_S)
            except subprocess.TimeoutExpired:
                if sys.platform == "win32":
                    proc.terminate()
                else:
                    proc.send_signal(signal.SIGINT)
                proc.wait()
            sys.stderr.write("\npytest-exit-guard: interrupted.\n")
            return 130
    if proc.returncode == 0:
        return 0
    status, how = exit_status(proc.returncode)
    if half_imported:
        sys.stderr.write(
            f"\npytest-exit-guard: FAILED - tests/conftest.py found a half-imported "
            f"package (see 'half-imported packages' above; {how}).\n"
        )
    elif summary is not None and is_green(summary):
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
