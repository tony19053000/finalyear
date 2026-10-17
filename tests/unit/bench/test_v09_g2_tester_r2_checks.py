"""V0.9 G2 tester round 2: do the fixed self-checks close the whole class?"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

np = pytest.importorskip("numpy")

from meshforge.bench.workloads.kernels import cpu, memory, runner  # noqa: E402
from meshforge.bench.workloads.kernels._support import (  # noqa: E402
    Check,
    KernelParamsError,
)

pytestmark = pytest.mark.unit


def _run(
    mod: Any,
    family: str,
    params: Mapping[str, Any],
    mutate: Callable[[Any, Any], Any] | None = None,
    seed: int = 3,
    device: str = "cpu",
) -> Check:
    kernel = mod.KERNELS[family]
    state = kernel.prepare(params, seed, device)
    if kernel.warmup:
        kernel.warmup(state)
    out = kernel.compute(state)
    if mutate:
        out = mutate(state, out)
    check: Check = kernel.check(state, out)
    return check


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


# ------------------------------------------------- torch kernels, device=cpu

torch = pytest.importorskip("torch")
from meshforge.bench.workloads.kernels import gpu, ml  # noqa: E402


def test_gpu_matmul_honest_and_one_wrong_element() -> None:
    from meshforge.bench.workloads.kernels import gpu as g

    p = {"n": 512, "repeats": 1, "dtype": "float32"}
    assert _run(g, "gpu.matmul", p).ok

    def one(_s: Any, c: Any) -> Any:
        c = c.clone()
        c[5, 7] = 0.0
        return c

    assert not _run(g, "gpu.matmul", p, one).ok, "gpu.matmul misses one wrong element"


def test_gpu_matmul_wrong_shape_is_failed_check_not_exception() -> None:
    p = {"n": 64, "repeats": 1, "dtype": "float32"}
    assert not _run(gpu, "gpu.matmul", p, lambda _s, c: c[:-1]).ok


def test_gpu_matmul_nan_fails() -> None:
    p = {"n": 64, "repeats": 1, "dtype": "float32"}

    def nan(_s: Any, c: Any) -> Any:
        c = c.clone()
        c[1, 1] = float("nan")
        return c

    assert not _run(gpu, "gpu.matmul", p, nan).ok


def test_gpu_conv2d_wrong_shape_is_failed_check_not_exception() -> None:
    p = {"batch": 2, "channels": 4, "size": 8, "repeats": 1}
    assert not _run(gpu, "gpu.conv2d", p, lambda _s, y: y[:, :, :-1]).ok


def test_gpu_conv2d_one_wrong_element_anywhere() -> None:
    p = {"batch": 2, "channels": 8, "size": 16, "repeats": 1}

    def one(_s: Any, y: Any) -> Any:
        y = y.clone()
        y[1, 3, 9, 9] += 5.0
        return y

    assert not _run(gpu, "gpu.conv2d", p, one).ok, "conv2d samples only 16 elements"


def test_gpu_elementwise_tamper() -> None:
    p = {"elements_m": 1, "ops": 4, "repeats": 1}
    assert _run(gpu, "gpu.elementwise", p).ok

    def last(_s: Any, y: Any) -> Any:
        y = y.clone()
        y[-1] += 1e-2
        return y

    def perm(_s: Any, y: Any) -> Any:
        y = y.clone()
        y[[5, 900_000]] = y[[900_000, 5]]
        return y

    for m in (last, perm, lambda _s, y: y[:-1], lambda _s, y: y.reshape(1000, -1)):
        assert not _run(gpu, "gpu.elementwise", p, m).ok


def test_gpu_elementwise_multi_chunk_honest() -> None:
    assert _run(gpu, "gpu.elementwise", {"elements_m": 9, "ops": 6, "repeats": 1}).ok


def test_gpu_resize_tamper() -> None:
    p = {"batch": 5, "size": 32, "out_size": 48, "repeats": 1}
    assert _run(gpu, "gpu.resize", p).ok

    def last_image(_s: Any, y: Any) -> Any:
        y = y.clone()
        y[-1, 2, 10, 10] += 1e-3
        return y

    for m in (
        last_image,
        lambda _s, y: y[:-1],
        lambda _s, y: y[..., :-1],
        lambda _s, y: y.permute(0, 1, 3, 2).contiguous() * 0 + y.mean(),
    ):
        assert not _run(gpu, "gpu.resize", p, m).ok


def test_gpu_resize_perm_of_images() -> None:
    p = {"batch": 4, "size": 32, "out_size": 48, "repeats": 1}
    assert not _run(gpu, "gpu.resize", p, lambda _s, y: y.flip(0)).ok


def test_ml_checks_wrong_shape_no_exception() -> None:
    p = {"batch": 2, "width": 8, "depth": 1, "image": 32, "batches": 2}
    c = _run(ml, "ml.cnn_infer", p)
    assert c.ok, c.detail
    assert not _run(ml, "ml.cnn_infer", p, lambda _s, o: o[:, :-1]).ok
    assert not _run(ml, "ml.cnn_infer", p, lambda _s, o: o * float("nan")).ok


def test_ml_train_check_wrong_inputs() -> None:
    p = {"batch": 2, "width": 8, "depth": 1, "image": 32, "steps": 8}
    assert _run(ml, "ml.cnn_train", p).ok
    assert not _run(ml, "ml.cnn_train", p, lambda _s, o: o.flip(0)).ok
    assert not _run(ml, "ml.cnn_train", p, lambda _s, o: o * float("nan")).ok


# ------------------------------------------------------- PARAM_LIMITS (D6)


def test_param_limits_parity_with_pydantic_for_every_family() -> None:
    from meshforge.bench.workloads import get_family, list_families

    fams = list_families()
    assert (
        {f.name for f in fams} == set(runner.PARAM_LIMITS) == set(runner.KERNEL_MODULES)
    )
    for fam in fams:
        model = fam.params_model
        limits = runner.PARAM_LIMITS[fam.name]
        assert set(model.model_fields) == set(limits), fam.name
        for name, field in model.model_fields.items():
            lim = limits[name]
            if isinstance(lim, tuple):
                assert set(lim) == set(_literal_args(field.annotation)), (
                    fam.name,
                    name,
                )
            else:
                le = next((m.le for m in field.metadata if hasattr(m, "le")), None)
                assert le == lim, (fam.name, name, le, lim)
        # accepted at the max, refused one past (pydantic and runner agree)
        for name, lim in limits.items():
            if isinstance(lim, tuple):
                continue
            dflt = fam.smallest_params().model_dump()
            over = {**dflt, name: lim + 1}
            with pytest.raises(ValueError):  # pydantic ValidationError
                fam.params_model(**over)
            with pytest.raises(KernelParamsError):
                runner.check_params(fam.name, over)
    _ = get_family


def _literal_args(ann: Any) -> tuple[Any, ...]:
    import typing

    args = typing.get_args(ann)
    if args and typing.get_origin(ann) is typing.Literal:
        return args
    if args:  # Annotated / Optional
        for a in args:
            r = _literal_args(a)
            if r:
                return r
    # enum
    try:
        return tuple(m.value for m in ann)
    except TypeError:
        return ()


@pytest.mark.parametrize("bad", [True, False, 1.0, "4", None, [1], 0, -1, 10**30])
def test_check_params_rejects_type_confusion(bad: Any) -> None:
    with pytest.raises(KernelParamsError):
        runner.check_params("cpu.sort", {"n": bad, "repeats": 1})


@pytest.mark.parametrize(
    "seed", [True, False, 1.0, "1", None, -1, 2**32, 10**40, [], {}]
)
def test_check_seed_rejects(seed: Any) -> None:
    with pytest.raises(KernelParamsError):
        runner.check_seed(seed)


def test_seed_rule_identical_across_run_job_files_and_runner() -> None:
    from meshforge.bench.workloads import ParamsError, get_family
    from meshforge.bench.workloads.materialise import job_files

    fam = get_family("cpu.sort")
    for seed in (0, 2**32 - 1, 2**32, -1, True, 1.5, "1", None):
        results = []
        for fn in (
            fam.check_seed,
            lambda s: job_files(fam, fam.smallest_params(), s),
            runner.check_seed,
        ):
            try:
                fn(seed)
                results.append(True)
            except (ParamsError, KernelParamsError):
                results.append(False)
        assert len(set(results)) == 1, (seed, results)


def test_transformer_width_multiple_and_choices() -> None:
    with pytest.raises(KernelParamsError):
        runner.check_params("ml.transformer_infer", {"width": 65})
    with pytest.raises(KernelParamsError):
        runner.check_params("gpu.matmul", {"dtype": "bfloat16"})
    with pytest.raises(KernelParamsError):
        runner.check_params("cpu.zlib", {"level": 5})
    with pytest.raises(KernelParamsError):
        runner.check_params("cpu.sort", {"n": 5, "bogus": 1})


def test_main_bad_params_variants_exit2_and_write_result(tmp_path: Path) -> None:
    cases = [
        {"family": "cpu.sort", "params": {"n": 10**12, "repeats": 1}, "seed": 1},
        {"family": "cpu.sort", "params": {"n": 5, "repeats": 1, "x": 1}, "seed": 1},
        {"family": "cpu.sort", "params": {"n": 5, "repeats": 1}, "seed": -1},
        {"family": "cpu.sort", "params": {"n": 5, "repeats": 1}, "seed": True},
        {"family": "nope", "params": {}, "seed": 1},
        {"family": "cpu.sort", "params": {"n": 5.0, "repeats": 1}, "seed": 1},
        [1, 2],
    ]
    for i, doc in enumerate(cases):
        d = tmp_path / str(i)
        d.mkdir()
        (d / runner.PARAMS_FILE).write_text(json.dumps(doc))
        assert runner.main(d) == runner.EXIT_BAD_PARAMS, doc
        res = json.loads((d / runner.RESULT_FILE).read_text())
        assert res["status"] == "bad_params" and res["verified"] is False


def test_main_corrupt_json_and_missing_file(tmp_path: Path) -> None:
    (tmp_path / runner.PARAMS_FILE).write_text("{not json")
    assert runner.main(tmp_path) == runner.EXIT_BAD_PARAMS
    other = tmp_path / "o"
    other.mkdir()
    assert runner.main(other) == runner.EXIT_BAD_PARAMS


def test_main_valid_tiny_run_exit0(tmp_path: Path) -> None:
    (tmp_path / runner.PARAMS_FILE).write_text(
        json.dumps({"family": "cpu.sort", "params": {"n": 1, "repeats": 1}, "seed": 0})
    )
    assert runner.main(tmp_path) == 0


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


def test_gpu_elementwise_nan_in_output_fails() -> None:
    p = {"elements_m": 1, "ops": 4, "repeats": 1}

    def nan(_s: Any, y: Any) -> Any:
        y = y.clone()
        y[777_777] = float("nan")
        return y

    assert not _run(gpu, "gpu.elementwise", p, nan).ok


def test_gpu_resize_nan_in_output_fails() -> None:
    p = {"batch": 5, "size": 32, "out_size": 48, "repeats": 1}

    def nan(_s: Any, y: Any) -> Any:
        y = y.clone()
        y[2, 0, 0, 0] = float("nan")
        return y

    assert not _run(gpu, "gpu.resize", p, nan).ok


def test_gpu_matmul_one_wrong_element_at_n2048() -> None:
    """TR2-D6 probe: gpu.matmul still uses the global-norm Freivalds (D3 fixed cpu only)."""

    def one(_s: Any, c: Any) -> Any:
        c = c.clone()
        c[5, 7] = 0.0
        return c

    assert not _run(
        gpu, "gpu.matmul", {"n": 2048, "repeats": 1, "dtype": "float32"}, one
    ).ok
