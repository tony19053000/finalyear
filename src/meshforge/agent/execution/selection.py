"""Which runtimes this node offers, the capability tags that say so, and which runtime
runs a given job (brief D45/D51, ADR-0014).

The tags are **computed here but not sent yet**: G4 adds them to ``Hello``'s
``HardwareSummary.capabilities``. The rules:

* ``[agent.execution] enabled = false`` -> no tag at all (the owner has not opted in;
  the controller never dispatches to a node without ``exec``).
* ``exec-docker`` when the Docker probe found a usable daemon.
* ``exec-subprocess`` only with ``allow_unsandboxed = true`` (the second opt-in):
  otherwise the unsandboxed runtime is neither advertised nor chosen.
* ``exec`` when at least one runtime is offered.
* ``exec-gpu`` when an offered runtime can give a job an NVIDIA GPU: Docker whose
  probe verified (or, without a local probe image, assumed) GPU pass-through, or the
  subprocess runtime on a host with an NVIDIA GPU.

The constants are the wire spelling and live in :mod:`meshforge.common.capabilities`
(D60), which the controller's placement check reads too; they are re-exported here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from meshforge.agent.execution.runtime import RuntimeAvailability
from meshforge.common.capabilities import (
    EXEC_CAPABILITY,
    EXEC_DOCKER_CAPABILITY,
    EXEC_GPU_CAPABILITY,
    EXEC_SUBPROCESS_CAPABILITY,
)

__all__ = [
    "EXEC_CAPABILITY",
    "EXEC_DOCKER_CAPABILITY",
    "EXEC_GPU_CAPABILITY",
    "EXEC_SUBPROCESS_CAPABILITY",
    "ExecutionOffer",
    "GpuNeed",
    "choose_runtime",
    "execution_offer",
]


class GpuNeed(StrEnum):
    """Manifest ``resources.gpu`` (brief D46)."""

    REQUIRED = "required"
    PREFERRED = "preferred"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ExecutionOffer:
    """What this node offers for execution, after probing (one per agent start)."""

    enabled: bool
    docker: RuntimeAvailability | None
    subprocess: RuntimeAvailability | None
    """``None`` when not offered (``allow_unsandboxed`` off) - not merely unavailable."""

    def offered(self) -> tuple[RuntimeAvailability, ...]:
        """Usable, offered runtimes in preference order (sandboxed first)."""
        if not self.enabled:
            return ()
        found = [
            r for r in (self.docker, self.subprocess) if r is not None and r.available
        ]
        return tuple(found)

    @property
    def capabilities(self) -> tuple[str, ...]:
        """The ``exec*`` tags for ``HardwareSummary.capabilities`` (sorted)."""
        offered = self.offered()
        if not offered:
            return ()
        tags = {EXEC_CAPABILITY}
        for runtime in offered:
            tags.add(
                EXEC_DOCKER_CAPABILITY
                if runtime is self.docker
                else EXEC_SUBPROCESS_CAPABILITY
            )
            if runtime.gpu_capable:
                tags.add(EXEC_GPU_CAPABILITY)
        return tuple(sorted(tags))


def execution_offer(
    *,
    enabled: bool,
    allow_unsandboxed: bool,
    docker: RuntimeAvailability | None,
    subprocess: RuntimeAvailability | None,
) -> ExecutionOffer:
    """Combine the settings and the probes into what the node offers."""
    return ExecutionOffer(
        enabled=enabled,
        docker=docker,
        subprocess=subprocess if allow_unsandboxed else None,
    )


def choose_runtime(offer: ExecutionOffer, gpu: GpuNeed) -> RuntimeAvailability | None:
    """The runtime to run a job with, or None when nothing offered fits.

    Sandboxed first (Docker), then the subprocess runtime. ``required`` needs a
    GPU-capable runtime; ``preferred`` takes a GPU-capable one if any, else any;
    ``none`` takes the first offered.
    """
    offered = offer.offered()
    if gpu is GpuNeed.NONE:
        return offered[0] if offered else None
    with_gpu = [r for r in offered if r.gpu_capable]
    if with_gpu:
        return with_gpu[0]
    if gpu is GpuNeed.PREFERRED and offered:
        return offered[0]
    return None
