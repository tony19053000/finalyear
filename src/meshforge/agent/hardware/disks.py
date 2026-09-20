"""Fixed-disk inventory and usage (roadmap 0.3.3, reused by the 0.3.5 monitor).

Only *fixed* filesystems are reported: a scheduler places work where there is
durable free space, so pseudo filesystems (``tmpfs``, ``overlay``, ``squashfs``,
``devtmpfs``), snap/loop mounts, network mounts (``nfs``, ``cifs``) and removable
or optical drives are filtered out. On Windows that leaves the fixed drive roots
(``C:\\``), on Linux the real mount points.

The mount *list* is discovered once (it changes rarely and enumerating it can
block on a stale network mount); the *usage* of those mounts is what the resource
monitor re-reads every couple of seconds, which is a cheap ``statvfs`` per mount.
A mount that has since disappeared is skipped, never an exception.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from typing import Final

from meshforge.agent.hardware.limits import MAX_DISKS, MOUNT_MAX_LENGTH
from meshforge.agent.hardware.optional import psutil_module
from meshforge.agent.hardware.probe import Budget, ProbeOutcome, clip, guarded

__all__ = [
    "DISKS_PROBE_CAP_S",
    "DiskFacts",
    "disk_usage",
    "is_fixed_filesystem",
    "list_mounts",
    "probe_disks",
]

DISKS_PROBE_CAP_S: Final = 2.0
"""Cap for the mount enumeration (a stale network mount can block ``statvfs``)."""

_PSEUDO_FSTYPES: Final[frozenset[str]] = frozenset(
    {
        "autofs",
        "binfmt_misc",
        "cgroup",
        "cgroup2",
        "configfs",
        "debugfs",
        "devfs",
        "devtmpfs",
        "efivarfs",
        "fuse.gvfsd-fuse",
        "fuse.portal",
        "fuse.snapfuse",
        "fusectl",
        "hugetlbfs",
        "iso9660",
        "mqueue",
        "overlay",
        "proc",
        "pstore",
        "ramfs",
        "securityfs",
        "squashfs",
        "sysfs",
        "tmpfs",
        "tracefs",
        "udf",
    }
)
_NETWORK_FSTYPES: Final[frozenset[str]] = frozenset(
    {"9p", "afs", "cifs", "fuse.sshfs", "nfs", "nfs4", "smb2", "smbfs", "sshfs"}
)
_EXCLUDED_OPTS: Final[frozenset[str]] = frozenset({"cdrom", "removable"})
_EXCLUDED_PREFIXES: Final[tuple[str, ...]] = ("/snap/", "/var/lib/docker/")


@dataclass(frozen=True, slots=True)
class DiskFacts:
    """One fixed filesystem. ``mount`` is already clipped to the wire cap."""

    mount: str
    fstype: str
    total_bytes: int
    free_bytes: int


def is_fixed_filesystem(
    *, device: str, mountpoint: str, fstype: str, opts: str
) -> bool:
    """True for a filesystem worth reporting (see the module docstring's rules)."""
    kind = fstype.strip().lower()
    if kind in _PSEUDO_FSTYPES or kind in _NETWORK_FSTYPES or not kind:
        return False
    if "loop" in device.lower():
        return False
    options = {opt.strip().lower() for opt in opts.split(",")}
    if options & _EXCLUDED_OPTS:
        return False
    normalised = mountpoint.replace("\\", "/")
    return not normalised.startswith(_EXCLUDED_PREFIXES)


def _root_mount() -> str:
    """The one mount every platform has, for a base install without psutil."""
    return os.path.abspath(os.sep)


def list_mounts() -> list[str]:
    """Fixed mount points, sorted and capped; never empty (the root is the floor)."""
    psutil = psutil_module()
    mounts: list[str] = []
    if psutil is not None:
        try:
            partitions = psutil.disk_partitions(all=False)
        except (OSError, RuntimeError):
            partitions = []
        mounts.extend(
            clip(str(part.mountpoint), MOUNT_MAX_LENGTH)
            for part in partitions
            if is_fixed_filesystem(
                device=str(part.device),
                mountpoint=str(part.mountpoint),
                fstype=str(part.fstype),
                opts=str(part.opts),
            )
        )
    if not mounts:
        mounts = [clip(_root_mount(), MOUNT_MAX_LENGTH)]
    return sorted(set(mounts))[:MAX_DISKS]


def disk_usage(mounts: list[str]) -> list[DiskFacts]:
    """Current total/free bytes of ``mounts``; a vanished mount is skipped."""
    out: list[DiskFacts] = []
    for mount in mounts:
        try:
            usage = shutil.disk_usage(mount)
        except (OSError, ValueError):
            continue
        out.append(
            DiskFacts(
                mount=mount, fstype="", total_bytes=usage.total, free_bytes=usage.free
            )
        )
    return out


def _collect(_timeout_s: float) -> list[DiskFacts]:
    return disk_usage(list_mounts())


def probe_disks(
    budget: Budget, *, cap_s: float = DISKS_PROBE_CAP_S
) -> ProbeOutcome[list[DiskFacts]]:
    """Fixed filesystems with their current usage."""
    return guarded("disks", budget, cap_s, _collect)
