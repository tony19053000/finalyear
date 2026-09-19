"""Unit tests for S7 problem-size selection (0.2.11).

These decide how big the benchmarks run on each laptop; getting them wrong
means an out-of-memory crash on the 4 GiB GPU or a number measured entirely
inside the L3 cache.
"""

from __future__ import annotations

import math

import pytest

from spikes._harness import MIN_VALID_REPS
from spikes._s7 import sizing


def test_profiles_never_drop_below_the_lab_repetition_rule() -> None:
    assert sizing.FULL.reps >= MIN_VALID_REPS
    assert sizing.QUICK.reps >= MIN_VALID_REPS  # a quick run is still n >= 5
    assert sizing.QUICK.memory_bytes < sizing.FULL.memory_bytes
    assert sizing.QUICK.budget_s < sizing.FULL.budget_s
    assert sizing.profile(quick=True) is sizing.QUICK
    assert sizing.profile(quick=False) is sizing.FULL
    assert (sizing.QUICK.name, sizing.FULL.name) == ("quick", "full")


def test_gpu_size_fits_three_matrices_in_half_the_free_vram() -> None:
    free = 4 * 2**30  # a 4 GiB RTX 3050 with nothing else running
    n = sizing.gpu_matmul_n(free, 4)
    assert n % sizing.GPU_N_MULTIPLE == 0
    assert 3 * n**2 * 4 <= free * 0.5
    assert 3 * (n + sizing.GPU_N_MULTIPLE) ** 2 * 4 > free * 0.5  # and it is maximal
    # fp16 items are half the size, so N grows by sqrt(2) (rounded to 256).
    assert sizing.gpu_matmul_n(free, 2) > n


def test_gpu_size_is_zero_when_vram_is_too_small_or_unknown() -> None:
    assert sizing.gpu_matmul_n(1 * 2**20, 4) == 0
    assert sizing.gpu_matmul_n(0, 4) == 0
    assert sizing.gpu_matmul_n(-1, 4) == 0


def test_gpu_size_is_capped_and_validated() -> None:
    assert sizing.gpu_matmul_n(10**15, 4) == sizing.MAX_GPU_N
    with pytest.raises(ValueError):
        sizing.gpu_matmul_n(2**30, 0)
    with pytest.raises(ValueError):
        sizing.gpu_matmul_n(2**30, 4, fraction=0.0)
    with pytest.raises(ValueError):
        sizing.gpu_matmul_n(2**30, 4, fraction=1.5)


def test_cpu_matmul_cap_follows_available_ram() -> None:
    n = sizing.max_matmul_n(16 * 2**30, 8)
    assert 3 * n**2 * 8 <= 16 * 2**30 * sizing.RAM_FRACTION
    assert sizing.max_matmul_n(10**15, 8) == sizing.MAX_CPU_N
    assert sizing.max_matmul_n(0, 8) == sizing.MIN_CPU_N
    assert sizing.max_matmul_n(1024, 8) == sizing.MIN_CPU_N  # never below the floor


def test_memory_buffer_is_several_times_the_cache_but_fits_in_ram() -> None:
    l3 = 24 * 2**20  # Laptop 1 (i7-12650H): 8 x 24 MiB = 192 MiB < the 256 MiB floor
    size = sizing.memory_buffer_bytes(cache_bytes=l3, available_bytes=16 * 2**30)
    assert size == sizing.DEFAULT_MEMORY_BYTES
    assert size >= sizing.CACHE_MULTIPLE * l3 or size == sizing.DEFAULT_MEMORY_BYTES
    big = sizing.memory_buffer_bytes(cache_bytes=64 * 2**20, available_bytes=16 * 2**30)
    assert big == sizing.CACHE_MULTIPLE * 64 * 2**20  # a big cache wins


