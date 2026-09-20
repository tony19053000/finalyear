"""Bounded subprocess calls for S6: argument lists only, a timeout on every
call, ``stdin`` closed, captured output capped, never ``shell=True``.

On timeout or Ctrl-C the child is killed before the exception propagates, so
no ``docker``/``uv`` process outlives the run. A *container* started by a
killed ``docker run`` client is not stopped by that kill; :mod:`.docker`
handles that with a ``--name`` and ``docker rm -f`` in its own ``finally``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

#: Captured stdout/stderr are cut here (a chatty pull log is not evidence).
MAX_OUTPUT_CHARS: Final = 20_000
_REAP_S: Final = 5.0


@dataclass(frozen=True, slots=True)
class Outcome:
    """One finished (or failed-to-run) command."""

    argv: tuple[str, ...]
    returncode: int | None
    stdout: str
    stderr: str
    elapsed_s: float
    timed_out: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and not self.timed_out and self.returncode == 0

    def describe(self) -> str:
        """Short failure text: the error, else stderr/stdout, else the code."""
        if self.error:
            return self.error
        if self.timed_out:
            return f"timed out after {self.elapsed_s:.0f} s"
        text = (self.stderr.strip() or self.stdout.strip())[:2000]
        return text or f"{Path(self.argv[0]).name} exited with code {self.returncode}"


def _decode(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace").replace("\x00", "")
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[: MAX_OUTPUT_CHARS // 2] + "\n...[truncated]...\n" + text[-2000:]
    return text


def _kill(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
        proc.communicate(timeout=_REAP_S)


def run(
    argv: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
) -> Outcome:
    """Run ``argv`` (``argv[0]`` must be an absolute path, see :func:`which`).

    Never raises for a failing or missing command: the :class:`Outcome` says
    what happened. ``KeyboardInterrupt`` kills the child and propagates.
    """
    if timeout <= 0:
        return Outcome(tuple(argv), None, "", "", 0.0, error="no time left")
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    creationflags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    )
    start = time.perf_counter()
    try:
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            env=full_env,
            creationflags=creationflags,
        )
    except (OSError, ValueError) as exc:
        return Outcome(
            tuple(argv),
            None,
            "",
            "",
            0.0,
            error=f"could not start: {exc.__class__.__name__}",
        )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(proc)
        return Outcome(
            tuple(argv), None, "", "", time.perf_counter() - start, timed_out=True
        )
    except BaseException:  # Ctrl-C: never leave the child behind, then re-raise
        _kill(proc)
        raise
    elapsed = time.perf_counter() - start
    return Outcome(tuple(argv), proc.returncode, _decode(out), _decode(err), elapsed)
