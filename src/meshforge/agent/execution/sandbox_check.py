"""The sandbox, asserted on what Docker actually created (V0.7 G5, 0.7.9, D143).

:class:`~.docker_runtime.DockerRuntime` builds every container with the hardening
in its module docstring. Building it right is not proof that the daemon applied
it (a daemon option, an authorization plugin or a future code change can drop or
add something), so **every container is inspected after ``create`` and before
``start``**: :func:`check_host_config` reads its ``HostConfig`` and raises
:class:`SandboxViolationError` for the first gap. The runtime then removes the
container unstarted and the attempt fails ``config_error`` with reason
``sandbox_violation`` - it never runs "almost sandboxed".

Asserted for every role (``run``, ``setup``, ``envbuild``):

* not privileged; no added capabilities and ``CapDrop`` holds ``ALL``;
* ``no-new-privileges`` set; no security option that turns a confinement off
  (``seccomp=unconfined``, ``apparmor=unconfined``, ``label=disable``,
  ``systempaths=unconfined``) - the default seccomp profile stays on (the
  daemon's own profile is checked by the probe: :func:`daemon_seccomp_problem`);
* ``/proc`` stays masked: ``MaskedPaths`` non-empty and ``/proc/sysrq-trigger``
  read-only;
* host namespaces never shared (PID, IPC, UTS, user namespace);
* ``PidsLimit`` > 0 and a memory limit set;
* ``/tmp`` is a tmpfs with a ``size=`` cap;
* **mounts**: exactly the expected binds (``run``/``setup``: the attempt
  workdir at ``/work``; ``envbuild``: the requirements staging and the
  wheelhouse, read-only) - no Docker socket, no other host path, no volumes,
  no legacy ``Binds``, no devices;
* the root filesystem read-only (all roles except ``envbuild``, whose layer
  receives the packages by design);
* ``run``: the network is ``none`` unless the plan allowed one (manifest
  ``limits.network: on`` **and** the owner's ``allow_network``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Final

__all__ = [
    "ExpectedMount",
    "SandboxViolationError",
    "check_host_config",
    "daemon_seccomp_problem",
]

_UNCONFINED: Final = (
    "seccomp=unconfined",
    "seccomp:unconfined",
    "apparmor=unconfined",
    "apparmor:unconfined",
    "label=disable",
    "label:disable",
    "systempaths=unconfined",
    "systempaths:unconfined",
)
_NO_NEW_PRIVILEGES: Final = frozenset(
    {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
)
_SHARED_HOST: Final = ("PidMode", "IpcMode", "UTSMode", "UsernsMode")
_TMP: Final = "/tmp"  # noqa: S108 - the container's own tmpfs, not a host path


class SandboxViolationError(Exception):
    """The created container is not the sandbox we asked for. ``what`` is a
    short stable code (``mounts``, ``seccomp``, ``network``...)."""

    def __init__(self, what: str, detail: str) -> None:
        super().__init__(f"{what}: {detail}")
        self.what = what
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ExpectedMount:
    source: str
    target: str
    read_only: bool


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list | tuple) else []


def _check_security(host: Mapping[str, Any]) -> None:
    if host.get("Privileged") is not False:
        raise SandboxViolationError("privileged", "the container is privileged")
    if _list(host.get("CapAdd")):
        raise SandboxViolationError("capabilities", "capabilities were added")
    if "ALL" not in {str(c).upper() for c in _list(host.get("CapDrop"))}:
        raise SandboxViolationError("capabilities", "CapDrop does not hold ALL")
    options = [str(o).strip().lower() for o in _list(host.get("SecurityOpt"))]
    if not _NO_NEW_PRIVILEGES.intersection(options):
        raise SandboxViolationError("no_new_privileges", "no-new-privileges is not set")
    for option in options:
        if option.replace(" ", "") in _UNCONFINED or option.startswith(
            ("seccomp=unconfined", "seccomp:unconfined")
        ):
            raise SandboxViolationError(
                "seccomp", f"a confinement is turned off ({option})"
            )
    if not _list(host.get("MaskedPaths")):
        raise SandboxViolationError("proc", "/proc is not masked (systempaths)")
    if "/proc/sysrq-trigger" not in _list(host.get("ReadonlyPaths")):
        raise SandboxViolationError("proc", "/proc/sysrq-trigger is not read-only")
    for key in _SHARED_HOST:
        if str(host.get(key) or "").lower() == "host":
            raise SandboxViolationError("namespaces", f"{key} shares the host's")
    if _list(host.get("Devices")):
        raise SandboxViolationError("devices", "host devices are mapped in")


def _check_limits(host: Mapping[str, Any]) -> None:
    pids = host.get("PidsLimit")
    if not isinstance(pids, int) or pids <= 0:
        raise SandboxViolationError("pids_limit", "no process limit")
    memory = host.get("Memory")
    if not isinstance(memory, int) or memory <= 0:
        raise SandboxViolationError("memory_limit", "no memory limit")
    tmpfs = host.get("Tmpfs")
    tmp = tmpfs.get(_TMP) if isinstance(tmpfs, Mapping) else None
    if not isinstance(tmp, str) or "size=" not in tmp:
        raise SandboxViolationError("tmpfs", "/tmp is not a size-capped tmpfs")  # noqa: S108


def _check_mounts(host: Mapping[str, Any], expected: Iterable[ExpectedMount]) -> None:
    if _list(host.get("Binds")) or _list(host.get("VolumesFrom")):
        raise SandboxViolationError("mounts", "legacy binds or volumes-from are set")
    want = {(m.source, m.target, m.read_only) for m in expected}
    seen: set[tuple[str, str, bool]] = set()
    for mount in _list(host.get("Mounts")):
        if not isinstance(mount, Mapping) or mount.get("Type") != "bind":
            raise SandboxViolationError("mounts", "a mount that is not a bind")
        found = (
            str(mount.get("Source", "")),
            str(mount.get("Target", "")),
            bool(mount.get("ReadOnly", False)),
        )
        if found not in want:
            raise SandboxViolationError(
                "mounts", f"an unexpected mount at {found[1] or '?'}"
            )
        seen.add(found)
    if seen != want:
        raise SandboxViolationError("mounts", "an expected mount is missing")


def check_host_config(
    host: Mapping[str, Any],
    *,
    role: str,
    mounts: Iterable[ExpectedMount],
    network_allowed: bool,
) -> None:
    """Raise :class:`SandboxViolationError` unless ``host`` (``HostConfig`` of an
    inspected, not yet started container) is our sandbox (module docstring).
    ``network_allowed``: the ``run`` container may have a network."""
    if not isinstance(host, Mapping):
        raise SandboxViolationError("inspect", "no HostConfig to check")
    _check_security(host)
    _check_limits(host)
    _check_mounts(host, mounts)
    if role != "envbuild" and host.get("ReadonlyRootfs") is not True:
        raise SandboxViolationError("read_only", "the root filesystem is writable")
    if role == "run" and not network_allowed and host.get("NetworkMode") != "none":
        raise SandboxViolationError(
            "network", "the job has a network it was not allowed"
        )


def daemon_seccomp_problem(info: Mapping[str, Any]) -> str | None:
    """None when the daemon confines containers with a seccomp profile by
    default (``docker info`` ``SecurityOptions`` lists ``name=seccomp`` with a
    profile other than ``unconfined``); else the probe's reason code."""
    options = [str(o).lower() for o in _list(info.get("SecurityOptions"))]
    seccomp = [o for o in options if "name=seccomp" in o]
    if not seccomp:
        return "docker_seccomp_off"
    if any("profile=unconfined" in o for o in seccomp):
        return "docker_seccomp_unconfined"
    return None
