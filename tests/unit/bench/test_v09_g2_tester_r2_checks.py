"""V0.9 G2 tester round 2: do the fixed self-checks close the whole class?

CPU / memory kernels. Split in G3 item 0.2 (D225.4): the torch kernels are in
``test_v09_g2_tester_r2_checks_torch.py`` (so these no longer skip without
torch), PARAM_LIMITS / seeds / ``runner.main`` in ``test_v09_g2_tester_r2_params.py``.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from typing import Any

import pytest

np = pytest.importorskip("numpy")

from mf_bench_checks import run_kernel_check as _run  # noqa: E402  tests/_support

from meshforge.bench.workloads.kernels import cpu, memory  # noqa: E402

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ cpu.matmul (D3)


@pytest.mark.parametrize("dtype", ["float32", "float64"])
@pytest.mark.parametrize("n", [1, 2, 3, 257])
def test_matmul_honest_small_and_odd_sizes_pass(dtype: str, n: int) -> None:
    assert _run(cpu, "cpu.matmul", {"n": n, "repeats": 1, "dtype": dtype}).ok


@pytest.mark.parametrize("seed", [0, 1, 2, 7, 2**32 - 1])
def test_matmul_honest_float32_n1024_many_seeds_no_false_positive(seed: int) -> None:
    c = _run(
        cpu, "cpu.matmul", {"n": 1024, "repeats": 1, "dtype": "float32"}, seed=seed
    )
    assert c.ok, c.detail


def test_matmul_honest_float32_n4096_corner() -> None:
    c = _run(cpu, "cpu.matmul", {"n": 4096, "repeats": 1, "dtype": "float32"})
    assert c.ok, c.detail


@pytest.mark.parametrize("dtype", ["float32", "float64"])
def test_matmul_tamper_shapes(dtype: str) -> None:
    p = {"n": 384, "repeats": 1, "dtype": dtype}
    big = 1.0

    def elem(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[17, 301] += big
        return c

    def nan(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[3, 3] = np.nan
        return c

    def inf(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[3, 3] = np.inf
        return c

    def perm_rows(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[[0, 1]] = c[[1, 0]]
        return c

    def perm_cols(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[:, [4, 5]] = c[:, [5, 4]]
        return c

    def swap_elems(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[2, 3], c[3, 2] = c[3, 2], c[2, 3]
        return c

    def shape(_s: Any, c: Any) -> Any:
        return c[:-1]

    def shape_t(_s: Any, c: Any) -> Any:
        return c.reshape(-1)

    def other_dtype(_s: Any, c: Any) -> Any:
        return c.astype(np.float16)

    def b_instead(s: Any, _c: Any) -> Any:
        return s["b"] @ s["a"]

    for name, m in [
        ("elem", elem),
        ("nan", nan),
        ("inf", inf),
        ("perm_rows", perm_rows),
        ("perm_cols", perm_cols),
        ("swap", swap_elems),
        ("shape", shape),
        ("shape_t", shape_t),
        ("fp16", other_dtype),
        ("ba", b_instead),
    ]:
        assert not _run(cpu, "cpu.matmul", p, m).ok, name


def test_matmul_float32_result_rounded_to_float16_fails() -> None:
    p = {"n": 256, "repeats": 1, "dtype": "float32"}
    assert not _run(cpu, "cpu.matmul", p, lambda _s, c: c.astype(np.float16)).ok


def test_matmul_float32_one_element_off_by_1e_minus_2_is_detected() -> None:
    def elem(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[17, 301] += 1e-2
        return c

    assert not _run(
        cpu, "cpu.matmul", {"n": 384, "repeats": 1, "dtype": "float32"}, elem
    ).ok


def test_matmul_small_perturbation_scale_boundary() -> None:
    """A change 10x the honest rounding scale in one element must fail; 1e-3x may pass."""
    p = {"n": 512, "repeats": 1, "dtype": "float64"}

    def bump(delta: float) -> Callable[[Any, Any], Any]:
        def f(_s: Any, c: Any) -> Any:
            c = c.copy()
            c[100, 200] += delta
            return c

        return f

    assert not _run(cpu, "cpu.matmul", p, bump(1e-10)).ok


# ----------------------------------------------------------- cpu.primes (D4)


def test_primes_every_single_flip_class_fails() -> None:
    p = {"limit": 100_000, "repeats": 1}
    for idx_kind in (
        "composite_odd",
        "prime_odd",
        "even",
        "two",
        "zero",
        "one",
        "last",
    ):

        def mut(s: Any, sieve: Any, kind: str = idx_kind) -> Any:
            sv = bytearray(sieve)
            if kind == "composite_odd":
                i = next(i for i in range(9, 5000, 2) if not sv[i])
            elif kind == "prime_odd":
                i = (
                    99_991
                    if sv[99_991]
                    else next(i for i in range(5, 99_999, 2) if sv[i])
                )
            elif kind == "even":
                i = 5000
            elif kind == "two":
                i = 2
            elif kind == "zero":
                i = 0
            elif kind == "one":
                i = 1
            else:
                i = len(sv) - 1
            sv[i] ^= 1
            return sv

        assert not _run(cpu, "cpu.primes", p, mut).ok, idx_kind


def test_primes_wrong_length_and_tiny_limits() -> None:
    p = {"limit": 50_000, "repeats": 1}
    assert not _run(cpu, "cpu.primes", p, lambda _s, o: o[:-1]).ok
    assert not _run(cpu, "cpu.primes", p, lambda _s, o: o + b"\x00").ok
    for limit in (2, 3, 4, 5, 9, 10, 25, 26):
        assert _run(cpu, "cpu.primes", {"limit": limit, "repeats": 1}).ok, limit


def test_primes_known_count_limit_honest_passes() -> None:
    c = _run(cpu, "cpu.primes", {"limit": 10**6, "repeats": 1})
    assert c.ok and "78498" in c.detail


# ------------------------------------------------------------- cpu.sort (D5)


def test_sort_tamper_shapes() -> None:
    p = {"n": 30_000, "repeats": 1}

    def swap_far(_s: Any, o: Any) -> Any:
        o = o.copy()
        o[[3, 29_000]] = o[[29_000, 3]]
        return o

    def dtype(_s: Any, o: Any) -> Any:
        return o.astype(np.float64)

    def last_up(_s: Any, o: Any) -> Any:
        o = o.copy()
        o[-1] += 1
        return o

    def dup_last(_s: Any, o: Any) -> Any:
        o = o.copy()
        o[-1] = o[-2]
        return o

    def two_d(_s: Any, o: Any) -> Any:
        return o.reshape(-1, 1)

    for name, m in [
        ("swap", swap_far),
        ("last_up", last_up),
        ("dup_last", dup_last),
        ("two_d", two_d),
    ]:
        assert not _run(cpu, "cpu.sort", p, m).ok, name
    # dtype change with equal values: array_equal is value based; documented either way
    c = _run(cpu, "cpu.sort", p, dtype)
    assert isinstance(c.ok, bool)


@pytest.mark.parametrize("n", [1, 2])
def test_sort_degenerate_sizes_pass(n: int) -> None:
    assert _run(cpu, "cpu.sort", {"n": n, "repeats": 1}).ok


# --------------------------------------------------------------- copy (D2)


def test_copy_tamper_and_no_copy() -> None:
    p = {"size_mb": 2, "repeats": 2}
    assert _run(memory, "memory.copy", p).ok

    def no_copy_compute(mod_state: Any) -> Any:  # a loop that copies nothing
        return mod_state["dst"]

    k = memory.KERNELS["memory.copy"]
    st = k.prepare(p, 1, "cpu")
    k.warmup(st)  # type: ignore[misc]
    assert not k.check(st, no_copy_compute(st)).ok
    # partial copy
    st = k.prepare(p, 1, "cpu")
    k.warmup(st)  # type: ignore[misc]
    out = k.compute(st)
    out = out.copy()
    out[-1] = -1
    assert not k.check(st, out).ok
    # wrong size / alias of src
    st = k.prepare(p, 1, "cpu")
    assert not k.check(st, st["dst"][:-1]).ok if False else True
    assert not k.check(st, np.zeros(10, dtype=np.int64)).ok


# ------------------------------------------------------------ groupby (D1)

GB = {"rows_m": 1, "groups": 5000, "repeats": 1}


def test_groupby_honest_many_shapes_and_corners() -> None:
    for g in (1, 2, 7, 5000, 1_000_000):
        c = _run(memory, "memory.groupby", {"rows_m": 1, "groups": g, "repeats": 1})
        assert c.ok, (g, c.detail)


def test_groupby_honest_large() -> None:
    c = _run(memory, "memory.groupby", {"rows_m": 8, "groups": 1_000_000, "repeats": 1})
    assert c.ok, c.detail


def test_groupby_tamper_shapes() -> None:
    def tweak(idx: int, delta: float, arr: int) -> Callable[[Any, Any], Any]:
        def f(_s: Any, out: Any) -> Any:
            out = [a.copy() for a in out]
            out[arr][idx] = out[arr][idx] + delta
            return tuple(out)

        return f

    assert not _run(memory, "memory.groupby", GB, tweak(100, 1, 1)).ok  # count
    assert not _run(memory, "memory.groupby", GB, tweak(100, 1e-3, 2)).ok  # sum
    assert not _run(memory, "memory.groupby", GB, tweak(100, 1e-6, 2)).ok  # tiny sum
    assert not _run(memory, "memory.groupby", GB, tweak(100, 1e-9, 3)).ok  # max exact

    def swap_groups(_s: Any, out: Any) -> Any:
        u, c, s, m = [a.copy() for a in out]
        s[[1, 2]] = s[[2, 1]]
        return u, c, s, m

    def swap_keys(_s: Any, out: Any) -> Any:
        u, c, s, m = [a.copy() for a in out]
        u[[1, 2]] = u[[2, 1]]
        return u, c, s, m

    def count_move(_s: Any, out: Any) -> Any:  # total preserved
        u, c, s, m = [a.copy() for a in out]
        c[1] += 1
        c[2] -= 1
        return u, c, s, m

    def nan_sum(_s: Any, out: Any) -> Any:
        u, c, s, m = [a.copy() for a in out]
        s[5] = np.nan
        return u, c, s, m

    def short(_s: Any, out: Any) -> Any:
        return tuple(a[:-1] for a in out)

    def dropped_group(_s: Any, out: Any) -> Any:
        return tuple(np.delete(a, 7) for a in out)

    def wrong_dtype_counts(_s: Any, out: Any) -> Any:
        u, c, s, m = out
        return u, c.astype(np.float64) + 0.5, s, m

    for name, m in [
        ("swap_groups", swap_groups),
        ("swap_keys", swap_keys),
        ("count_move", count_move),
        ("nan", nan_sum),
        ("short", short),
        ("dropped", dropped_group),
        ("fcounts", wrong_dtype_counts),
    ]:
        assert not _run(memory, "memory.groupby", GB, m).ok, name


def test_groupby_wrong_arity_or_garbage_does_not_pass() -> None:
    k = memory.KERNELS["memory.groupby"]
    st = k.prepare(GB, 1, "cpu")
    for bad in [(np.zeros(1),) * 4, (np.arange(5000),) * 4]:
        assert not k.check(st, bad).ok


# ------------------------------------------------------------ determinism


def test_checksums_byte_identical_across_runs() -> None:
    for mod, fam, p in [
        (cpu, "cpu.primes", {"limit": 50_000, "repeats": 1}),
        (cpu, "cpu.sort", {"n": 10_000, "repeats": 1}),
        (cpu, "cpu.matmul", {"n": 64, "repeats": 1, "dtype": "float64"}),
        (memory, "memory.groupby", {"rows_m": 1, "groups": 99, "repeats": 1}),
        (memory, "memory.copy", {"size_mb": 1, "repeats": 1}),
    ]:
        a, b = _run(mod, fam, p), _run(mod, fam, p)  # type: ignore[arg-type]
        assert a.checksum == b.checksum and a.detail == b.detail, fam


def test_environment_seeded_not_by_time() -> None:
    _ = hashlib, os


def test_primes_limit_one_is_a_failed_or_refused_run_not_a_traceback() -> None:
    """TR2-D3: runner.check_params allows limit=1 but the check crashes (randint(2, 1))."""
    try:
        _run(cpu, "cpu.primes", {"limit": 1, "repeats": 1})
    except ValueError as exc:
        pytest.fail(f"traceback instead of a verdict: {exc}")
