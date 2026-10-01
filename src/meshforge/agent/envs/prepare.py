"""``meshforge-agent prepare``: pre-pull the pinned base images (V0.5 D87, local part).

Lab laptops are prepared before a run - ideally on a good network - so the first
job does not spend minutes pulling ``cuda-pytorch`` (~4 GB compressed) over Wi-Fi.
For each selected base (``[agent.execution.images]``, every value pinned by digest
- the settings refuse an unpinned one):

* already local -> ``present`` (Docker found it *by digest*, so it is exactly the
  pinned content - the daemon verifies digests on pull);
* otherwise pulled by digest -> ``pulled`` with the time it took;
* a pull that fails or outlives ``timeout_s`` -> ``failed`` with the reason (exit 1).

Sizes are the local image sizes. Nothing else is built: env images are built on
demand by the first job that needs one (``compute node prepare``, wave 2, will add
controller-driven preparation). A node without Docker reports ``docker:
unavailable`` and pulls nothing (the venv runtime needs no image) - not a failure.
Also reports whether the pinned ``uv`` (venv runtime) runs here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from meshforge.agent.envs._tool import capture_tool, tool_env
from meshforge.agent.envs.builder import default_uv_argv
from meshforge.agent.envs.errors import EnvError

if TYPE_CHECKING:
    from docker import DockerClient

    from meshforge.agent.settings import AgentSettings

__all__ = ["BASES", "ImageReport", "PrepareReport", "prepare"]

BASES: Final = ("python-cpu", "cuda-pytorch")
_UV_TIMEOUT_S: Final = 15.0


@dataclass(slots=True)
class ImageReport:
    base: str
    image: str
    status: str
    """``present`` / ``pulled`` / ``failed``."""
    size_bytes: int | None = None
    seconds: float = 0.0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "base": self.base,
            "image": self.image,
            "status": self.status,
            "size_bytes": self.size_bytes,
            "seconds": round(self.seconds, 3),
            "reason": self.reason or None,
        }


@dataclass(slots=True)
class PrepareReport:
    docker: str
    """``available`` or ``unavailable: <reason>``."""
    uv: str
    """The ``uv --version`` text, or ``missing``/``failed``."""
    images: list[ImageReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(image.status != "failed" for image in self.images)

    def as_dict(self) -> dict[str, Any]:
        return {
            "docker": self.docker,
            "uv": self.uv,
            "images": [image.as_dict() for image in self.images],
            "ok": self.ok,
        }

    def lines(self) -> list[str]:
        out = [f"docker: {self.docker}", f"uv: {self.uv}"]
        for image in self.images:
            size = (
                f"{image.size_bytes / 1024**2:.0f} MiB"
                if image.size_bytes is not None
                else "size unknown"
            )
            text = f"{image.base}: {image.status}"
            if image.status == "failed":
                text += f" ({image.reason}) after {image.seconds:.1f} s"
            else:
                text += f", {size}"
                if image.status == "pulled":
                    text += f", pulled in {image.seconds:.1f} s"
            out.append(f"{text}  [{image.image}]")
        return out


def _images(settings: AgentSettings) -> Mapping[str, str]:
    images = settings.agent.execution.images
    return {"python-cpu": images.python_cpu, "cuda-pytorch": images.cuda_pytorch}


async def _uv_version(uv_argv: Sequence[str]) -> str:
    if not uv_argv:
        return "missing (install the agent extra)"
    try:
        code, out = await capture_tool(
            [*uv_argv, "--version"], env=tool_env(), timeout_s=_UV_TIMEOUT_S
        )
    except (EnvError, OSError):
        return "missing (cannot start the uv binary)"
    if code != 0:
        return f"failed (exit {code})"
    return out.decode("utf-8", "replace").strip()[:64]


def _size(client: DockerClient, image: str) -> int | None:
    try:
        found = client.images.get(image)
    except Exception:  # noqa: BLE001 - size is informational
        return None
    attrs = found.attrs if isinstance(found.attrs, dict) else {}
    size = attrs.get("Size")
    return int(size) if isinstance(size, int) else None


async def _one(
    client: DockerClient,
    base: str,
    image: str,
    timeout_s: float,
    clock: Callable[[], float],
) -> ImageReport:
    from meshforge.agent.execution.docker_runtime import _Pull, _pull_ref
    from meshforge.agent.execution.docker_support import docker_errors, docker_module

    docker = docker_module()
    try:
        await asyncio.to_thread(client.images.get, image)
    except docker_errors() as exc:
        if docker is None or not isinstance(exc, docker.errors.ImageNotFound):
            return ImageReport(base, image, "failed", reason=type(exc).__name__)
    else:
        size = await asyncio.to_thread(_size, client, image)
        return ImageReport(base, image, "present", size_bytes=size)
    started = clock()
    pull = _Pull(client, *_pull_ref(image))
    task = asyncio.ensure_future(asyncio.to_thread(pull.run))
    try:
        async with asyncio.timeout(timeout_s):
            error = await asyncio.shield(task)
    except TimeoutError:
        pull.stop()
        return ImageReport(
            base,
            image,
            "failed",
            seconds=clock() - started,
            reason=f"timed out after {timeout_s:g} s",
        )
    except asyncio.CancelledError:
        pull.stop()
        raise
    except docker_errors() as exc:
        error = type(exc).__name__
    elapsed = clock() - started
    if error is not None:
        return ImageReport(base, image, "failed", seconds=elapsed, reason=error)
    size = await asyncio.to_thread(_size, client, image)
    return ImageReport(base, image, "pulled", size_bytes=size, seconds=elapsed)


async def prepare(
    settings: AgentSettings,
    *,
    bases: Sequence[str] = BASES,
    timeout_s: float | None = None,
    client_factory: Callable[[float], DockerClient] | None = None,
    uv_argv: Sequence[str] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> PrepareReport:
    """Pre-pull ``bases`` (module docstring). Never raises for Docker problems."""
    from meshforge.agent.execution.docker_support import open_client

    images = _images(settings)
    unknown = [b for b in bases if b not in images]
    if unknown:
        raise ValueError(f"unknown base(s): {', '.join(unknown)}")
    cfg = settings.agent.execution
    limit = timeout_s if timeout_s is not None else cfg.env_timeout_s
    uv = await _uv_version(default_uv_argv() if uv_argv is None else uv_argv)
    try:
        client = await asyncio.to_thread(
            client_factory or open_client, cfg.docker_timeout_s
        )
    except Exception as exc:  # noqa: BLE001 - "no Docker" is a normal laptop
        return PrepareReport(docker=f"unavailable: {type(exc).__name__}", uv=uv)
    report = PrepareReport(docker="available", uv=uv)
    try:
        for base in dict.fromkeys(bases):
            report.images.append(await _one(client, base, images[base], limit, clock))
    finally:
        await asyncio.to_thread(client.close)
    return report
