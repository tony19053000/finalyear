"""The sandbox, asserted on what Docker actually created (V0.7 G5, 0.7.9, D143).

:class:`~.docker_runtime.DockerRuntime` builds every container with the hardening
in its module docstring. Building it right is not proof that the daemon applied
it (a daemon option, an authorization plugin or a future code change can drop or
add something), so **every container is inspected after ``create`` and before
``start``**: :func:`check_host_config` reads its ``HostConfig`` and raises
:class:`SandboxViolationError` for the first gap. The runtime then removes the
container unstarted and the attempt fails ``config_error`` with reason
``sandbox_violation`` - it never runs "almost sandboxed".

The check is an **allow-list** (fix round 1, D160): a field that widens the
box must hold one of the values the runtime itself sets, not merely "not the
known bad one". Asserted for every role (``run``, ``setup``, ``envbuild``):

* not privileged; no added capabilities and ``CapDrop`` holds ``ALL``;
* ``SecurityOpt`` holds ``no-new-privileges`` and **nothing else**: no
  ``seccomp=`` at all (neither ``unconfined`` nor a custom allow-all profile -
  only the daemon's default profile, which the probe checks:
  :func:`daemon_seccomp_problem`), no ``apparmor=``/``label=``/
  ``systempaths=`` option;
* ``/proc`` stays masked: ``MaskedPaths`` non-empty and ``/proc/sysrq-trigger``
  read-only;
* namespaces private: ``PidMode``/``UTSMode``/``UsernsMode`` empty (the
  container's own), ``IpcMode`` ``private``/``shareable``/``none``/empty,
  ``CgroupnsMode`` ``private`` (or empty on a daemon that does not report it) -
  never ``host`` nor ``container:<id>``;
* ``NetworkMode`` only ``none`` or the default bridge - never ``host`` or
  ``container:<id>``;
* ``PidsLimit`` > 0; ``Memory`` **and** ``MemorySwap`` equal to the planned cap
  (no swap, no larger box); ``OomKillDisable`` not set;
* ``Tmpfs`` holds only ``/tmp``, with exactly the planned options (size
  included);
* no ``Sysctls``, no ``PortBindings``, no ``GroupAdd``;
* **mounts**: exactly the expected binds (``run``/``setup``: the attempt
  workdir at ``/work``; ``envbuild``: the requirements staging and the
  wheelhouse, read-only) - no Docker socket, no other host path, no volumes,
  no legacy ``Binds``, no devices;
* the create request itself holds only the options the runtime uses
  (:func:`check_create_options`; checked last);
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
    "ExpectedLimits",
    "ExpectedMount",
    "SandboxViolationError",
    "check_create_options",
    "check_host_config",
    "daemon_seccomp_problem",
]

_NO_NEW_PRIVILEGES: Final = frozenset(
    {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
)
_NAMESPACES: Final[Mapping[str, frozenset[str]]] = {
    "PidMode": frozenset({""}),
    "IpcMode": frozenset({"", "private", "shareable", "none"}),
    "UTSMode": frozenset({""}),
    "UsernsMode": frozenset({""}),
    "CgroupnsMode": frozenset({"", "private"}),
}
"""The only namespace modes a container may have (each key absent = ``""``)."""
_NETWORKS: Final = frozenset({"none", "bridge", "default"})
"""``default`` is how some daemons (Docker Desktop) name the default bridge."""
_MUST_BE_EMPTY: Final = ("Sysctls", "PortBindings", "GroupAdd")
CREATE_OPTIONS: Final = frozenset(
    {
        "command",
        "name",
        "hostname",
        "user",
        "working_dir",
        "environment",
        "labels",
        "mounts",
        "cap_drop",
        "security_opt",
        "read_only",
        "tmpfs",
        "shm_size",
        "pids_limit",
        "mem_limit",
        "memswap_limit",
        "network_mode",
        "cgroupns",
        "init",
        "log_config",
        "stdin_open",
        "tty",
        "nano_cpus",
        "device_requests",
    }
)
"""The only ``containers.create`` options the runtime uses (:func:`check_create_options`)."""
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


@dataclass(frozen=True, slots=True)
class ExpectedLimits:
    """What the plan asked for: ``Memory`` and ``MemorySwap`` must both be
    ``memory_bytes``; ``/tmp`` must be a tmpfs with exactly ``tmp_options``.
    T-10: when given, ``PidsLimit`` must equal ``pids`` and ``NanoCpus`` must
    equal ``nano_cpus`` (0 = no CPU cap); ``DeviceRequests`` may only name
    exactly ``gpu_ids`` (empty = no device request at all)."""

    memory_bytes: int
    tmp_options: str
    pids: int | None = None
    nano_cpus: int | None = None
    gpu_ids: tuple[str, ...] = ()


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
        if option not in _NO_NEW_PRIVILEGES:
            # seccomp=unconfined, a custom seccomp profile, apparmor=..., label=...,
            # systempaths=...: only the daemon's defaults are trusted.
            raise SandboxViolationError(
                "seccomp",
                f"a security option replaces a default confinement "
                f"({option.partition('=')[0].partition(':')[0]})",
            )
    if not _list(host.get("MaskedPaths")):
        raise SandboxViolationError("proc", "/proc is not masked (systempaths)")
    if "/proc/sysrq-trigger" not in _list(host.get("ReadonlyPaths")):
        raise SandboxViolationError("proc", "/proc/sysrq-trigger is not read-only")
    for key, allowed in _NAMESPACES.items():
        value = host.get(key)
        if value is None:
            value = ""
        if not isinstance(value, str) or value.lower() not in allowed:
            raise SandboxViolationError(
                "namespaces", f"{key} is {str(value)[:40]!r}, not a private namespace"
            )
    if _list(host.get("Devices")):
        raise SandboxViolationError("devices", "host devices are mapped in")
    for key in _MUST_BE_EMPTY:
        if host.get(key):
            raise SandboxViolationError("host_config", f"{key} is set")
    if host.get("OomKillDisable") not in (None, False):
        raise SandboxViolationError("memory_limit", "the OOM killer is disabled")


def _options(text: str) -> frozenset[str]:
    return frozenset(o.strip() for o in text.split(",") if o.strip())


def _check_devices(host: Mapping[str, Any], gpu_ids: tuple[str, ...]) -> None:
    requests = _list(host.get("DeviceRequests"))
    if not gpu_ids:
        if requests:
            raise SandboxViolationError(
                "devices", "a device request on a plan without GPUs"
            )
        return
    named: list[str] = []
    for req in requests:
        ids = req.get("DeviceIDs") if isinstance(req, Mapping) else None
        if not isinstance(ids, list) or req.get("Count") not in (None, 0):
            raise SandboxViolationError("devices", "a device request without exact ids")
        named.extend(str(i) for i in ids)
    if sorted(named) != sorted(gpu_ids):
        raise SandboxViolationError(
            "devices", f"devices {named[:8]!r}, the plan says {list(gpu_ids)!r}"
        )


def _check_limits(host: Mapping[str, Any], limits: ExpectedLimits | None) -> None:
    pids = host.get("PidsLimit")
    if not isinstance(pids, int) or isinstance(pids, bool) or pids <= 0:
        raise SandboxViolationError("pids_limit", "no process limit")
    if limits is not None and limits.pids is not None and pids != limits.pids:
        raise SandboxViolationError(
            "pids_limit", f"PidsLimit is {pids}, the plan says {limits.pids}"
        )
    if limits is not None and limits.nano_cpus is not None:
        cpus = host.get("NanoCpus") or 0
        if cpus != limits.nano_cpus:
            raise SandboxViolationError(
                "cpu_limit", f"NanoCpus is {cpus}, the plan says {limits.nano_cpus}"
            )
    if limits is not None:
        _check_devices(host, limits.gpu_ids)
    memory = host.get("Memory")
    if not isinstance(memory, int) or isinstance(memory, bool) or memory <= 0:
        raise SandboxViolationError("memory_limit", "no memory limit")
    if limits is not None and memory != limits.memory_bytes:
        raise SandboxViolationError(
            "memory_limit", f"Memory is {memory}, the plan says {limits.memory_bytes}"
        )
    if host.get("MemorySwap") != memory:
        raise SandboxViolationError(
            "memory_limit", "MemorySwap differs from Memory (swap or a larger box)"
        )
    tmpfs = host.get("Tmpfs")
    if not isinstance(tmpfs, Mapping) or set(tmpfs) != {_TMP}:
        raise SandboxViolationError("tmpfs", "Tmpfs must hold /tmp and nothing else")
    tmp = tmpfs.get(_TMP)
    if not isinstance(tmp, str) or not any(
        o.startswith("size=") for o in _options(tmp)
    ):
        raise SandboxViolationError("tmpfs", "/tmp is not a size-capped tmpfs")  # noqa: S108
    if limits is not None and _options(tmp) != _options(limits.tmp_options):
        raise SandboxViolationError(
            "tmpfs",
            f"the /tmp options are {tmp[:80]!r}, the plan says {limits.tmp_options!r}",
        )


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
    limits: ExpectedLimits | None = None,
) -> None:
    """Raise :class:`SandboxViolationError` unless ``host`` (``HostConfig`` of an
    inspected, not yet started container) is our sandbox (module docstring).
    ``network_allowed``: the ``run`` container may have a network.
    ``limits``: the planned memory cap and ``/tmp`` options - the runtime always
    passes them; without them only the shape is checked (``MemorySwap`` equal to
    ``Memory``, ``/tmp`` the only tmpfs and sized)."""
    if not isinstance(host, Mapping):
        raise SandboxViolationError("inspect", "no HostConfig to check")
    _check_security(host)
    _check_limits(host, limits)
    _check_mounts(host, mounts)
    if role != "envbuild" and host.get("ReadonlyRootfs") is not True:
        raise SandboxViolationError("read_only", "the root filesystem is writable")
    network = host.get("NetworkMode")
    if not isinstance(network, str) or network not in _NETWORKS:
        raise SandboxViolationError(
            "network", f"NetworkMode {str(network)[:40]!r} is not none or the bridge"
        )
    if role == "run" and not network_allowed and network != "none":
        raise SandboxViolationError(
            "network", "the job has a network it was not allowed"
        )


def check_create_options(options: Mapping[str, Any]) -> None:
    """Raise :class:`SandboxViolationError` (``create_options``) when the create
    request holds an option outside :data:`CREATE_OPTIONS`.

    Checked after the ``HostConfig`` check, before ``start``: some
    options are accepted by the API but not reported back (a cgroup v2 daemon
    ignores ``oom_kill_disable`` and inspects ``OomKillDisable: false``), so
    only the request shows that the runtime asked for more than its own set."""
    extra = sorted(set(options) - CREATE_OPTIONS)
    if extra:
        raise SandboxViolationError(
            "create_options", f"unexpected container options: {', '.join(extra)}"
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
