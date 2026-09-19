"""Unit tests for the S7 score arithmetic and the throttling flag (0.2.11).

The scores are what the planner ranks nodes with (ARCHITECTURE §7), so the
formulas are pinned here with hand-computed values.
"""

from __future__ import annotations

import pytest

from spikes._s7 import scores


def test_matmul_flops_is_two_n_cubed() -> None:
    assert scores.matmul_flops(2) == 16.0
    assert scores.matmul_flops(2, iters=3) == 48.0
    # 4096^3 * 2 = 137.44 GFLOP, in 1 s -> 137.44 GFLOP/s
    assert scores.matmul_gflops(4096, 1.0) == pytest.approx(137.438953472)
    assert scores.matmul_tflops(4096, 1.0) == pytest.approx(0.137438953472)
    assert scores.matmul_gflops(1024, 0.5, iters=4) == pytest.approx(
        2 * 1024**3 * 4 / 0.5 / 1e9
    )


@pytest.mark.parametrize(("n", "iters"), [(0, 1), (1, 0), (-4, 1)])
def test_matmul_rejects_impossible_sizes(n: int, iters: int) -> None:
    with pytest.raises(ValueError):
        scores.matmul_flops(n, iters)


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_rates_reject_non_positive_time(bad: float) -> None:
    with pytest.raises(ValueError, match="elapsed"):
        scores.matmul_gflops(64, bad)
    with pytest.raises(ValueError, match="elapsed"):
        scores.saxpy_mflops(10, bad)
    with pytest.raises(ValueError, match="elapsed"):
        scores.stream_gb_s("copy", 10, 8, bad)
    with pytest.raises(ValueError, match="elapsed"):
        scores.bytes_gb_s(10, bad)


def test_saxpy_is_two_flops_per_element() -> None:
    assert scores.saxpy_mflops(1_000_000, 1.0) == pytest.approx(2.0)
    assert scores.saxpy_mflops(1_000_000, 0.5, iters=2) == pytest.approx(8.0)


def test_stream_traffic_counts_reads_and_writes() -> None:
    # copy touches 2 arrays, triad 3 (STREAM convention).
    assert scores.stream_gb_s("copy", 1_000_000_000, 1, 1.0) == pytest.approx(2.0)
    assert scores.stream_gb_s("triad", 125_000_000, 8, 1.0) == pytest.approx(3.0)
    assert scores.STREAM_STREAMS["scale"][0] == 2
    assert set(scores.STREAM_STREAMS) == {"copy", "scale", "add", "triad"}


def test_stream_rejects_unknown_op_and_sizes() -> None:
    with pytest.raises(ValueError, match="unknown stream op"):
        scores.stream_gb_s("nope", 10, 8, 1.0)
    with pytest.raises(ValueError):
        scores.stream_gb_s("copy", 0, 8, 1.0)
    with pytest.raises(ValueError):
        scores.bytes_gb_s(0, 1.0)


def test_bytes_gb_s_uses_ten_to_the_nine() -> None:
    assert scores.bytes_gb_s(2e9, 2.0) == pytest.approx(1.0)


def test_slowdown_is_last_over_first() -> None:
    assert scores.slowdown([1.0, 1.0, 1.2]) == pytest.approx(0.2)
    assert scores.slowdown([1.0]) is None
    assert scores.slowdown([]) is None
    with pytest.raises(ValueError):
        scores.slowdown([0.0, 1.0])


def test_throttling_flag_threshold() -> None:
    assert scores.throttling_suspected([1.0, 1.0, 1.16])
    assert not scores.throttling_suspected([1.0, 1.0, 1.15])  # exactly at the limit
    assert not scores.throttling_suspected([1.0, 2.0, 0.9])  # ended as fast as it began
    assert not scores.throttling_suspected([1.0])


def test_stability_record_flags_a_throttled_run() -> None:
    record = scores.stability([0.5, 0.55, 0.7])
    assert record["n"] == 3
    assert record["first_s"] == 0.5
    assert record["last_s"] == 0.7
    assert record["slowdown"] == pytest.approx(0.4)
    assert record["inconclusive"] is False
    assert record["thermal_throttling_suspected"] is True


def test_stability_of_very_short_reps_is_inconclusive() -> None:
    """A 4 ms quick-mode repetition is timer jitter, not thermal evidence."""
    record = scores.stability([0.004, 0.004, 0.009])
    assert record["slowdown"] == pytest.approx(1.25)
    assert record["inconclusive"] is True
    assert record["thermal_throttling_suspected"] is False


def test_stability_of_no_samples() -> None:
    record = scores.stability([])
    assert record["n"] == 0
    assert record["first_s"] is None
    assert record["slowdown"] is None
    assert record["inconclusive"] is True
    assert record["thermal_throttling_suspected"] is False
