"""V0.9 G2 tester round 2 (split from ``..._checks.py``, G3 item 0.2): the fixed
self-checks of the torch kernels, run on ``device="cpu"``.
"""

from __future__ import annotations

from typing import Any

import pytest

np = pytest.importorskip("numpy")


from mf_bench_checks import run_kernel_check as _run  # noqa: E402  tests/_support

pytestmark = pytest.mark.unit


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
