"""Docker on this host: daemon facts, images, sandboxed ``docker run`` and the
typed classification of everything that can go wrong.

Everything is the ``docker`` command line (no SDK), so the same code runs
against Docker Engine on Linux and Docker Desktop on Windows. Free text from
the daemon (``docker info``, error output) is passed through the privacy
sanitizer before it is stored.

Failure types (``failure_type`` in error records; each has a ``fix`` hint):

==============================  ==============================================
``docker_cli_missing``          no ``docker`` on PATH
``docker_daemon_unreachable``   CLI present, daemon not running / not reachable
``docker_permission_denied``    Linux: socket exists, user not in ``docker`` group
``no_nvidia_runtime``           ``--gpus`` refused: no NVIDIA container support
``nvidia_driver_error``         runtime present, driver/NVML failed inside
``no_gpu_on_host``              host has no ``nvidia-smi`` GPU: GPU cases n/a
``image_pull_failed``           registry unreachable / image unknown
``sandbox_flag_refused``        one of the sandbox flags is not accepted here
``container_failed``            the container ran but exited non-zero
``timeout``                     the command exceeded its time limit
``insufficient_disk``           not enough free disk for a large image pull
==============================  ==============================================
"""

from __future__ import annotations

import json
import shutil
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from spikes._harness.envelope import sanitize_text
from spikes._s6 import proc
from spikes._s6.proc import Outcome

type JSON = Any

IS_WINDOWS: Final = sys.platform == "win32"
SMALL_IMAGE: Final = "python:3.12-slim"
GPU_SMI_IMAGE: Final = "nvidia/cuda:12.6.3-base-ubuntu24.04"
GPU_TORCH_IMAGE: Final = "pytorch/pytorch:2.8.0-cuda12.6-cudnn9-runtime"
#: ADR-0004 sandbox shape: no network, CPU/RAM/pids limits, non-root, read-only
#: root with a writable tmpfs. Every flag is a finding if a host refuses it.
SANDBOX_FLAGS: Final[tuple[str, ...]] = (
    "--network",
    "none",
    "--cpus",
    "1",
    "--memory",
    "512m",
    "--pids-limit",
    "64",
    "--user",
    "65534:65534",
    "--read-only",
    "--tmpfs",
    "/tmp",  # noqa: S108 - a tmpfs mount point *inside* the container
)
_SANDBOX_FLAG_NAMES: Final = tuple(f for f in SANDBOX_FLAGS if f.startswith("--"))
CONTAINER_NAME_PREFIX: Final = "s6-"
#: ``_MAX_ERROR_CHARS`` of daemon output are kept in a failure message.
_MAX_ERROR_CHARS: Final = 500
INFO_TIMEOUT_S: Final = 30.0

FIX_DAEMON: Final = (
    "start Docker Desktop and wait for 'Engine running'"
    if IS_WINDOWS
    else "start the daemon: sudo systemctl start docker"
)
FIX_PERMISSION: Final = (
    "add your user to the docker group: sudo usermod -aG docker $USER, then log out/in"
)
FIX_NVIDIA_RUNTIME: Final = (
    "Docker Desktop: Settings -> General -> 'Use the WSL 2 based engine'; "
    "NVIDIA Windows driver >= 470 (no toolkit inside WSL needed); "
    "check 'nvidia-smi' works in PowerShell"
    if IS_WINDOWS
    else "install the NVIDIA Container Toolkit, then: sudo nvidia-ctk runtime "
    "configure --runtime=docker && sudo systemctl restart docker"
)
FIX_DRIVER: Final = (
    "the NVIDIA driver on the host is not usable from containers: run 'nvidia-smi' "
    "on the host; on Linux check Secure Boot / dkms, on Windows update the driver"
)
FIX_PULL: Final = "check the network / proxy and that the image tag exists; retry"
FIX_CLI: Final = (
    "install Docker Desktop (Windows) or Docker Engine (Linux) and open a new shell"
)
FIX_FLAG: Final = (
    "this Docker version/host does not accept the flag; record it as an ADR-0004 "
    "finding (the sandbox shape must be adapted for this host)"
)


class DockerError(Exception):
    """A typed Docker failure (``failure_type`` + ``fix``)."""

    def __init__(self, failure_type: str, message: str, *, fix: str = "") -> None:
        super().__init__(message)
        self.failure_type = failure_type
        self.fix = fix

    def to_json(self) -> dict[str, JSON]:
        return {
            "failure_type": self.failure_type,
            "message": str(self),
            "fix": self.fix,
        }


def find_docker() -> str | None:
    """Absolute path of the ``docker`` CLI, or ``None``."""
    return shutil.which("docker")


