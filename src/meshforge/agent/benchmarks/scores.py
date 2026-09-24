"""The score arithmetic of the hardware scores: FLOPs, bytes moved, rates, throttling.

Moved here from ``lab/spikes/_s7/scores.py`` (spike S7, roadmap 0.2.11) by G7
(brief §5g D33): this is now the single source of truth, and the lab module is a
thin re-export. :data:`SCORE_VERSION` below versions every formula and kernel.

Every score is a *derived* number with one documented formula, so two laptops
can be compared even when they ran different problem sizes:

====================  =========================================================
score                 formula
====================  =========================================================
matmul GFLOP/s        ``2 * N^3 * iters / elapsed_s / 1e9``   (N x N x N matmul)
matmul TFLOP/s        ``2 * N^3 * iters / elapsed_s / 1e12``
SAXPY MFLOP/s         ``2 * elements / elapsed_s / 1e6``      (``y = a*x + y``)
stream GB/s           ``streams * elements * itemsize / elapsed_s / 1e9``
====================  =========================================================

``GB/s`` is 10^9 bytes per second and ``GFLOP/s`` 10^9 FLOPs per second (STREAM
and vendor convention), never 2^30. ``streams`` is how many array elements the
operation reads plus writes: STREAM counts the write of a non-temporal store as
traffic, so ``copy`` moves 2 elements, ``triad`` 3 (see :data:`STREAM_STREAMS`).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

SCORE_VERSION: Final = 1
"""Version of the score formulas **and** the kernels that feed them (D35).

Part of every cached score's fingerprint, and stored in every ``ScoreSet``: bump it
whenever a formula in this module, a kernel in :mod:`.kernels`, the case plan in
:mod:`.plan` or a sizing rule in :mod:`.sizing` changes in a way that can move a
number. Every agent then re-scores once on its next start instead of mixing old and
new numbers in the planner's training data (ARCHITECTURE §7). History:

* ``1`` - G7 (0.3.4): the S7 kernels; CPU matmul fp32/fp64, STREAM triad, GPU
  matmul fp32/fp16 with TF32 off, GPU device copy.
"""

#: ``2 * N^3`` for an N x N by N x N matrix multiply (N^3 multiply-adds).
MATMUL_FORMULA: Final = "2 * N^3 * iters / elapsed_s"
SAXPY_FORMULA: Final = "2 * elements / elapsed_s"
STREAM_FORMULA: Final = "streams * elements * itemsize / elapsed_s"

#: STREAM kernels: name -> (array elements read+written per element, expression).
STREAM_STREAMS: Final[dict[str, tuple[int, str]]] = {
    "copy": (2, "c[i] = a[i]"),
    "scale": (2, "b[i] = q * c[i]"),
    "add": (3, "c[i] = a[i] + b[i]"),
    "triad": (3, "a[i] = b[i] + q * c[i]"),
}
#: A repetition that is this much slower than the first one suggests throttling.
THROTTLE_THRESHOLD: Final = 0.15
#: Repetitions shorter than this are dominated by timer and scheduler jitter, so
#: a slowdown between them is no evidence of thermal throttling (``inconclusive``).
MIN_JUDGE_S: Final = 0.05


def _positive(seconds: float) -> float:
    if not seconds > 0:
        raise ValueError(f"elapsed time must be > 0, got {seconds!r}")
    return seconds


def matmul_flops(n: int, iters: int = 1) -> float:
    """FLOPs of ``iters`` multiplications of two ``n x n`` matrices."""
    if n < 1 or iters < 1:
        raise ValueError("n and iters must be >= 1")
    return 2.0 * n**3 * iters


def matmul_gflops(n: int, elapsed_s: float, iters: int = 1) -> float:
    return matmul_flops(n, iters) / _positive(elapsed_s) / 1e9


def matmul_tflops(n: int, elapsed_s: float, iters: int = 1) -> float:
    return matmul_flops(n, iters) / _positive(elapsed_s) / 1e12


def saxpy_mflops(elements: int, elapsed_s: float, iters: int = 1) -> float:
    """``y = a*x + y`` is one multiply + one add per element."""
    if elements < 1 or iters < 1:
        raise ValueError("elements and iters must be >= 1")
    return 2.0 * elements * iters / _positive(elapsed_s) / 1e6


def stream_gb_s(op: str, elements: int, itemsize: int, elapsed_s: float) -> float:
    """GB/s of one STREAM kernel over arrays of ``elements`` items."""
    if op not in STREAM_STREAMS:
        raise ValueError(f"unknown stream op {op!r}; known: {sorted(STREAM_STREAMS)}")
    if elements < 1 or itemsize < 1:
        raise ValueError("elements and itemsize must be >= 1")
    streams = STREAM_STREAMS[op][0]
    return streams * elements * itemsize / _positive(elapsed_s) / 1e9


def bytes_gb_s(bytes_moved: float, elapsed_s: float) -> float:
    """GB/s for an explicit byte count (device copies: read + write)."""
    if bytes_moved <= 0:
        raise ValueError("bytes_moved must be > 0")
    return bytes_moved / _positive(elapsed_s) / 1e9


def slowdown(elapsed: Sequence[float]) -> float | None:
    """``last / first - 1`` over the measured repetitions; ``None`` for n < 2.

    Positive means the machine got slower during the run (thermal/power limit).
    """
    if len(elapsed) < 2:
        return None
    first = elapsed[0]
    if not first > 0:
        raise ValueError("first elapsed time must be > 0")
    return elapsed[-1] / first - 1.0


def throttling_suspected(
    elapsed: Sequence[float], threshold: float = THROTTLE_THRESHOLD
) -> bool:
    """True when the last repetition is more than ``threshold`` slower than the
    first one (the honesty flag of every S7 case)."""
    drift = slowdown(elapsed)
    return drift is not None and drift > threshold


def stability(
    elapsed: Sequence[float],
    threshold: float = THROTTLE_THRESHOLD,
    min_judge_s: float = MIN_JUDGE_S,
) -> dict[str, float | int | bool | None]:
    """Stability record stored next to every case.

    ``thermal_throttling_suspected`` is the honest flag of the run: the last
    repetition was more than ``threshold`` slower than the first one. It is only
    raised for repetitions long enough to be meaningful (``min_judge_s``);
    shorter ones (a ``--quick`` smoke run) are marked ``inconclusive``.
    """
    drift = slowdown(elapsed)
    inconclusive = not elapsed or elapsed[0] < min_judge_s or drift is None
    return {
        "n": len(elapsed),
        "first_s": elapsed[0] if elapsed else None,
        "last_s": elapsed[-1] if elapsed else None,
        "slowdown": drift,
        "threshold": threshold,
        "min_judge_s": min_judge_s,
        "inconclusive": inconclusive,
        "thermal_throttling_suspected": (
            not inconclusive and drift is not None and drift > threshold
        ),
    }
