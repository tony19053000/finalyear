"""A real CPU share for the unsandboxed Linux runtimes: a cgroup v2 child per job.

V0.7 G6 item 0 (D161 tester follow-up; D142 item 5 "say it, never claim it").
Docker caps CPUs with ``--cpus``; the subprocess and venv runtimes on Linux
had only a lowered priority and advertised ``max_cpu_percent:unsandboxed_linux``
as not enforced. When the agent runs where it may manage cgroups, each job now
gets its own cgroup with ``cpu.max`` = its planned CPUs, and the cap is enforced
by the kernel.

**When it is used** (:func:`probe_cpu_cgroups`, once at start): Linux, the
unified cgroup v2 hierarchy, and the agent in a leaf cgroup named ``agent``
whose parent it may write and that offers the ``cpu`` controller - exactly what
the systemd user unit sets up with ``Delegate=yes`` + ``DelegateSubgroup=agent``
(``deploy/systemd/meshforge-agent.service``, systemd >= 254). The agent never
moves itself or other processes and never writes into a cgroup it was not
delegated: an agent started from a terminal (its cgroup is a session scope)
does not qualify. Otherwise the probe returns a typed reason and the cap stays
advertised as ``not_enforced`` - no silent claim.

**Per job** (:class:`CpuCgroups`): ``<parent>/mf-<attempt_id>`` is created
with ``cpu.max = <quota> <period>`` before the spawn; the exec shim moves
itself into it (``cgroup.procs``) right before ``execve`` - a shim that cannot
join fails the run (exit 125, like an rlimit it cannot set). After the run,
``cgroup.kill`` ends anything still in it (also a descendant that escaped the
process group with ``setsid``) and the folder is removed. Folders a crashed
agent left behind are swept at the next start.
"""

from __future__ import annotations

import contextlib
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from meshforge.common.logging import get_logger

__all__ = [
    "CGROUP_PREFIX",
    "LEAF_NAME",
    "PERIOD_US",
    "CpuCgroupProbe",
    "CpuCgroups",
    "cpu_max",
    "probe_cpu_cgroups",
]

log = get_logger(__name__)

CGROUP_ROOT: Final = Path("/sys/fs/cgroup")
SELF_CGROUP: Final = Path("/proc/self/cgroup")
LEAF_NAME: Final = "agent"
"""The agent's own leaf (systemd ``DelegateSubgroup=agent``)."""
CGROUP_PREFIX: Final = "mf-"
PERIOD_US: Final = 100_000
_MIN_QUOTA_US: Final = 1_000
_SAFE_PATH: Final = re.compile(r"[A-Za-z0-9_./@:\\-]+")
"""Characters the parent path may contain (it travels inside the shim's
``k=v;k=v`` limits argument, so never ``;`` or ``=``)."""
_RMDIR_TRIES: Final = 20
_RMDIR_WAIT_S: Final = 0.05


@dataclass(frozen=True, slots=True)
class CpuCgroupProbe:
    """The probe's answer: ``parent`` set = usable, else ``reason`` says why."""

    parent: Path | None
    reason: str | None = None


def cpu_max(cpus: float, *, period_us: int = PERIOD_US) -> str:
    """``cpu.max`` content for ``cpus`` CPUs (rounded up, never below 1 ms)."""
    if not cpus > 0:
        raise ValueError("cpus must be > 0")
    quota = max(_MIN_QUOTA_US, math.ceil(cpus * period_us))
    return f"{quota} {period_us}"


def _own_cgroup(self_file: Path) -> str | None:
    """The v2 path from ``/proc/self/cgroup`` (``0::/path``), else None."""
    try:
        text = self_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("0::"):
            return line[3:].strip() or None
    return None


def probe_cpu_cgroups(
    *,
    root: Path = CGROUP_ROOT,
    self_file: Path = SELF_CGROUP,
    platform: str = sys.platform,
) -> CpuCgroupProbe:
    """Can this agent give each job a ``cpu.max`` (module docstring)? Enables
    the ``cpu`` controller for the parent's children when it is not yet on.
    Never raises."""
    if not platform.startswith("linux"):
        return CpuCgroupProbe(None, "not_linux")
    if not (root / "cgroup.controllers").is_file():
        return CpuCgroupProbe(None, "no_cgroup_v2")
    own = _own_cgroup(self_file)
    if own is None or own == "/":
        return CpuCgroupProbe(None, "no_cgroup_v2")
    leaf = root / own.lstrip("/")
    if leaf.name != LEAF_NAME:
        return CpuCgroupProbe(None, "not_delegated")
    parent = leaf.parent
    if parent == root or not _SAFE_PATH.fullmatch(str(parent)):
        return CpuCgroupProbe(None, "not_delegated")
    try:
        controllers = (parent / "cgroup.controllers").read_text().split()
        if "cpu" not in controllers:
            return CpuCgroupProbe(None, "no_cpu_controller")
        if not os.access(parent, os.W_OK | os.X_OK):
            return CpuCgroupProbe(None, "not_writable")
        subtree = parent / "cgroup.subtree_control"
        if "cpu" not in subtree.read_text().split():
            subtree.write_text("+cpu\n")
            if "cpu" not in subtree.read_text().split():
                return CpuCgroupProbe(None, "cannot_enable_cpu")
    except OSError:
        return CpuCgroupProbe(None, "cannot_enable_cpu")
    return CpuCgroupProbe(parent)


class CpuCgroups:
    """Per-job cgroups under one delegated parent (module docstring)."""

    def __init__(self, parent: Path) -> None:
        self.parent = parent

    def path_for(self, attempt_id: str) -> Path:
        name = f"{CGROUP_PREFIX}{attempt_id.lower()}"
        if not _SAFE_PATH.fullmatch(name) or "/" in name or ".." in name:
            raise ValueError("attempt id unusable as a cgroup name")
        return self.parent / name

    def create(self, attempt_id: str, cpus: float) -> Path:
        """The job's cgroup with its ``cpu.max``. Raises OSError/ValueError
        (the caller refuses the run: the cap was promised)."""
        path = self.path_for(attempt_id)
        with contextlib.suppress(FileExistsError):
            path.mkdir()
        try:
            (path / "cpu.max").write_text(cpu_max(cpus) + "\n")
        except BaseException:
            self.remove(path)
            raise
        return path

    def remove(self, path: Path) -> bool:
        """Kill what is left in ``path`` and remove it (best effort, bounded:
        about one second). True when it is gone."""
        with contextlib.suppress(OSError):
            (path / "cgroup.kill").write_text("1\n")
        for _ in range(_RMDIR_TRIES):
            try:
                path.rmdir()
            except FileNotFoundError:
                return True
            except OSError:
                time.sleep(_RMDIR_WAIT_S)
            else:
                return True
        log.warning("agent.execution.cgroup_cleanup_failed", cgroup=path.name)
        return False

    def sweep(self) -> int:
        """Remove job cgroups a previous (crashed) agent left; returns how many."""
        removed = 0
        try:
            entries = list(self.parent.iterdir())
        except OSError:
            return 0
        for entry in entries:
            if entry.name.startswith(CGROUP_PREFIX) and entry.is_dir():
                removed += int(self.remove(entry))
        if removed:
            log.info("agent.execution.cgroups_swept", count=removed)
        return removed
