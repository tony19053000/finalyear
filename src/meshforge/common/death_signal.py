"""Linux parent-death signal for child programs (shared by agent and controller).

A child must not outlive the MeshForge process that started it when that process
is SIGKILLed or OOM-killed. On Linux, ``prctl(PR_SET_PDEATHSIG, sig)`` asks the
kernel to send ``sig`` to the child when its parent dies. It must be armed *in the
child* before ``execve``, and ``preexec_fn`` is unsafe in a threaded asyncio
process, so a tiny exec shim does it: ``python -I -S -c <shim> ...`` arms the
signal, checks the parent did not already die (the race before ``prctl``), then
``execve``s the real program - same pid, so the caller's pid *is* the program's.

Two users, two signals:

* the agent (:mod:`meshforge.agent.process`) arms **SIGKILL** for workloads and
  embeds :data:`ARM_SNIPPET` in its larger shim (process-group watchdog,
  resource limits, cgroup join);
* the controller's managed PostgreSQL (:mod:`meshforge.controller.db_managed`)
  arms **SIGINT** = PostgreSQL's *fast shutdown* through :func:`with_death_signal`
  (a SIGKILLed postmaster would leave backends and shared memory behind).

The death signal fires when the **thread** that forked exits, so start such
children from the event-loop thread, never from ``asyncio.to_thread``. It survives
``execve`` (except for set-uid programs; none of ours are). Exit codes of the
shim: 125 = could not arm, 137 = the parent was already gone.

Stdlib only; importable on every platform (the shim is only used on Linux).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from typing import Final

__all__ = [
    "ARM_SNIPPET",
    "EXIT_CANNOT_ARM",
    "EXIT_PARENT_GONE",
    "SIMPLE_SHIM",
    "death_signal_supported",
    "with_death_signal",
]

EXIT_CANNOT_ARM: Final = 125
EXIT_PARENT_GONE: Final = 137

ARM_SNIPPET: Final = r"""
try:
    if ctypes.CDLL(None, use_errno=True).prctl(1, sig, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl(PR_SET_PDEATHSIG) failed")
except (OSError, AttributeError) as exc:
    sys.stderr.write("meshforge: cannot arm the parent-death signal: %s\n" % exc)
    os._exit(125)
if os.getppid() != ppid:
    os._exit(137)
"""
"""Arms ``PR_SET_PDEATHSIG`` (1) with ``sig`` and checks ``ppid``. Expects the
names ``ctypes``, ``os``, ``sys``, ``ppid`` (int) and ``sig`` (int) to exist."""

SIMPLE_SHIM: Final = (
    "import ctypes, os, sys\n"
    "ppid, sig, exe, argv = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4:]"
    + ARM_SNIPPET
    + "os.execv(exe, argv)\n"
)
"""The whole shim: argv = parent pid, signal number, executable, *argv."""


def death_signal_supported(platform: str = sys.platform) -> bool:
    """True where :func:`with_death_signal` wraps the command (Linux)."""
    return platform.startswith("linux")


def with_death_signal(
    argv: Sequence[str],
    *,
    parent_pid: int,
    signum: int,
    python: str | None = None,
    platform: str = sys.platform,
) -> list[str]:
    """``argv`` wrapped in :data:`SIMPLE_SHIM` on Linux; unchanged elsewhere.

    ``argv[0]`` must be an absolute path (the shim ``execv``s it, no PATH
    lookup). ``python`` defaults to this interpreter.
    """
    if not death_signal_supported(platform):
        return list(argv)
    return [
        python or sys.executable,
        "-I",
        "-S",
        "-c",
        SIMPLE_SHIM,
        str(parent_pid),
        str(int(signum)),
        argv[0],
        *argv,
    ]
