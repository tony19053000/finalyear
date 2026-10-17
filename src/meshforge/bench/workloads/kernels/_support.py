"""Job-side helpers shared by every benchmark kernel (V0.9 G2, D214).

This package (``kernels/``) is **copied into every benchmark job folder** as
``bench_kernels/`` (:mod:`meshforge.bench.workloads.materialise`), where it runs
inside the job's own environment: a clean ``python-cpu`` interpreter plus
``numpy``, or the ``cuda-pytorch`` base. MeshForge, pydantic and structlog are
**not** installed there, so the rules for every module in this package are:

* standard library only at import time; ``numpy`` / ``torch`` are imported inside
  the functions that need them (through :func:`require_numpy` /
  :func:`require_torch`, which turn a missing library into :class:`Unavailable`);
* relative imports only (``from ._support import ...``) - the package is
  ``meshforge.bench.workloads.kernels`` in the repository and ``bench_kernels``
  in a job folder;
* no file, network or environment access: a kernel builds its synthetic data
  from ``(params, seed)`` and returns values; :mod:`.runner` does the I/O.

A kernel is four small functions (:class:`Kernel`): ``prepare`` builds the data
(not timed), ``warmup`` runs a small slice once (not timed: first-touch page
faults, BLAS thread start, CUDA context and cuDNN autotune), ``compute`` is the
measured work, ``check`` verifies the output against an independent property
(Freivalds for matmul, an analytic integral, decompression, ...) and returns a
checksum.
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import ModuleType
from typing import Any

__all__ = [
    "Check",
    "Kernel",
    "KernelParamsError",
    "Unavailable",
    "canonical_json",
    "choice_param",
    "digest",
    "floats_digest",
    "int_param",
    "require_numpy",
    "require_torch",
    "synchronize",
]


class Unavailable(Exception):  # noqa: N818 - the brief's name for the typed outcome
    """This kernel cannot run on this host (no CUDA device, no torch, no numpy).

    A typed, expected outcome - never a crash: the job writes
    ``status: "unavailable"`` with :attr:`reason` and exits
    :data:`~.runner.EXIT_UNAVAILABLE`.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class KernelParamsError(ValueError):
    """A parameter is missing or has the wrong type (the job folder was edited)."""


@dataclass(frozen=True)
class Check:
    """What :attr:`Kernel.check` found.

    ``checksum`` is ``sha256:<hex>`` over the output (or a reduction of it).
    ``exact`` says whether the checksum is expected to be **bit-identical on every
    host** for the same ``(params, seed)``: true for integer and byte outputs
    (sort, sieve, zlib, sha256, counts), false for floating-point outputs, whose
    last bits depend on the BLAS / CPU / GPU (a float checksum is only expected to
    repeat on the same node and library build).
    """

    ok: bool
    checksum: str
    exact: bool
    detail: str = ""


@dataclass(frozen=True)
class Kernel:
    """One workload family's job-side code (see the module docstring)."""

    prepare: Callable[[Mapping[str, Any], int, str], Any]
    compute: Callable[[Any], Any]
    check: Callable[[Any, Any], Check]
    warmup: Callable[[Any], None] | None = None


def canonical_json(value: Any) -> str:
    """Sorted keys, no whitespace, no NaN: the one serialisation of params."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(*parts: bytes) -> str:
    """``sha256:<hex>`` of the parts, each prefixed with its length (no ambiguity
    between ``(b"ab", b"c")`` and ``(b"a", b"bc")``)."""
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(struct.pack("<Q", len(part)))
        hasher.update(part)
    return "sha256:" + hasher.hexdigest()


def floats_digest(values: Sequence[float]) -> str:
    """Checksum of a short sequence of floats (little-endian float64)."""
    return digest(struct.pack(f"<{len(values)}d", *values))


def int_param(params: Mapping[str, Any], name: str) -> int:
    value = params.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise KernelParamsError(f"{name}: expected an integer, got {value!r}")
    return value


def choice_param(params: Mapping[str, Any], name: str, choices: Sequence[str]) -> str:
    value = params.get(name)
    if not isinstance(value, str) or value not in choices:
        raise KernelParamsError(
            f"{name}: expected one of {list(choices)}, got {value!r}"
        )
    return value


def require_numpy() -> ModuleType:
    """``numpy``, or :class:`Unavailable` when the environment has none."""
    try:
        import numpy
    except ImportError:
        raise Unavailable("numpy is not installed in this job environment") from None
    return numpy


def require_torch(device: str) -> ModuleType:
    """``torch``, checked to reach ``device`` (``"cpu"`` or ``"cuda"``).

    ``"cuda"`` without a usable CUDA device is :class:`Unavailable` (a GPU family
    on a CPU-only node), never a crash.
    """
    try:
        import torch
    except ImportError:
        raise Unavailable("torch is not installed in this job environment") from None
    if device == "cuda":
        if not torch.cuda.is_available():
            raise Unavailable("no CUDA device is available on this node")
    elif device != "cpu":
        raise KernelParamsError(f"device: expected 'cpu' or 'cuda', got {device!r}")
    return torch


def synchronize(torch: ModuleType, device: str) -> None:
    """Wait for queued GPU work, so a timer around it measures the work itself."""
    if device == "cuda":
        torch.cuda.synchronize()
