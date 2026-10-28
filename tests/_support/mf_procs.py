"""Is a process gone? One answer for every test (V1.0 G7).

A killed process passes through ``running -> zombie -> dead ('X') -> no such
process``. ``'X'`` is the short window while its parent (or init) reaps it: the
``/proc/<pid>/stat`` entry still exists and psutil reports ``STATUS_DEAD``. A
helper that counts only "zombie or missing" as gone answers *gone*, then *alive*
(``dead``), then *gone* again for one process that was killed exactly once - the
CI flake of ``test_grandchildren_are_swept_even_after_the_child_exits_by_itself``
(run 40751531658, measured locally: 21 flips in 300 kills). Use :func:`pid_gone`
for a single-shot "it is gone" assertion.
"""

from __future__ import annotations

import psutil  # type: ignore[import-untyped]

__all__ = ["GONE_STATUSES", "pid_gone"]

#: States of a process that has exited and is only waiting to be (or being) reaped.
GONE_STATUSES = frozenset({psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD})


def pid_gone(pid: int) -> bool:
    """True once ``pid`` has exited: missing, zombie, or being reaped (``dead``)."""
    try:
        return psutil.Process(pid).status() in GONE_STATUSES
    except psutil.NoSuchProcess:  # includes psutil.ZombieProcess
        return True
