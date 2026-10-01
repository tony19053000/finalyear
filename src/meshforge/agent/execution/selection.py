"""Which runtimes this node offers, the capability tags that say so, and which runtime
runs a given job (brief D45/D51, ADR-0014).

The tags are **computed here but not sent yet**: G4 adds them to ``Hello``'s
``HardwareSummary.capabilities``. The rules:

* ``[agent.execution] enabled = false`` -> no tag at all (the owner has not opted in;
  the controller never dispatches to a node without ``exec``).
* ``exec-docker`` when the Docker probe found a usable daemon.
* ``exec-subprocess`` only with ``allow_unsandboxed = true`` (the second opt-in):
  otherwise the unsandboxed runtime is neither advertised nor chosen.
* ``exec-venv`` (V0.5 D86) likewise only with ``allow_unsandboxed = true``: the venv
  runtime is unsandboxed too (it differs from ``subprocess`` in *which* Python and
  packages a job sees, not in isolation).
* ``exec`` when at least one runtime is offered.
* ``exec-gpu`` when an offered runtime can give a job an NVIDIA GPU: Docker whose
  probe verified (or, without a local probe image, assumed) GPU pass-through, or the
  subprocess runtime on a host with an NVIDIA GPU.

The constants are the wire spelling and live in :mod:`meshforge.common.capabilities`
(D60), which the controller's placement check reads too; they are re-exported here.
``exec-venv`` is defined here: the controller does not act on runtime-specific tags
(placement reads ``exec`` / ``exec-gpu`` only), so it has no shared constant yet.

**Preference order** (:meth:`ExecutionOffer.offered`, :func:`choose_runtime`):
**docker > venv > subprocess** - the sandbox first; then the venv runtime (the
job's own interpreter version and packages only, cached by env hash); the plain
subprocess runtime (the agent's interpreter) last.

**Bases (V0.5).** The venv runtime provides only the ``python-cpu`` base: a clean
interpreter plus the job's requirements, exactly like the ``python-cpu`` image.
A ``cuda-pytorch`` job is promised PyTorch by its base, which a fresh venv cannot
give (installing torch per env would mean a multi-GB download and a CUDA build
choice the agent cannot make), so it never goes to the venv runtime: Docker runs
it in the ``cuda-pytorch`` image, the subprocess runtime on the agent's own
interpreter (``uv sync --extra gpu``), as in V0.4.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Final

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
    "EXEC_VENV_CAPABILITY",
    "ExecutionOffer",
    "GpuNeed",
    "choose_runtime",
    "execution_offer",
    "runtime_fits_base",
]


VENV_BASES: Final = frozenset({"python-cpu"})
"""Manifest ``env.base`` values the venv runtime can provide (module docstring)."""

EXEC_VENV_CAPABILITY: Final = "exec-venv"
"""The venv runtime (V0.5 D86), only with ``allow_unsandboxed``."""


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
    venv: RuntimeAvailability | None = None
    """Same rule as ``subprocess`` (V0.5)."""

    def offered(self) -> tuple[RuntimeAvailability, ...]:
        """Usable, offered runtimes in preference order: docker > venv > subprocess."""
        if not self.enabled:
            return ()
        found = [
            r
            for r in (self.docker, self.venv, self.subprocess)
            if r is not None and r.available
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
            if runtime is self.docker:
                tags.add(EXEC_DOCKER_CAPABILITY)
            elif runtime is self.venv:
                tags.add(EXEC_VENV_CAPABILITY)
            else:
                tags.add(EXEC_SUBPROCESS_CAPABILITY)
            if runtime.gpu_capable:
                tags.add(EXEC_GPU_CAPABILITY)
        return tuple(sorted(tags))


def execution_offer(
    *,
    enabled: bool,
    allow_unsandboxed: bool,
    docker: RuntimeAvailability | None,
    subprocess: RuntimeAvailability | None,
    venv: RuntimeAvailability | None = None,
) -> ExecutionOffer:
    """Combine the settings and the probes into what the node offers."""
    return ExecutionOffer(
        enabled=enabled,
        docker=docker,
        subprocess=subprocess if allow_unsandboxed else None,
        venv=venv if allow_unsandboxed else None,
    )


def runtime_fits_base(runtime: RuntimeAvailability, base: str) -> bool:
    """Can ``runtime`` provide the manifest base (module docstring)?"""
    return runtime.name != "venv" or base in VENV_BASES


def choose_runtime(
    offer: ExecutionOffer, gpu: GpuNeed, base: str = "python-cpu"
) -> RuntimeAvailability | None:
    """The runtime to run a job with, or None when nothing offered fits.

    docker > venv > subprocess (:meth:`ExecutionOffer.offered`). ``required`` needs a
    GPU-capable runtime; ``preferred`` takes a GPU-capable one if any, else any;
    ``none`` takes the first offered.
    """
    offered = tuple(r for r in offer.offered() if runtime_fits_base(r, base))
    if gpu is GpuNeed.NONE:
        return offered[0] if offered else None
    with_gpu = [r for r in offered if r.gpu_capable]
    if with_gpu:
        return with_gpu[0]
    if gpu is GpuNeed.PREFERRED and offered:
        return offered[0]
    return None
