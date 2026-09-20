"""Capability tags: what this node can *run* (roadmap 0.3.3).

``HardwareSummary.capabilities`` is the free-form list the planner filters on, so
every tag must mean one checked fact, not a guess:

======================== =========================================================
``v0.3``                 the Hello *shape* marker (G3); always first
``python3.12``           the interpreter the agent itself runs on
``docker``               a Docker daemon answered ``docker version`` in time
``cuda``                 hardware discovery found at least one NVIDIA device
``nvidia-container-toolkit`` ``nvidia-ctk`` / ``nvidia-container-runtime`` on PATH
``torch``                ``import torch`` would succeed (checked with ``find_spec``,
                         which does **not** import it - that costs seconds)
``torch-cuda``           a child process confirmed ``torch.cuda.is_available()``
======================== =========================================================

``torch-cuda`` is the only tag that can cost real time, so it is probed last, in a
child process (a broken CUDA install can abort the interpreter natively), with
whatever is left of the discovery budget. If it does not answer the tag is simply
absent and ``HardwareFacts`` records ``timeout`` - a missing tag costs this node a
GPU-backed placement, never its enrolment (D10).
"""

from __future__ import annotations

import platform
import sys
from typing import Final

from meshforge.agent.hardware.probe import (
    Budget,
    ProbeOutcome,
    ProbeStatus,
    module_installed,
    run_command,
    which,
)

__all__ = [
    "DOCKER_PROBE_CAP_S",
    "TORCH_PROBE_CAP_S",
    "docker_available",
    "probe_software",
    "python_tag",
    "torch_cuda_available",
]

DOCKER_PROBE_CAP_S: Final = 3.0
TORCH_PROBE_CAP_S: Final = 3.0
"""Cap for the ``torch.cuda`` child process; the budget may shorten it further."""

_TORCH_SCRIPT: Final = (
    "import torch,sys;sys.stdout.write('1' if torch.cuda.is_available() else '0')"
)
_TOOLKIT_EXECUTABLES: Final[tuple[str, ...]] = (
    "nvidia-ctk",
    "nvidia-container-runtime",
    "nvidia-container-toolkit",
)


def python_tag() -> str:
    """``python3.12`` - major.minor of the interpreter running the agent."""
    version = platform.python_version_tuple()
    return f"python{version[0]}.{version[1]}"


def docker_available(timeout_s: float) -> bool:
    """True when a Docker *daemon* answers (the client alone is not a capability)."""
    if which("docker") is None:
        return False
    result = run_command(
        "docker", ["version", "--format", "{{.Server.Version}}"], timeout_s=timeout_s
    )
    return result.ok and bool(result.stdout.strip())


def nvidia_container_toolkit_available() -> bool:
    """True when the container toolkit's CLI is on PATH (no subprocess needed)."""
    return any(which(name) is not None for name in _TOOLKIT_EXECUTABLES)


def torch_cuda_available(timeout_s: float) -> bool | None:
    """``torch.cuda.is_available()`` from a child process; None when it did not answer."""
    result = run_command(sys.executable, ["-c", _TORCH_SCRIPT], timeout_s=timeout_s)
    if not result.ok:
        return None
    answer = result.stdout.strip()[-1:]
    return answer == "1" if answer in {"0", "1"} else None


def probe_software(
    budget: Budget,
    *,
    has_gpu: bool,
    docker_cap_s: float = DOCKER_PROBE_CAP_S,
    torch_cap_s: float = TORCH_PROBE_CAP_S,
) -> ProbeOutcome[list[str]]:
    """The capability tags of this node. Never raises, never returns ``None``."""
    started = budget.now()
    tags = [python_tag()]
    degraded: list[str] = []
    try:
        if has_gpu:
            tags.append("cuda")
        if nvidia_container_toolkit_available():
            tags.append("nvidia-container-toolkit")
        if budget.expired:
            degraded.append("docker: discovery budget spent")
        elif docker_available(budget.slice_s(docker_cap_s)):
            tags.append("docker")
        if module_installed("torch"):
            tags.append("torch")
            if budget.expired:
                degraded.append("torch-cuda: discovery budget spent")
            else:
                cuda = torch_cuda_available(budget.slice_s(torch_cap_s))
                if cuda is None:
                    degraded.append("torch-cuda: torch did not answer in time")
                elif cuda:
                    tags.append("torch-cuda")
    except Exception as exc:  # noqa: BLE001 - one field, never the agent (D10)
        return ProbeOutcome(
            "software",
            ProbeStatus.ERROR,
            value=tags,
            reason=f"{type(exc).__name__}: {exc}"[:200],
            duration_s=budget.now() - started,
        )
    duration = budget.now() - started
    if degraded:
        return ProbeOutcome(
            "software",
            ProbeStatus.TIMEOUT,
            value=tags,
            reason="; ".join(degraded)[:200],
            duration_s=duration,
        )
    return ProbeOutcome("software", ProbeStatus.OK, value=tags, duration_s=duration)
