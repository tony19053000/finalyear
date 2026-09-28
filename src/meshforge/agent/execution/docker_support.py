"""Docker plumbing shared by the runtime and its probe: the SDK (optional import),
container labels, the start-up orphan sweep and the availability/GPU probe.

The ``docker`` SDK is in the ``agent`` extra, and a laptop without Docker is normal
(brief §1: tests *skip* there; the agent falls back to the subprocess runtime when
its owner allows it). Every blocking SDK call is made from a worker thread by the
callers (``asyncio.to_thread``); nothing here touches the event loop.

**Labels.** Every container the runtime creates carries ``meshforge.managed=1``,
``meshforge.agent=<agent label>`` (the node id), ``meshforge.attempt_id`` and
``meshforge.role`` (``setup`` / ``run`` / ``probe``). A container with our agent
label that exists when the agent starts belongs to a previous agent process that
died without cleaning up - :func:`sweep_orphans` removes it (brief D51). Another
agent on the same machine (other node id) is never touched.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
from typing import TYPE_CHECKING, Any, Final

from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.common.logging import get_logger

if TYPE_CHECKING:
    from docker import DockerClient

__all__ = [
    "DOCKER_RUNTIME_NAME",
    "LABEL_AGENT",
    "LABEL_ATTEMPT",
    "LABEL_MANAGED",
    "LABEL_ROLE",
    "docker_errors",
    "docker_module",
    "open_client",
    "probe_docker",
    "sweep_orphans",
]

log = get_logger(__name__)

DOCKER_RUNTIME_NAME: Final = "docker"
LABEL_MANAGED: Final = "meshforge.managed"
LABEL_AGENT: Final = "meshforge.agent"
LABEL_ATTEMPT: Final = "meshforge.attempt_id"
LABEL_ROLE: Final = "meshforge.role"

_NO_GPU_DRIVER: Final = ("could not select device driver", "unknown or invalid runtime")


def docker_module() -> Any | None:
    """The ``docker`` SDK module, or None on an install without the agent extra."""
    try:
        import docker
    except ImportError:
        return None
    return docker


def docker_errors() -> tuple[type[BaseException], ...]:
    """Exception types the SDK raises for daemon/API problems (for ``except``)."""
    docker = docker_module()
    errors: list[type[BaseException]] = [OSError]
    if docker is not None:
        errors.append(docker.errors.DockerException)
        with contextlib.suppress(ImportError):
            import requests

            errors.append(requests.exceptions.RequestException)
    return tuple(errors)


def open_client(timeout_s: float) -> DockerClient:
    """A client for the local daemon (``DOCKER_HOST`` etc. honoured). Blocking.

    Raises the SDK's ``DockerException`` when the daemon cannot be reached and
    ``RuntimeError`` when the SDK is not installed.
    """
    docker = docker_module()
    if docker is None:
        raise RuntimeError("docker_sdk_missing")
    timeout = max(1, round(timeout_s))
    _precheck_unix_socket(docker, timeout_s)
    newest = docker.constants.DEFAULT_DOCKER_API_VERSION
    # Negotiate the API version ourselves: the SDK's version="auto" leaks its
    # socket when the daemon does not answer (seen as a ResourceWarning).
    first: DockerClient = docker.from_env(timeout=timeout, version=newest)
    try:
        server = str(first.api.version(api_version=False)["ApiVersion"])
    except KeyError as exc:
        raise docker.errors.DockerException("no ApiVersion from the daemon") from exc
    finally:
        first.close()
    use = server if docker.utils.version_lt(server, newest) else newest
    client: DockerClient = docker.from_env(timeout=timeout, version=use)
    return client


def _precheck_unix_socket(docker: Any, timeout_s: float) -> None:
    """Connect to a unix-socket daemon once ourselves, closing the socket either way.

    The SDK's unix transport does not close its socket when ``connect`` fails, so
    every probe of a stopped daemon would leak a file descriptor.
    """
    base = docker.utils.parse_host(
        os.environ.get("DOCKER_HOST"), sys.platform == "win32"
    )
    if not isinstance(base, str) or not base.startswith("http+unix://"):
        return
    path = base[len("http+unix://") :]
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(max(0.1, timeout_s))
        try:
            sock.connect(path)
        except OSError as exc:
            raise docker.errors.DockerException(
                f"cannot connect to the Docker daemon: {exc.strerror or exc}"
            ) from exc


def _unreachable_reason(exc: BaseException) -> str:
    text = str(exc).lower()
    if "permission denied" in text:
        return "docker_permission_denied"
    return "docker_daemon_unreachable"


def probe_docker(
    *,
    timeout_s: float,
    host_has_nvidia_gpu: bool,
    gpu_probe_image: str | None,
    agent_label: str,
) -> RuntimeAvailability:
    """Is a Docker daemon usable here, and can its containers get an NVIDIA GPU?

    Blocking (call it from a thread). Never raises. The GPU answer is **measured**
    when possible: a throw-away container asks for every GPU through
    ``device_requests`` with the (already local) ``gpu_probe_image`` - that is the
    exact mechanism :class:`DockerRuntime` uses, so "yes" means it works. Without a
    local probe image the answer falls back to "an ``nvidia`` runtime is registered
    / this is Docker Desktop" and says so in ``gpu_reason``.
    """
    if docker_module() is None:
        return RuntimeAvailability(
            DOCKER_RUNTIME_NAME, False, reason="docker_sdk_missing", sandboxed=True
        )
    try:
        client = open_client(timeout_s)
    except Exception as exc:  # noqa: BLE001 - every way of failing is "unavailable"
        return RuntimeAvailability(
            DOCKER_RUNTIME_NAME, False, reason=_unreachable_reason(exc), sandboxed=True
        )
    try:
        try:
            client.ping()
            version = str(client.version().get("Version", ""))[:64]
            info = client.info()
        except Exception as exc:  # noqa: BLE001
            return RuntimeAvailability(
                DOCKER_RUNTIME_NAME,
                False,
                reason=_unreachable_reason(exc),
                sandboxed=True,
            )
        gpu, gpu_reason = _probe_gpu(
            client, info, host_has_nvidia_gpu, gpu_probe_image, agent_label
        )
        return RuntimeAvailability(
            DOCKER_RUNTIME_NAME,
            True,
            gpu_capable=gpu,
            gpu_reason=gpu_reason,
            version=version,
            sandboxed=True,
        )
    finally:
        with contextlib.suppress(Exception):
            client.close()


def _probe_gpu(
    client: DockerClient,
    info: dict[str, Any],
    host_has_nvidia_gpu: bool,
    image: str | None,
    agent_label: str,
) -> tuple[bool, str]:
    if not host_has_nvidia_gpu:
        return False, "no_nvidia_gpu"
    docker = docker_module()
    if image is not None and docker is not None:
        try:
            client.images.get(image)
        except Exception:  # noqa: BLE001 - not local: fall back to the heuristic
            image = None
    if image is None or docker is None:
        runtimes = info.get("Runtimes") or {}
        desktop = "docker desktop" in str(info.get("OperatingSystem", "")).lower()
        if "nvidia" in runtimes or desktop:
            return True, "assumed: nvidia runtime registered (not verified by a run)"
        return False, "no_nvidia_container_runtime"
    container = None
    try:
        container = client.containers.create(
            image,
            # a GPU device node must really be there (Linux: /dev/nvidia*, Docker
            # Desktop / WSL2: /dev/dxg), not merely a container that started
            command=["sh", "-c", "test -e /dev/nvidiactl || test -e /dev/dxg"],
            labels={
                LABEL_MANAGED: "1",
                LABEL_AGENT: agent_label,
                LABEL_ROLE: "probe",
            },
            network_mode="none",
            user="65534:65534",
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            device_requests=[
                docker.types.DeviceRequest(count=-1, capabilities=[["gpu"]])
            ],
        )
        container.start()
        result = container.wait(timeout=30)
    except Exception as exc:  # noqa: BLE001
        text = str(exc).lower()
        if any(marker in text for marker in _NO_GPU_DRIVER):
            return False, "no_nvidia_container_runtime"
        return False, f"gpu_probe_failed: {type(exc).__name__}"
    else:
        if int(result.get("StatusCode", 1)) != 0:
            return False, "gpu_probe_no_device_node"
        return True, "verified: a container received the GPUs"
    finally:
        if container is not None:
            with contextlib.suppress(Exception):
                container.remove(force=True)


def sweep_orphans(client: DockerClient, agent_label: str) -> int:
    """Remove every container labelled for this agent (left by a dead agent).

    Blocking; call it once at agent start, before any attempt runs. Returns how
    many were removed; a container that cannot be removed is logged, not raised.
    """
    removed = 0
    for container in client.containers.list(
        all=True,
        filters={"label": [f"{LABEL_AGENT}={agent_label}", f"{LABEL_MANAGED}=1"]},
    ):
        labels = container.labels or {}
        try:
            container.remove(force=True)
            removed += 1
            log.warning(
                "agent.execution.orphan_removed",
                attempt_id=labels.get(LABEL_ATTEMPT),
                role=labels.get(LABEL_ROLE),
                container=container.short_id,
            )
        except Exception as exc:  # noqa: BLE001 - one stuck container must not stop the sweep
            log.warning(
                "agent.execution.orphan_remove_failed",
                attempt_id=labels.get(LABEL_ATTEMPT),
                container=container.short_id,
                cause=type(exc).__name__,
            )
    return removed