def test_memory_buffer_falls_back_and_respects_a_small_machine() -> None:
    assert (
        sizing.memory_buffer_bytes(cache_bytes=None, available_bytes=16 * 2**30)
        == sizing.DEFAULT_MEMORY_BYTES
    )
    # 512 MiB available -> a quarter of it, not 256 MiB.
    assert sizing.memory_buffer_bytes(
        cache_bytes=None, available_bytes=512 * 2**20
    ) == int(512 * 2**20 * sizing.RAM_FRACTION)
    # and never below the floor, whatever the machine says.
    assert (
        sizing.memory_buffer_bytes(cache_bytes=None, available_bytes=1)
        == sizing.MIN_MEMORY_BYTES
    )
    assert (
        sizing.memory_buffer_bytes(cache_bytes=None, available_bytes=None)
        == sizing.DEFAULT_MEMORY_BYTES
    )


def test_quick_mode_ignores_a_huge_cache() -> None:
    size = sizing.memory_buffer_bytes(
        cache_bytes=256 * 2**20,
        available_bytes=16 * 2**30,
        profile_bytes=sizing.QUICK_MEMORY_BYTES,
        quick=True,
    )
    assert size == sizing.QUICK_MEMORY_BYTES


def test_calibrate_grows_until_the_target_time() -> None:
    # A cubic cost model: an N x N matmul at 1 ns per N^3.
    def measure(n: int) -> float:
        return (n**3) * 1e-9

    size, trace = sizing.calibrate(
        measure, start=128, target_s=0.3, max_size=8192, exponent=3.0
    )
    assert measure(size) >= 0.3
    assert [step["size"] for step in trace] == sorted(step["size"] for step in trace)
    assert trace[-1]["size"] == size
    assert size < 8192  # it stopped as soon as the target was reached


def test_calibrate_stops_at_max_size_on_a_slow_machine() -> None:
    calls: list[int] = []

    def measure(n: int) -> float:
        calls.append(n)
        return 1e-9  # never reaches the target

    size, trace = sizing.calibrate(
        measure, start=64, target_s=1.0, max_size=256, exponent=3.0
    )
    assert size == 256
    assert calls[-1] == 256
    assert len(trace) <= 5


def test_calibrate_linear_kernel_uses_bigger_steps() -> None:
    def measure(n: int) -> float:
        return n * 1e-8

    size, _ = sizing.calibrate(
        measure, start=1000, target_s=0.1, max_size=10**9, exponent=1.0, growth=16
    )
    assert size * 1e-8 >= 0.1
    assert size <= 16 * 10**7  # it did not overshoot by orders of magnitude


def test_calibrate_handles_a_zero_measurement() -> None:
    seen: list[int] = []

    def measure(n: int) -> float:
        seen.append(n)
        return 0.0 if n < 1000 else 1.0

    size, _ = sizing.calibrate(
        measure, start=1, target_s=0.5, max_size=4096, exponent=1.0, growth=4
    )
    assert size >= 1000
    assert seen[0] == 1


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start": 0},
        {"max_size": 0},
        {"growth": 1},
        {"exponent": 0.0},
        {"target_s": 0.0},
    ],
)
def test_calibrate_validates_its_arguments(kwargs: dict[str, float | int]) -> None:
    args: dict[str, float | int] = {
        "start": 8,
        "target_s": 0.1,
        "max_size": 64,
        "growth": 2,
        "exponent": 3.0,
    }
    args.update(kwargs)
    with pytest.raises(ValueError):
        sizing.calibrate(lambda _n: 0.001, **args)  # type: ignore[arg-type]


def test_round_down_to() -> None:
    assert sizing.round_down_to(1000, 256) == 768
    assert sizing.round_down_to(256, 256) == 256
    assert sizing.round_down_to(5, 256) == 0
    with pytest.raises(ValueError):
        sizing.round_down_to(10, 0)


def test_gpu_size_matches_the_closed_form() -> None:
    free, itemsize = 3 * 2**30, 2
    expected = sizing.round_down_to(
        int(math.isqrt(int(free * 0.5 // (3 * itemsize)))), sizing.GPU_N_MULTIPLE
    )
    assert sizing.gpu_matmul_n(free, itemsize) == expected