def _short(text: str) -> str:
    """Daemon output for a message: last non-empty lines, sanitised, capped."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    tail = " | ".join(lines[-3:])
    return sanitize_text(tail[-_MAX_ERROR_CHARS:])


# ------------------------------------------------------------ classification
def classify(outcome: Outcome, *, image: str = "") -> DockerError:
    """Turn a failed ``docker`` command into a typed :class:`DockerError`.

    Pure on its inputs (the real error strings are in the unit tests).
    """
    if outcome.error:
        return DockerError("docker_cli_missing", outcome.error, fix=FIX_CLI)
    if outcome.timed_out:
        return DockerError("timeout", outcome.describe(), fix="rerun with --timeout")
    text = f"{outcome.stderr}\n{outcome.stdout}"
    low = text.lower()
    detail = _short(text) or f"exit code {outcome.returncode}"
    if "permission denied" in low and "docker daemon socket" in low:
        return DockerError("docker_permission_denied", detail, fix=FIX_PERMISSION)
    if (
        "cannot connect to the docker daemon" in low
        or "error during connect" in low
        or "is the docker daemon running" in low
        or ("docker desktop" in low and "not running" in low)
        or "dockerdesktoplinuxengine" in low
    ):
        return DockerError("docker_daemon_unreachable", detail, fix=FIX_DAEMON)
    if "could not select device driver" in low or (
        "unknown or invalid runtime name" in low and "nvidia" in low
    ):
        return DockerError("no_nvidia_runtime", detail, fix=FIX_NVIDIA_RUNTIME)
    if (
        "nvidia-container-cli" in low
        or "nvml" in low
        or "nvidia-smi has failed" in low
        or "libnvidia" in low
    ):
        return DockerError("nvidia_driver_error", detail, fix=FIX_DRIVER)
    refused = next((flag for flag in _SANDBOX_FLAG_NAMES if flag in low), None)
    if refused and (
        "unknown flag" in low
        or "invalid argument" in low
        or "does not support" in low
        or "unsupported" in low
        or "not supported" in low
    ):
        return DockerError(
            "sandbox_flag_refused", f"{refused}: {detail}", fix=f"{FIX_FLAG}: {refused}"
        )
    if (
        "pull access denied" in low
        or "manifest unknown" in low
        or "not found: manifest" in low
        or "repository does not exist" in low
        or "no such host" in low
        or "tls handshake timeout" in low
        or "context deadline exceeded" in low
        or "unable to find image" in low
        or "failed to resolve reference" in low
        or "i/o timeout" in low
        or ("connection refused" in low and "registry" in low)
    ):
        return DockerError(
            "image_pull_failed", f"{image or 'image'}: {detail}", fix=FIX_PULL
        )
    return DockerError("container_failed", detail, fix="see the message")


# --------------------------------------------------------------------- facts
def parse_version(text: str) -> dict[str, JSON]:
    """``docker version --format '{{json .}}'`` -> client/server versions."""
    try:
        doc = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {"client_version": None, "server_version": None}
    if not isinstance(doc, dict):
        return {"client_version": None, "server_version": None}
    client = _mapping(doc.get("Client"))
    server = _mapping(doc.get("Server"))
    return {
        "client_version": _str_or_none(client.get("Version")),
        "server_version": _str_or_none(server.get("Version")),
        "client_os_arch": _os_arch(client),
        "server_os_arch": _os_arch(server),
    }


def _mapping(value: object) -> Mapping[str, JSON]:
    return value if isinstance(value, Mapping) else {}


def _os_arch(part: Mapping[str, JSON]) -> str | None:
    os_name, arch = part.get("Os"), part.get("Arch")
    if isinstance(os_name, str) and isinstance(arch, str):
        return f"{os_name}/{arch}"
    return None


def _str_or_none(value: object) -> str | None:
    return sanitize_text(value) if isinstance(value, str) and value else None


def _str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [sanitize_text(v) for v in value if isinstance(v, str)]


def parse_info(text: str) -> dict[str, JSON]:
    """``docker info --format '{{json .}}'`` -> the facts S6 records.

    Docker Desktop reports ``OperatingSystem: "Docker Desktop"`` and a
    ``KernelVersion`` ending in ``-microsoft-standard-WSL2``; its ``Runtimes``
    has **no** ``nvidia`` key even when ``--gpus`` works (GPU support lives in
    the Desktop VM), so ``nvidia_runtime`` is informational there and the
    ``docker-gpu-smi`` case decides. ``ServerErrors`` is what a reachable CLI
    reports when the daemon is down.
    """
    try:
        doc = json.loads(text or "{}")
    except json.JSONDecodeError:
        return {"parsed": False, "server_errors": ["docker info output is not JSON"]}
    if not isinstance(doc, dict):
        return {
            "parsed": False,
            "server_errors": ["docker info output is not an object"],
        }
    runtimes = doc.get("Runtimes")
    runtime_names = (
        sorted(str(k) for k in runtimes) if isinstance(runtimes, dict) else []
    )
    server_errors = _str_list(doc.get("ServerErrors"))
    kernel = _str_or_none(doc.get("KernelVersion"))
    operating_system = _str_or_none(doc.get("OperatingSystem"))
    return {
        "parsed": True,
        "server_version": _str_or_none(doc.get("ServerVersion")),
        "os_type": _str_or_none(doc.get("OSType")),
        "operating_system": operating_system,
        "kernel_version": kernel,
        "runtimes": runtime_names,
        "nvidia_runtime": "nvidia" in runtime_names,
        "default_runtime": _str_or_none(doc.get("DefaultRuntime")),
        "cgroup_version": _str_or_none(doc.get("CgroupVersion")),
        "cgroup_driver": _str_or_none(doc.get("CgroupDriver")),
        "security_options": _str_list(doc.get("SecurityOptions")),
        "driver": _str_or_none(doc.get("Driver")),
        "docker_root_dir": _str_or_none(doc.get("DockerRootDir")),
        "docker_desktop": bool(
            operating_system and "docker desktop" in operating_system.lower()
        ),
        "wsl2_backend": bool(kernel and "microsoft" in kernel.lower()),
        "ncpu": doc.get("NCPU") if isinstance(doc.get("NCPU"), int) else None,
        "mem_total_bytes": (
            doc.get("MemTotal") if isinstance(doc.get("MemTotal"), int) else None
        ),
        "server_errors": server_errors,
    }


def docker_facts(
    docker: str | None, *, timeout: float = INFO_TIMEOUT_S
) -> dict[str, JSON]:
    """Everything ``docker version`` / ``docker info`` say, plus ``ok`` and,
    when not ok, the typed ``failure_type`` / ``message`` / ``fix``."""
    if docker is None:
        err = DockerError("docker_cli_missing", "docker is not on PATH", fix=FIX_CLI)
        return {"ok": False, **err.to_json()}
    facts: dict[str, JSON] = {"ok": False}
    version = proc.run([docker, "version", "--format", "{{json .}}"], timeout=timeout)
    facts.update(parse_version(version.stdout))
    info = proc.run([docker, "info", "--format", "{{json .}}"], timeout=timeout)
    parsed = parse_info(info.stdout)
    facts.update(parsed)
    server_errors = list(parsed.get("server_errors") or [])
    if not info.ok or server_errors or not parsed.get("server_version"):
        if server_errors:
            text = "; ".join(server_errors)
            fake = Outcome(info.argv, 1, "", text, info.elapsed_s)
            err = classify(fake)
            if err.failure_type == "container_failed":
                err = DockerError(
                    "docker_daemon_unreachable", _short(text), fix=FIX_DAEMON
                )
        elif info.ok:
            err = DockerError(
                "docker_daemon_unreachable",
                "docker info reports no server",
                fix=FIX_DAEMON,
            )
        else:
            err = classify(info)
            if err.failure_type == "container_failed":
                err = DockerError("docker_daemon_unreachable", str(err), fix=FIX_DAEMON)
        facts.update(err.to_json())
        return facts
    facts["ok"] = True
    return facts


# -------------------------------------------------------------------- images
def image_present(docker: str, image: str, *, timeout: float = 60.0) -> bool:
    return proc.run([docker, "image", "inspect", image], timeout=timeout).ok


def image_facts(docker: str, image: str, *, timeout: float = 60.0) -> dict[str, JSON]:
    """``digest`` (first RepoDigest) and ``size_bytes`` of a local image."""
    out = proc.run(
        [
            docker,
            "image",
            "inspect",
            "--format",
            "{{index .RepoDigests 0}}|{{.Size}}",
            image,
        ],
        timeout=timeout,
    )
    if not out.ok:
        return {"image": image, "digest": None, "size_bytes": None}
    digest, _, size = out.stdout.strip().partition("|")
    try:
        size_bytes: int | None = int(size)
    except ValueError:
        size_bytes = None
    return {"image": image, "digest": digest or None, "size_bytes": size_bytes}


def pull_image(docker: str, image: str, *, timeout: float) -> Outcome:
    return proc.run([docker, "pull", "--quiet", image], timeout=timeout)


def remove_image(docker: str, image: str, *, timeout: float = 120.0) -> Outcome:
    return proc.run([docker, "rmi", image], timeout=timeout)


def compressed_size(docker: str, image: str, *, timeout: float = 60.0) -> int | None:
    """Sum of the layer sizes of the image manifest (compressed, on the wire).

    Needs the network; ``None`` when unknown (the pull then uses a default
    estimate for the disk check).
    """
    out = proc.run([docker, "manifest", "inspect", image], timeout=timeout)
    if not out.ok:
        return None
    return parse_manifest_size(out.stdout)


def parse_manifest_size(text: str) -> int | None:
    """Layer bytes of one manifest; for a manifest *list* the largest
    ``linux/amd64`` entry cannot be resolved without another call, so ``None``."""
    try:
        doc = json.loads(text or "{}")
    except json.JSONDecodeError:
        return None
    layers = doc.get("layers") if isinstance(doc, dict) else None
    if not isinstance(layers, list):
        return None
    total = 0
    for layer in layers:
        size = layer.get("size") if isinstance(layer, dict) else None
        if not isinstance(size, int):
            return None
        total += size
    return total


# ----------------------------------------------------------------------- run
@dataclass(slots=True)
class ContainerRun:
    """A finished ``docker run`` with its wall-clock time."""

    name: str
    outcome: Outcome
    elapsed_ms: float
    launch_wall_s: float = 0.0
    warnings: list[str] = field(default_factory=list)


def new_container_name() -> str:
    return CONTAINER_NAME_PREFIX + uuid.uuid4().hex[:12]


def force_remove(docker: str, name: str, *, timeout: float = 30.0) -> None:
    """Best effort ``docker rm -f``; never raises."""
    proc.run([docker, "rm", "-f", name], timeout=timeout)


def list_own_containers(docker: str, *, timeout: float = 30.0) -> list[str]:
    """Names of containers with the S6 prefix still known to the daemon."""
    out = proc.run(
        [
            docker,
            "ps",
            "-a",
            "--filter",
            f"name={CONTAINER_NAME_PREFIX}",
            "--format",
            "{{.Names}}",
        ],
        timeout=timeout,
    )
    if not out.ok:
        return []
    return [
        ln.strip()
        for ln in out.stdout.splitlines()
        if ln.strip().startswith(CONTAINER_NAME_PREFIX)
    ]


def run_container(
    docker: str,
    image: str,
    command: Sequence[str],
    *,
    timeout: float,
    gpus: bool = False,
    sandbox: bool = False,
    extra_args: Sequence[str] = (),
) -> ContainerRun:
    """``docker run --rm --name s6-<id> [flags] IMAGE CMD...`` timed as a whole.

    The container is force-removed in ``finally`` (timeout, Ctrl-C, or a
    ``--rm`` that did not happen), so nothing is left running.
    """
    name = new_container_name()
    argv = [docker, "run", "--rm", "--name", name]
    if gpus:
        argv += ["--gpus", "all"]
    if sandbox:
        argv += list(SANDBOX_FLAGS)
    argv += list(extra_args)
    argv += [image, *command]
    launch_wall = time.time()
    try:
        outcome = proc.run(argv, timeout=timeout)
    finally:
        # ``--rm`` removes it on a clean exit; after a kill it may still run.
        force_remove(docker, name)
    warnings = [
        sanitize_text(ln.strip())
        for ln in outcome.stderr.splitlines()
        if ln.strip().upper().startswith("WARNING")
    ]
    return ContainerRun(
        name=name,
        outcome=outcome,
        elapsed_ms=outcome.elapsed_s * 1000.0,
        launch_wall_s=launch_wall,
        warnings=warnings,
    )


def parse_smi_row(text: str) -> dict[str, JSON]:
    """``name, driver_version, memory.total`` CSV row -> fields."""
    line = next((ln for ln in text.splitlines() if ln.strip()), "")
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return {"gpu_name": None, "driver_version": None, "memory_total": None}
    return {
        "gpu_name": sanitize_text(parts[0]),
        "driver_version": parts[1],
        "memory_total": parts[2],
    }


#: The script run inside the PyTorch image: import, check CUDA, one matmul,
#: print one JSON line. ``ready_wall_s`` lets the host compute
#: ``start_to_ready_ms`` against its own launch time (same kernel clock on
#: Linux; the Docker Desktop VM clock is expected to track the host's).
TORCH_SCRIPT: Final = """
import json, sys, time
t0 = time.perf_counter()
import torch
t1 = time.perf_counter()
ok = bool(torch.cuda.is_available())
name = torch.cuda.get_device_name(0) if ok else None
if ok:
    a = torch.randn(1024, 1024, device="cuda")
    b = torch.randn(1024, 1024, device="cuda")
    c = a @ b
    torch.cuda.synchronize()
t2 = time.perf_counter()
print(json.dumps({
    "torch_version": torch.__version__,
    "torch_cuda": torch.version.cuda,
    "cuda_available": ok,
    "device_name": name,
    "import_s": t1 - t0,
    "first_cuda_op_s": t2 - t1,
    "ready_wall_s": time.time(),
}))
sys.exit(0 if ok else 3)
"""


def parse_torch_line(stdout: str) -> dict[str, JSON] | None:
    """The JSON line printed by :data:`TORCH_SCRIPT`, or ``None``."""
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(doc, dict):
            return doc
    return None
