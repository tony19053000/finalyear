"""V0.9 G2 fix round 2: the three self-check rules hold for EVERY kernel.

The rules (``kernels/_support.py``): a wrong shape / type / dtype is
``ok=False`` - never an exception; a NaN or inf anywhere in a float output
fails; worst-error reductions keep NaN (``worse``). Every family is listed, so a
new kernel that skips a rule fails here, not only the repro of one defect.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import pytest

from meshforge.bench.workloads import list_families
from meshforge.bench.workloads.kernels import runner
from meshforge.bench.workloads.kernels._support import Check, worse

pytestmark = pytest.mark.unit

TINY: dict[str, dict[str, Any]] = {
    "cpu.matmul": {"n": 48, "repeats": 1, "dtype": "float32"},
    "cpu.primes": {"limit": 50_000, "repeats": 1},
    "cpu.sort": {"n": 5_000, "repeats": 1},
    "cpu.zlib": {"size_mb": 1, "level": 1, "repeats": 1},
    "cpu.sha256": {"size_mb": 1, "chunk_kb": 64},
    "cpu.integrate": {"points_m": 1, "repeats": 1},
    "memory.copy": {"size_mb": 1, "repeats": 1},
    "memory.groupby": {"rows_m": 1, "groups": 20, "repeats": 1},
    "gpu.matmul": {"n": 48, "repeats": 1, "dtype": "float32"},
    "gpu.conv2d": {"batch": 2, "channels": 4, "size": 8, "repeats": 1},
    "gpu.elementwise": {"elements_m": 1, "ops": 2, "repeats": 1},
    "gpu.resize": {"batch": 2, "size": 8, "out_size": 12, "repeats": 1},
    "ml.cnn_infer": {"batch": 2, "width": 8, "depth": 2, "image": 32, "batches": 2},
    "ml.cnn_train": {"batch": 8, "width": 8, "depth": 2, "image": 32, "steps": 20},
    "ml.transformer_infer": {
        "batch": 2,
        "seq_len": 8,
        "width": 64,
        "depth": 1,
        "batches": 2,
    },
}
TORCH = {n for n in TINY if n.split(".")[0] in ("gpu", "ml")}
NUMPY = {"cpu.matmul", "cpu.sort", "cpu.integrate", "memory.copy", "memory.groupby"}
FLOAT_OUTPUT = {"cpu.matmul", "cpu.integrate", "memory.groupby"} | TORCH


def test_every_family_is_covered() -> None:
    assert set(TINY) == {f.name for f in list_families()}


def _run(family: str, mutate: Callable[[Any], Any]) -> Check:
    if family in TORCH:
        pytest.importorskip("torch", reason="needs torch")
    if family in NUMPY:
        pytest.importorskip("numpy", reason="needs numpy")
    import importlib

    module = importlib.import_module(
        f"meshforge.bench.workloads.kernels.{runner.KERNEL_MODULES[family]}"
    )
    kernel = module.KERNELS[family]
    state = kernel.prepare(TINY[family], 3, "cpu")
    if kernel.warmup:
        kernel.warmup(state)
    out = kernel.compute(state)
    assert kernel.check(state, out).ok, family  # honest output passes
    check: Check = kernel.check(state, mutate(out))
    return check


GARBAGE: list[Any] = ["garbage", None, 7, (), b""]


@pytest.mark.parametrize("family", sorted(TINY))
@pytest.mark.parametrize("garbage", GARBAGE, ids=repr)
def test_garbage_output_is_a_failed_check_not_an_exception(
    family: str, garbage: Any
) -> None:
    if family == "cpu.zlib" and garbage == b"":
        pytest.skip("an empty zlib stream is a zlib.error: the job fails loudly")
    assert not _run(family, lambda _o: garbage).ok


def _truncate(out: Any) -> Any:
    if isinstance(out, tuple):
        return tuple(a[:-1] for a in out)
    if isinstance(out, float):
        return [out]
    return out[:-1]


@pytest.mark.parametrize("family", sorted(TINY))
def test_wrong_shape_is_a_failed_check_not_an_exception(family: str) -> None:
    if family == "cpu.zlib":
        pytest.skip("a truncated zlib stream is a zlib.error: the job fails loudly")
    assert not _run(family, _truncate).ok


def _poison(value: float) -> Callable[[Any], Any]:
    def mutate(out: Any) -> Any:
        if isinstance(out, float):
            return value
        if isinstance(out, tuple):  # groupby: sums
            sums = out[2].copy()
            sums[-1] = value
            return (out[0], out[1], sums, out[3])
        copy = out.clone() if hasattr(out, "clone") else out.copy()
        copy.reshape(-1)[-1] = value
        return copy

    return mutate


@pytest.mark.parametrize("family", sorted(FLOAT_OUTPUT))
@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf], ids=str)
def test_a_non_finite_value_anywhere_fails(family: str, value: float) -> None:
    assert not _run(family, _poison(value)).ok


def test_worse_keeps_nan() -> None:
    assert worse(0.5, math.nan) == math.inf
    assert worse(math.nan, 0.1) == math.inf
    assert worse(0.5, 0.2) == 0.5
    assert max(0.5, math.nan) == 0.5  # why `worse` exists
