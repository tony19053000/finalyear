"""Failure classification from evidence a runtime alone cannot see (V0.6 G1,
roadmap 0.6.10, D116).

Retry decisions (V0.6 G3) and the ML planner's labels (V0.15) are only as good
as the ``failure_type`` an ending gets, so every mapping here is driven by
evidence, never by a guess:

* **``vram_oom``** - the workload exited non-zero and the **tail of its
  stderr** (the last :data:`STDERR_TAIL_BYTES`, kept in memory as it streams)
  contains a CUDA out-of-memory marker (:data:`VRAM_OOM_MARKER`:
  ``CUDA out of memory``, ``torch.OutOfMemoryError`` /
  ``torch.cuda.OutOfMemoryError``, ``CUDA error: out of memory``). Retryable:
  another node may have more VRAM.
* **``oom``** (host RAM) - the workload exited non-zero and the last line of
  its stderr is a Python ``MemoryError`` (an allocation refused: an ``RLIMIT_AS``,
  a Windows job-object memory limit, a full commit charge); or it died of
  ``SIGKILL`` that the agent did not send while the kernel's ``oom_kill``
  counter moved (:class:`OomEvidence`); or it exited with Windows
  ``STATUS_NO_MEMORY`` / ``STATUS_COMMITMENT_LIMIT``. The Docker runtime reads
  the container's own ``OOMKilled`` flag instead.
* **``disk_full``** - ``ENOSPC``/``EDQUOT`` anywhere in an exception's chain
  (:func:`disk_full_cause`) while staging, building the env or collecting the
  outputs, or the env installer reporting it (:data:`DISK_FULL_MARKER`).

Workload output is user data: only a match/no-match decision leaves this
module, never the bytes.
"""

from __future__ import annotations

import errno
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

from meshforge.agent.execution.runtime import RunResult
from meshforge.common.errors import FailureType

__all__ = [
    "DISK_FULL_ERRNOS",
    "DISK_FULL_MARKER",
    "STDERR_TAIL_BYTES",
    "VRAM_OOM_MARKER",
    "WINDOWS_OOM_EXIT_CODES",
    "OomEvidence",
    "OutputTail",
    "disk_full_cause",
    "refine_result",
    "windows_oom_exit",
]

STDERR_TAIL_BYTES: Final = 64 * 1024
"""How much of the end of a workload's stderr is examined (D116)."""
VRAM_OOM_MARKER: Final = re.compile(
    rb"CUDA out of memory|torch\.(?:cuda\.)?OutOfMemoryError|CUDA error: out of memory"
)
_MEMORY_ERROR: Final = re.compile(rb"^MemoryError\b")
DISK_FULL_MARKER: Final = re.compile(
    rb"No space left on device|Disk quota exceeded|\(os error 28\)|\[Errno 28\]"
)
DISK_FULL_ERRNOS: Final = frozenset(
    code for code in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if code is not None
)
WINDOWS_OOM_EXIT_CODES: Final = frozenset({0xC0000017, 0xC000012D})
"""``STATUS_NO_MEMORY`` and ``STATUS_COMMITMENT_LIMIT`` as Windows exit codes."""
_VMSTAT: Final = Path("/proc/vmstat")


class OutputTail:
    """The last ``limit`` bytes of one output stream (bounded, in memory)."""

    __slots__ = ("_buf", "_limit")

    def __init__(self, limit: int = STDERR_TAIL_BYTES) -> None:
        self._limit = limit
        self._buf = bytearray()

    def add(self, data: bytes) -> None:
        if len(data) >= self._limit:
            self._buf[:] = data[-self._limit :]
            return
        self._buf += data
        excess = len(self._buf) - self._limit
        if excess > 0:
            del self._buf[:excess]

    @property
    def data(self) -> bytes:
        return bytes(self._buf)

    def clear(self) -> None:
        """Forget what was kept (V0.10 G4: a lane's next chunk starts afresh)."""
        self._buf.clear()


def _last_line(data: bytes) -> bytes:
    for line in reversed(data.splitlines()):
        if line.strip():
            return line.strip()
    return b""


def refine_result(result: RunResult, stderr_tail: bytes) -> RunResult:
    """``result`` with a more precise failure type when the evidence says so.

    Only a ``user_error`` (the workload exited non-zero / died by a signal) is
    refined: a CUDA OOM marker -> ``vram_oom``; a final ``MemoryError`` line ->
    ``oom``. Everything else is returned unchanged."""
    if result.failure_type is not FailureType.USER_ERROR:
        return result
    if VRAM_OOM_MARKER.search(stderr_tail):
        return replace(
            result,
            failure_type=FailureType.VRAM_OOM,
            reason="cuda_out_of_memory",
            detail=f"GPU memory exhausted (CUDA out of memory); {result.detail}",
        )
    if _MEMORY_ERROR.match(_last_line(stderr_tail)):
        return replace(
            result,
            failure_type=FailureType.OOM,
            oom_killed=True,
            reason="memory_error",
            detail=f"host memory exhausted (MemoryError); {result.detail}",
        )
    return result


def windows_oom_exit(returncode: int | None) -> bool:
    """A Windows exit status that means "out of memory" (never one on POSIX)."""
    return returncode is not None and (returncode & 0xFFFFFFFF) in (
        WINDOWS_OOM_EXIT_CODES
    )


def disk_full_cause(exc: BaseException | None) -> OSError | None:
    """The ``ENOSPC``/``EDQUOT`` :class:`OSError` in ``exc``'s chain, if any."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and exc.errno in DISK_FULL_ERRNOS:
            return exc
        exc = exc.__cause__ or exc.__context__
    return None


def _read_oom_kills(path: Path = _VMSTAT) -> int | None:
    try:
        text = path.read_text(encoding="ascii", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name == "oom_kill":
            try:
                return int(value)
            except ValueError:
                return None
    return None


@dataclass(frozen=True, slots=True)
class OomEvidence:
    """The kernel's OOM-kill counter before a run (Linux ``/proc/vmstat``).

    :meth:`fired` after the run says whether the kernel killed *some* process
    for memory meanwhile; combined with "the workload died of a SIGKILL the
    agent did not send" that is the evidence for ``oom`` (D116). A machine
    without the counter (macOS, Windows, old kernels) never gives it."""

    before: int | None
    read: Callable[[], int | None] = _read_oom_kills

    @classmethod
    def snapshot(cls, read: Callable[[], int | None] | None = None) -> OomEvidence:
        reader = read or (
            _read_oom_kills if sys.platform.startswith("linux") else _none
        )
        return cls(reader(), reader)

    def fired(self) -> bool:
        if self.before is None:
            return False
        after = self.read()
        return after is not None and after > self.before


def _none() -> int | None:
    return None
