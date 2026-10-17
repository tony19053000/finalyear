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
import math
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
    "rows_freivalds",
    "shape_guard",
    "synchronize",
    "worse",
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


# ------------------------------------------------------------- check helpers
#
# Every self-check follows the same three rules (tester r1/r2; the dataset's
# labels trust ``verified``):
#   1. shape/dtype guard first - a wrong-shaped output is ``ok=False``, never an
#      exception (:func:`shape_guard`);
#   2. a non-finite value anywhere fails (callers check isfinite, and every
#      worst-error reduction goes through :func:`worse`, which keeps NaN/inf -
#      Python's ``max(worst, nan)`` silently drops a NaN);
#   3. the whole output is verified, independently of the timed code path.


def worse(current: float, new: float) -> float:
    """The larger error, where NaN counts as infinitely bad."""
    if new != new or current != current:  # NaN
        return math.inf
    return max(current, new)


def shape_guard(out: Any, shape: tuple[int, ...], dtype: Any) -> Check | None:
    """A failed :class:`Check` when ``out`` is not an array of ``shape`` and
    ``dtype``; None when it is (works for numpy arrays and torch tensors)."""
    got_shape = getattr(out, "shape", None)
    got_dtype = getattr(out, "dtype", None)
    if got_shape is None or tuple(got_shape) != tuple(shape) or got_dtype != dtype:
        return Check(
            ok=False,
            checksum=digest(b"wrong-shape"),
            exact=False,
            detail=f"output shape {None if got_shape is None else tuple(got_shape)} "
            f"dtype {got_dtype}, expected {tuple(shape)} {dtype}",
        )
    return None


FREIVALDS_FACTOR: dict[str, float] = {"float16": 8.0, "float32": 4.0, "float64": 8.0}
"""Per-row tolerance of :func:`rows_freivalds` per dtype, in units of
``eps(dtype) * (|A| (|B| |x|))_i / sqrt(n)`` (tester r2 D1).

Measured on Laptop 1 (2026-10-17; numpy/OpenBLAS and torch-CPU, n = 1 .. 4096,
20 seeds x 3 vectors up to n = 1024, 3 seeds above): the worst honest product
used 1.43 units in float32, 3.44 in float64 (the float64 reference's own
rounding is the same size as the product's) and 0.51 in float16 (fp32
accumulation). The worst cases are small n: the ratio *falls* with n (rounding
grows like sqrt(n), the scale like n; float32 0.35 at n = 4096), so n = 12288
extrapolates below that. Factors 4 / 8 / 8 leave >= 2.3x headroom everywhere. At
n = 384 one float32 element off by 1e-2 measures 22 units at factor 1 (5.5x the
tolerance); the r1 bound (2 eps |A||B||x|, no sqrt(n)) was ~10x looser there."""

_ROWS = 1024
"""Rows converted to float64 at a time (bounded extra memory, also on a GPU)."""


def rows_freivalds(
    a: Any,
    b: Any,
    c: Any,
    vectors: Sequence[Any],
    eps: float,
    to64: Callable[[Any], Any],
    factor: float,
) -> float:
    """Worst row-wise Freivalds error of ``C = A B`` over ``vectors`` (float64),
    in units of the tolerance (``<= 1`` passes; NaN/inf -> ``inf``).

    For every row ``i`` and vector ``x``: ``|(C x)_i - (A (B x))_i|`` against
    ``factor * eps * (|A| (|B| |x|))_i / sqrt(n)`` (:data:`FREIVALDS_FACTOR`). ``to64`` turns a
    row slice into float64 (``astype`` for numpy, ``.double()`` for torch); rows
    are converted :data:`_ROWS` at a time, so a 12288 x 12288 matrix never needs
    a float64 copy. O(n^2) per vector.
    """
    n = a.shape[0]
    scale = factor * eps / math.sqrt(max(n, 1))
    worst = 0.0
    for x in vectors:
        x_abs = abs(x)
        parts_bx = []
        parts_babs = []
        for start in range(0, n, _ROWS):
            rows = to64(b[start : start + _ROWS])
            parts_bx.append(rows @ x)
            parts_babs.append(abs(rows) @ x_abs)
        bx = _concat(parts_bx)
        babs = _concat(parts_babs)
        for start in range(0, n, _ROWS):
            a_rows = to64(a[start : start + _ROWS])
            error = abs(to64(c[start : start + _ROWS]) @ x - a_rows @ bx)
            bound = abs(a_rows) @ babs * scale + 1e-300
            worst = worse(worst, float((error / bound).max()))
    return worst


def _concat(parts: list[Any]) -> Any:
    first = parts[0]
    if hasattr(first, "new_empty"):  # torch
        import torch

        return torch.cat(parts)
    import numpy

    return numpy.concatenate(parts)
