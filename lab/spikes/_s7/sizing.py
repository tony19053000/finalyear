"""Problem-size selection: big enough to measure, small enough to always fit.

Sizes are *derived on the machine*, not hard-coded, because the four laptops
have different cache sizes, RAM and VRAM. What is fixed is the *rule*, so the
derived score stays comparable:

* **CPU matmul** - ``N`` is calibrated up from a small size until one
  multiplication takes at least ``target_s`` (so the timer and turbo noise are
  irrelevant), capped by :func:`max_matmul_n` from free RAM.
* **Memory bandwidth** - buffers are several times the last-level cache (so the
  data cannot live in cache) and never more than a quarter of the available RAM.
* **GPU matmul** - the three matrices must fit in a fraction (default half) of
  the *free* VRAM, so a desktop session using the same GPU cannot make the spike
  fail with out-of-memory.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

#: Buffers for the memory-bandwidth test: this many times the last-level cache.
CACHE_MULTIPLE: Final = 8
#: Used when no cache size can be read (docs/LAB.md: laptops have 12-24 MiB L3).
DEFAULT_MEMORY_BYTES: Final = 256 * 2**20
QUICK_MEMORY_BYTES: Final = 32 * 2**20
MIN_MEMORY_BYTES: Final = 8 * 2**20
#: Never take more than this share of the memory that is actually available.
RAM_FRACTION: Final = 0.25
#: Share of *free* VRAM the three GPU matrices may use.
VRAM_FRACTION: Final = 0.5
#: GPU matmul sizes are multiples of this (tensor cores like 128/256 alignment).
GPU_N_MULTIPLE: Final = 256
MIN_GPU_N: Final = 256
MAX_GPU_N: Final = 16384
MIN_CPU_N: Final = 64
MAX_CPU_N: Final = 8192


@dataclass(frozen=True, slots=True)
class Profile:
    """Everything the run size depends on. ``quick`` profiles are smoke runs."""

    quick: bool
    reps: int
    warmup: int
    #: Calibration target for one CPU repetition (seconds).
    cpu_target_s: float
    #: Calibration target for one GPU repetition (seconds).
    gpu_target_s: float
    #: Host memory-bandwidth buffer size (bytes, before the RAM cap).
    memory_bytes: int
    #: Per-operation timeouts of a benchmark child (seconds).
    setup_timeout_s: float
    rep_timeout_s: float
    #: Default whole-run budget (seconds).
    budget_s: float

    @property
    def name(self) -> str:
        return "quick" if self.quick else "full"


FULL: Final = Profile(
    quick=False,
    reps=5,
    warmup=2,
    cpu_target_s=0.30,
    gpu_target_s=0.20,
    memory_bytes=DEFAULT_MEMORY_BYTES,
    setup_timeout_s=300.0,
    rep_timeout_s=120.0,
    budget_s=1800.0,
)
QUICK: Final = Profile(
    quick=True,
    reps=5,  # never below MIN_VALID_REPS: a quick run is still a real n
    warmup=1,
    cpu_target_s=0.02,
    gpu_target_s=0.02,
    memory_bytes=QUICK_MEMORY_BYTES,
    setup_timeout_s=180.0,
    rep_timeout_s=60.0,
    budget_s=600.0,
)


def profile(*, quick: bool) -> Profile:
    return QUICK if quick else FULL


def round_down_to(value: int, multiple: int) -> int:
    if multiple < 1:
        raise ValueError("multiple must be >= 1")
    return (value // multiple) * multiple


def gpu_matmul_n(
    free_bytes: int,
    itemsize: int,
    *,
    fraction: float = VRAM_FRACTION,
    multiple: int = GPU_N_MULTIPLE,
    min_n: int = MIN_GPU_N,
    max_n: int = MAX_GPU_N,
) -> int:
    """Largest ``N`` whose three ``N x N`` matrices fit in ``fraction`` of free VRAM.

    ``3 * N^2 * itemsize <= fraction * free_bytes``. Returns a multiple of
    ``multiple`` clamped to ``[min_n, max_n]``; ``0`` when not even ``min_n``
    fits (the caller then records an explicit "not enough free VRAM" error).
    """
    if itemsize < 1:
        raise ValueError("itemsize must be >= 1")
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be in (0, 1]")
    if free_bytes <= 0:
        return 0
    budget = free_bytes * fraction
    n = math.isqrt(int(budget // (3 * itemsize)))
    n = min(round_down_to(n, multiple), round_down_to(max_n, multiple))
    return n if n >= min_n else 0


def max_matmul_n(
    available_bytes: int,
    itemsize: int,
    *,
    fraction: float = RAM_FRACTION,
    max_n: int = MAX_CPU_N,
) -> int:
    """Largest CPU matmul ``N`` whose three matrices fit in ``fraction`` of RAM."""
    if itemsize < 1:
        raise ValueError("itemsize must be >= 1")
    if available_bytes <= 0:
        return MIN_CPU_N
    n = math.isqrt(int(available_bytes * fraction // (3 * itemsize)))
    return max(MIN_CPU_N, min(n, max_n))


def memory_buffer_bytes(
    *,
    cache_bytes: int | None,
    available_bytes: int | None,
    profile_bytes: int = DEFAULT_MEMORY_BYTES,
    quick: bool = False,
) -> int:
    """Stream buffer size: ``max(profile, CACHE_MULTIPLE * L3)``, capped by RAM.

    ``quick`` keeps the profile size (a smoke run must stay small even on a
    machine with a huge cache).
    """
    wanted = profile_bytes
    if not quick and cache_bytes and cache_bytes > 0:
        wanted = max(wanted, CACHE_MULTIPLE * cache_bytes)
    if available_bytes and available_bytes > 0:
        wanted = min(wanted, int(available_bytes * RAM_FRACTION))
    return max(MIN_MEMORY_BYTES, wanted)


def calibrate(
    measure: Callable[[int], float],
    *,
    start: int,
    target_s: float,
    max_size: int,
    exponent: float = 3.0,
    growth: int = 2,
    max_steps: int = 24,
) -> tuple[int, list[dict[str, float | int]]]:
    """Grow the problem size until one run takes at least ``target_s``.

    ``measure(size) -> seconds`` is called with growing sizes until it reaches
    ``target_s`` or ``max_size``. ``exponent`` is how the work scales with the
    size (3 for an ``N x N`` matmul, 1 for a vector kernel), so the next size is
    ``size * (target / seconds) ** (1 / exponent)``, clamped to ``growth`` per
    step. Returns the chosen size and the trace (kept in the result JSON, so a
    suspicious size can be explained afterwards).
    """
    if start < 1 or max_size < 1:
        raise ValueError("start and max_size must be >= 1")
    if growth < 2:
        raise ValueError("growth must be >= 2")
    if exponent <= 0:
        raise ValueError("exponent must be > 0")
    if target_s <= 0:
        raise ValueError("target_s must be > 0")
    size = min(start, max_size)
    trace: list[dict[str, float | int]] = []
    for _ in range(max_steps):
        seconds = measure(size)
        trace.append({"size": size, "elapsed_s": seconds})
        if seconds >= target_s or size >= max_size:
            return size, trace
        factor = growth
        if seconds > 0:
            wanted = math.ceil((target_s / seconds) ** (1.0 / exponent))
            factor = max(2, min(growth, wanted))
        size = min(size * factor, max_size)
    return size, trace
