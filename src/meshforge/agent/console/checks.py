"""What this laptop can offer, checked **by the agent itself** (D388).

Reports, never gates: a laptop without Docker or a GPU still joins and runs CPU
jobs (subprocess/venv runtimes); a missing item only lowers what it offers, and
the check says why and how to fix it. Every check runs in the agent's own
process - on the system service that is the ``meshforge`` user, which is the
whole point: "Docker works in my terminal" says nothing about the service.

Each :class:`Check` is ``ok`` | ``warn`` | ``missing`` + one plain sentence
(``why``) + the fix (``fix``, may be empty) + an optional ``action`` the console
can offer (``docker-access``: the app-menu helper of D391/Q24; ``restart``).

Checks (each bounded; the slow ones in threads, all at once):

* ``docker`` - SDK, daemon reachable *from this process*, default seccomp
  (:func:`~meshforge.agent.execution.docker_support.probe_docker`, the same
  probe the runtime uses), and "added to the docker group but not restarted";
* ``gpu`` - the NVIDIA driver and ``nvidia-smi`` (names of the GPUs found);
* ``cuda`` - the service user can open ``/dev/nvidia*`` and the driver's CUDA
  version (torch is not in the agent: jobs bring their own);
* ``disk`` - free space where job environments and caches live;
* ``firewall`` - Linux: ``ufw`` on/off (``/etc/ufw/ufw.conf``, readable without
  root); other firewalls and Windows are said to be not checked;
* ``peer`` - the peer file server is off, or answers on its address.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final

from meshforge.agent.hardware.probe import run_command, which
from meshforge.agent.settings import AgentSettings
from meshforge.common.config import InstallMode, install_mode
from meshforge.common.logging import get_logger

__all__ = [
    "DOCKER_ACCESS_ACTION",
    "RESTART_ACTION",
    "Check",
    "CheckStatus",
    "run_checks",
]

log = get_logger(__name__)

DOCKER_ACCESS_ACTION: Final = "docker-access"
RESTART_ACTION: Final = "restart"
CHECK_TIMEOUT_S: Final = 8.0
_GIB: Final = 1024**3
DISK_WARN_BYTES: Final = 10 * _GIB
DISK_MISSING_BYTES: Final = 1 * _GIB
UFW_CONF: Final = Path("/etc/ufw/ufw.conf")
NVIDIA_PROC: Final = Path("/proc/driver/nvidia")
NVIDIA_CTL: Final = Path("/dev/nvidiactl")
_CUDA_RE: Final = re.compile(r"CUDA Version:\s*([0-9]+(?:\.[0-9]+)*)")
SERVICE_USER: Final = "meshforge"
DOCKER_ACCESS_COMMAND: Final = (
    "sudo usermod -aG docker meshforge && sudo systemctl restart meshforge-agent"
)


class CheckStatus(StrEnum):
    OK = "ok"
    WARN = "warn"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class Check:
    id: str
    title: str
    status: CheckStatus
    why: str
    fix: str = ""
    action: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "title": self.title,
            "status": self.status.value,
            "why": self.why,
            "fix": self.fix,
            "action": self.action,
        }


# ---------------------------------------------------------------------- docker


def _in_docker_group_on_disk() -> bool | None:
    """Is the service user listed in ``/etc/group``'s docker group? (None: no
    such group.) The running process only gets the group after a restart."""
    try:
        import grp
        import pwd
    except ImportError:  # pragma: no cover - not POSIX
        return None
    try:
        group = grp.getgrnam("docker")
    except KeyError:
        return None
    if os.getgid() == group.gr_gid or group.gr_gid in os.getgroups():
        return True
    with contextlib.suppress(KeyError):
        return pwd.getpwuid(os.getuid()).pw_name in group.gr_mem
    return False


def check_docker(settings: AgentSettings) -> Check:
    from meshforge.agent.execution.docker_support import probe_docker

    title = "Docker"
    socket_path = Path("/var/run/docker.sock")
    if which("docker") is None and not socket_path.exists():
        return Check(
            "docker",
            title,
            CheckStatus.MISSING,
            "Docker is not installed. Jobs run without containers, only if you allow "
            "unsandboxed jobs in Settings.",
            "Install Docker Engine (docs.docker.com/engine/install), then come back "
            "here.",
        )
    result = probe_docker(
        timeout_s=5.0, host_has_nvidia_gpu=False, gpu_probe_image=None, agent_label=""
    )
    if result.available:
        version = f" {result.version}" if result.version else ""
        return Check(
            "docker",
            title,
            CheckStatus.OK,
            f"Docker{version} works for MeshForge: jobs run in containers.",
        )
    reason = result.reason or "unknown"
    if reason == "docker_sdk_missing":
        return Check(
            "docker",
            title,
            CheckStatus.WARN,
            "This MeshForge installation has no Docker library.",
            "Reinstall the MeshForge agent package.",
        )
    if reason == "docker_permission_denied":
        listed = _in_docker_group_on_disk()
        if listed:
            return Check(
                "docker",
                title,
                CheckStatus.WARN,
                "MeshForge was given Docker access but has not restarted since.",
                "Restart MeshForge (button below).",
                RESTART_ACTION,
            )
        system = sys.platform == "linux" and install_mode() is InstallMode.SYSTEM
        return Check(
            "docker",
            title,
            CheckStatus.WARN,
            "Docker is installed, but MeshForge may not use it. Docker access is "
            "the same as full admin rights on this laptop, so it is off until you "
            "turn it on.",
            (
                "Right-click MeshForge in the app menu and choose 'Give Docker "
                f"access' (asks for your password), or run: {DOCKER_ACCESS_COMMAND}"
            )
            if system
            else "Add the user running the agent to the docker group, then restart "
            "the agent.",
            DOCKER_ACCESS_ACTION if system else None,
        )
    if reason.startswith("docker_seccomp"):
        return Check(
            "docker",
            title,
            CheckStatus.WARN,
            "Docker runs without its default security profile (seccomp), so "
            "MeshForge does not use it.",
            "Remove the seccomp=unconfined option from the Docker daemon settings "
            "and restart Docker.",
        )
    return Check(
        "docker",
        title,
        CheckStatus.WARN,
        "Docker is installed but not running.",
        "Start it: sudo systemctl start docker (and enable it to start at boot).",
    )


# ------------------------------------------------------------------- GPU, CUDA


def _nvidia_smi_header(timeout_s: float) -> tuple[str | None, str]:
    """``(output, failure)`` of a plain ``nvidia-smi`` run."""
    result = run_command("nvidia-smi", [], timeout_s=timeout_s)
    if result.ok:
        return result.stdout, ""
    return None, result.describe_failure()


def check_gpu() -> tuple[Check, Check]:
    from meshforge.agent.hardware.gpu import NVIDIA_SMI_FIELDS, parse_nvidia_smi_csv

    title = "NVIDIA GPU"
    cuda_title = "CUDA for jobs"
    no_cuda = Check(
        "cuda",
        cuda_title,
        CheckStatus.MISSING,
        "GPU jobs need an NVIDIA GPU with its driver.",
    )
    if which("nvidia-smi") is None:
        if NVIDIA_PROC.exists():
            return (
                Check(
                    "gpu",
                    title,
                    CheckStatus.WARN,
                    "An NVIDIA driver is loaded, but its nvidia-smi tool is missing.",
                    "Install the driver's utilities package (Ubuntu: "
                    "nvidia-utils-<version>), then restart MeshForge.",
                ),
                no_cuda,
            )
        return (
            Check(
                "gpu",
                title,
                CheckStatus.MISSING,
                "No NVIDIA GPU driver found. This laptop shares its CPU only.",
                "If it has an NVIDIA GPU, install the NVIDIA driver (Ubuntu: Software "
                "& Updates > Additional Drivers), then restart MeshForge.",
            ),
            no_cuda,
        )
    query = run_command(
        "nvidia-smi",
        [f"--query-gpu={','.join(NVIDIA_SMI_FIELDS)}", "--format=csv,noheader,nounits"],
        timeout_s=5.0,
    )
    if not query.ok:
        return (
            Check(
                "gpu",
                title,
                CheckStatus.WARN,
                "The NVIDIA driver does not answer MeshForge: "
                f"{query.describe_failure()[:160]}",
                "Restart the laptop; if it stays, reinstall the NVIDIA driver.",
            ),
            no_cuda,
        )
    try:
        gpus = parse_nvidia_smi_csv(query.stdout, NVIDIA_SMI_FIELDS)
    except ValueError:
        gpus = []
    names = ", ".join(g.model for g in gpus if g.model) or "an NVIDIA GPU"
    gpu = Check("gpu", title, CheckStatus.OK, f"Found {names}.")
    devices = [NVIDIA_CTL, *sorted(Path("/dev").glob("nvidia[0-9]*"))]
    blocked = [d for d in devices if d.exists() and not os.access(d, os.R_OK | os.W_OK)]
    if blocked:
        return gpu, Check(
            "cuda",
            cuda_title,
            CheckStatus.WARN,
            f"MeshForge may not open {blocked[0]}, so GPU jobs would fail.",
            "sudo usermod -aG video,render meshforge && sudo systemctl restart "
            "meshforge-agent",
        )
    header, failure = _nvidia_smi_header(5.0)
    match = _CUDA_RE.search(header or "")
    if match is None:
        return gpu, Check(
            "cuda",
            cuda_title,
            CheckStatus.WARN,
            "The driver did not report a CUDA version"
            + (f" ({failure[:120]})" if failure else "")
            + ".",
            "Update the NVIDIA driver.",
        )
    return gpu, Check(
        "cuda",
        cuda_title,
        CheckStatus.OK,
        f"The driver offers CUDA {match.group(1)} to MeshForge's jobs.",
    )


# ------------------------------------------------------------------------ disk


def check_disk(settings: AgentSettings) -> Check:
    title = "Free disk space"
    where = settings.data_dir
    probe = where
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as exc:
        return Check(
            "disk",
            title,
            CheckStatus.WARN,
            f"Cannot read the free space of {where}: {exc.strerror or exc}.",
        )
    gib = free / _GIB
    if free < DISK_MISSING_BYTES:
        return Check(
            "disk",
            title,
            CheckStatus.MISSING,
            f"Only {gib:.1f} GiB free where MeshForge keeps job files - not enough "
            "for any job environment.",
            "Free some disk space.",
        )
    if free < DISK_WARN_BYTES:
        return Check(
            "disk",
            title,
            CheckStatus.WARN,
            f"{gib:.1f} GiB free; GPU job environments can need 5-10 GiB each.",
            "Free some disk space to run GPU jobs.",
        )
    return Check("disk", title, CheckStatus.OK, f"{gib:.0f} GiB free.")


# -------------------------------------------------------------------- firewall


def _ufw_enabled(conf: Path = UFW_CONF) -> bool | None:
    """``ENABLED=yes`` in ufw.conf; None when ufw is not installed."""
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() == "ENABLED":
            return value.strip().strip("\"'").lower() == "yes"
    return False


def check_firewall(settings: AgentSettings, *, ufw_conf: Path = UFW_CONF) -> Check:
    title = "Firewall"
    peer_on = settings.agent.peer.bind is not None
    if sys.platform != "linux":
        return Check(
            "firewall",
            title,
            CheckStatus.WARN,
            "Not checked on this system yet.",
        )
    enabled = _ufw_enabled(ufw_conf)
    if not enabled:
        return Check(
            "firewall",
            title,
            CheckStatus.OK,
            "ufw is off or not installed (other firewalls are not checked). "
            "MeshForge only needs outgoing connections"
            + (" and its peer port." if peer_on else "."),
        )
    if not peer_on:
        return Check(
            "firewall",
            title,
            CheckStatus.OK,
            "ufw is on; MeshForge only makes outgoing connections, which ufw allows.",
        )
    port = settings.agent.peer.port
    return Check(
        "firewall",
        title,
        CheckStatus.WARN,
        f"ufw is on and may block other laptops from fetching files from this one "
        f"(port {port}).",
        "Allow it from your home network only, e.g.: sudo ufw allow from "
        f"192.168.1.0/24 to any port {port} proto tcp (use your network's range)",
    )


# ------------------------------------------------------------------------ peer


def check_peer(settings: AgentSettings, endpoint: str | None) -> Check:
    title = "Peer file sharing"
    peer = settings.agent.peer
    if peer.bind is None:
        return Check(
            "peer",
            title,
            CheckStatus.OK,
            "Off: other laptops fetch job files from the controller, not from this "
            "one.",
        )
    if not endpoint:
        return Check(
            "peer",
            title,
            CheckStatus.WARN,
            "The peer file server is not running (the laptop has not joined, or "
            "MeshForge is stopped).",
        )
    host, _, port_text = endpoint.rpartition(":")
    host = host.strip("[]")
    try:
        with socket.create_connection((host, int(port_text)), timeout=2.0):
            pass
    except (OSError, ValueError) as exc:
        return Check(
            "peer",
            title,
            CheckStatus.WARN,
            f"The peer file server at {endpoint} does not answer: "
            f"{getattr(exc, 'strerror', None) or exc}.",
            "Check the agent's log, then restart MeshForge.",
            RESTART_ACTION,
        )
    return Check(
        "peer",
        title,
        CheckStatus.OK,
        f"Serving files to other laptops at {endpoint} (checked from this laptop).",
    )


# ------------------------------------------------------------------------- all


async def _bounded(
    fn: Callable[[], Check | tuple[Check, ...]], ids: tuple[str, ...], title: str
) -> list[Check]:
    try:
        result = await asyncio.wait_for(asyncio.to_thread(fn), CHECK_TIMEOUT_S)
    except TimeoutError:
        return [
            Check(i, title, CheckStatus.WARN, "The check did not finish in time.")
            for i in ids
        ]
    except Exception as exc:  # noqa: BLE001 - a check never breaks the page
        log.warning("agent.console.check_failed", check=ids[0], cause=repr(exc))
        return [
            Check(
                i, title, CheckStatus.WARN, f"The check failed: {type(exc).__name__}."
            )
            for i in ids
        ]
    return list(result) if isinstance(result, tuple) else [result]


async def run_checks(
    settings: AgentSettings, *, peer_endpoint: str | None
) -> list[Check]:
    """Every check, in a fixed order (module docstring)."""
    groups = await asyncio.gather(
        _bounded(lambda: check_docker(settings), ("docker",), "Docker"),
        _bounded(check_gpu, ("gpu", "cuda"), "NVIDIA GPU"),
        _bounded(lambda: check_disk(settings), ("disk",), "Free disk space"),
        _bounded(lambda: check_firewall(settings), ("firewall",), "Firewall"),
        _bounded(lambda: check_peer(settings, peer_endpoint), ("peer",), "Peer"),
    )
    return [check for group in groups for check in group]
