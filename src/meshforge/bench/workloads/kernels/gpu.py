"""GPU kernels (torch): matmul, conv2d, elementwise, image resize (V0.9 G2, D214).

Job-side code (rules in :mod:`._support`). In a job they always run on
``device="cuda"``: a node without CUDA gets :class:`~._support.Unavailable`.
``device="cpu"`` exists so the kernels themselves can be tested on any host with
torch. Data is generated on the device with a seeded generator; every timed
loop ends with a CUDA synchronize, so the timer measures the GPU work, not the
launch queue.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._support import (
    FREIVALDS_FACTOR,
    Check,
    Kernel,
    choice_param,
    floats_digest,
    int_param,
    require_torch,
    rows_freivalds,
    shape_guard,
    synchronize,
    worse,
)

__all__ = ["KERNELS"]


_CHECK_CHUNK = 1 << 22
"""Elements (or images x pixels) recomputed per step of a full-output check."""


def _setup(params: Mapping[str, Any], seed: int, device: str) -> dict[str, Any]:
    torch = require_torch(device)
    generator = torch.Generator(device=device)
    generator.manual_seed(seed)
    return {
        "torch": torch,
        "device": device,
        "gen": generator,
        "repeats": int_param(params, "repeats"),
    }


def _dtype(state: Mapping[str, Any], name: str) -> Any:
    return getattr(state["torch"], name)


def _sums_digest(tensor: Any, dims: tuple[int, ...]) -> str:
    reduced = tensor.detach().double().sum(dim=dims).flatten().cpu()
    return floats_digest([float(v) for v in reduced.tolist()])


# --------------------------------------------------------------------- matmul


def _matmul_prepare(params: Mapping[str, Any], seed: int, device: str) -> Any:
    state = _setup(params, seed, device)
    torch, n = state["torch"], int_param(params, "n")
    dtype_name = choice_param(params, "dtype", ("float32", "float16"))
    dtype = _dtype(state, dtype_name)
    state.update(
        n=n,
        dtype_name=dtype_name,
        a=torch.randn(n, n, generator=state["gen"], device=device).to(dtype),
        b=torch.randn(n, n, generator=state["gen"], device=device).to(dtype),
    )
    return state


def _matmul_warmup(state: Any) -> None:
    m = min(256, state["n"])
    state["a"][:m, :m] @ state["b"][:m, :m]
    synchronize(state["torch"], state["device"])


def _matmul_compute(state: Any) -> Any:
    a, b = state["a"], state["b"]
    c = a @ b
    for _ in range(state["repeats"] - 1):
        c = a @ b
    synchronize(state["torch"], state["device"])
    return c


def _nonfinite(torch: Any, out: Any) -> Check | None:
    """A failed check when ``out`` holds NaN or inf (tester r2 D2)."""
    if not bool(torch.isfinite(out).all()):
        return Check(False, floats_digest([]), False, "non-finite values in the output")
    return None


def _matmul_check(state: Any, c: Any) -> Check:
    """The same row-wise float64 Freivalds as ``cpu.matmul``
    (:func:`~._support.rows_freivalds`, tester r2 D6): three vectors, every row
    within its rounding tolerance, rows converted to float64 1024 at a time on
    the device. Shape/dtype first; NaN/inf fails."""
    torch, n = state["torch"], state["n"]
    guard = shape_guard(c, (n, n), _dtype(state, state["dtype_name"])) or _nonfinite(
        torch, c
    )
    if guard is not None:
        return guard
    gen = torch.Generator(device=state["device"])
    gen.manual_seed(int(state["gen"].initial_seed()) + 1)
    vectors = [
        torch.randn(n, generator=gen, device=state["device"], dtype=torch.float64)
        for _ in range(3)
    ]
    worst = rows_freivalds(
        state["a"],
        state["b"],
        c,
        vectors,
        float(torch.finfo(c.dtype).eps),
        lambda rows: rows.double(),
        FREIVALDS_FACTOR[state["dtype_name"]],
    )
    return Check(
        ok=worst <= 1.0,
        checksum=_sums_digest(c, (0,)),
        exact=False,
        detail=f"row-wise freivalds: worst row {worst:.3f} of its tolerance",
    )


# --------------------------------------------------------------------- conv2d


def _conv_prepare(params: Mapping[str, Any], seed: int, device: str) -> Any:
    state = _setup(params, seed, device)
    torch = state["torch"]
    batch, channels, size = (
        int_param(params, k) for k in ("batch", "channels", "size")
    )
    state.update(
        x=torch.randn(
            batch, channels, size, size, generator=state["gen"], device=device
        ),
        w=torch.randn(channels, channels, 3, 3, generator=state["gen"], device=device)
        / (3.0 * channels**0.5),
    )
    return state


def _conv(state: Any) -> Any:
    return state["torch"].nn.functional.conv2d(state["x"], state["w"], padding=1)


def _conv_warmup(state: Any) -> None:
    _conv(state)
    synchronize(state["torch"], state["device"])


def _conv_compute(state: Any) -> Any:
    y = _conv(state)
    for _ in range(state["repeats"] - 1):
        y = _conv(state)
    synchronize(state["torch"], state["device"])
    return y


def _conv_check(state: Any, y: Any) -> Check:
    """The **whole** output recomputed in float64 on the device, a few images at
    a time (tester r2 D5). Each element must be within 2e-3 of
    ``conv(|x|, |w|)`` at that position - the size of the terms it sums - which
    allows cuDNN's TF32 rounding on a GPU but fails one wrong element. Shape and
    dtype first; NaN/inf fails."""
    torch, x, w = state["torch"], state["x"], state["w"]
    guard = shape_guard(y, tuple(x.shape), x.dtype) or _nonfinite(torch, y)
    if guard is not None:
        return guard
    conv = torch.nn.functional.conv2d
    w64, w_abs = w.double(), w.double().abs()
    per_image = max(1, x[0].numel())
    step = max(1, _CHECK_CHUNK // per_image)
    worst = 0.0
    for start in range(0, x.shape[0], step):
        part = slice(start, start + step)
        x64 = x[part].double()
        expected = conv(x64, w64, padding=1)
        scale = conv(x64.abs(), w_abs, padding=1) + 1e-30
        error = (y[part].double() - expected).abs() / scale
        worst = worse(worst, float(error.max()))
    return Check(
        ok=worst <= 2e-3,
        checksum=_sums_digest(y, (0, 2, 3)),
        exact=False,
        detail=f"whole output vs float64 recompute: worst error {worst:.3e} of the term size",
    )


# ---------------------------------------------------------------- elementwise

_COEFFICIENTS = (0.5, 0.6, 0.7, 0.8, 0.9)


def _chain(torch: Any, x: Any, ops: int) -> Any:
    """``y = sin(x)``, then ``ops`` times ``y = x + c_k * y`` (one fused read-read-
    write kernel each; ``|c_k| < 1`` keeps the values bounded)."""
    y = torch.sin(x)
    for k in range(ops):
        y = torch.add(x, y, alpha=_COEFFICIENTS[k % len(_COEFFICIENTS)])
    return y


def _elementwise_prepare(params: Mapping[str, Any], seed: int, device: str) -> Any:
    state = _setup(params, seed, device)
    count = int_param(params, "elements_m") * 1_000_000
    state.update(
        x=state["torch"].randn(count, generator=state["gen"], device=device),
        ops=int_param(params, "ops"),
    )
    return state


def _elementwise_warmup(state: Any) -> None:
    _chain(state["torch"], state["x"][:65_536], state["ops"])
    synchronize(state["torch"], state["device"])


def _elementwise_compute(state: Any) -> Any:
    torch, x, ops = state["torch"], state["x"], state["ops"]
    y = _chain(torch, x, ops)
    for _ in range(state["repeats"] - 1):
        y = _chain(torch, x, ops)
    synchronize(torch, state["device"])
    return y


def _elementwise_check(state: Any, y: Any) -> Check:
    """The **whole** output recomputed in float64 on the same device, in chunks
    (tester r1 D8: not only a prefix). Bandwidth-bound like the run itself;
    recorded in ``check_ms``."""
    torch, x, ops = state["torch"], state["x"], state["ops"]
    guard = shape_guard(y, tuple(x.shape), x.dtype) or _nonfinite(torch, y)
    if guard is not None:
        return guard
    worst = 0.0
    for start in range(0, x.numel(), _CHECK_CHUNK):
        part = slice(start, start + _CHECK_CHUNK)
        expected = _chain(torch, x[part].double(), ops)
        error = (y[part].double() - expected).abs() / (1.0 + expected.abs())
        worst = worse(worst, float(error.max()))
    tolerance = 1e-5 * (ops + 2)
    return Check(
        ok=worst <= tolerance,
        checksum=floats_digest([round(float(y.double().sum()), 3)]),
        exact=False,
        detail=f"whole output vs float64 recompute: worst relative error {worst:.3e} "
        f"(tolerance {tolerance:.1e})",
    )


# --------------------------------------------------------------------- resize


def _resize_prepare(params: Mapping[str, Any], seed: int, device: str) -> Any:
    state = _setup(params, seed, device)
    batch, size = int_param(params, "batch"), int_param(params, "size")
    state.update(
        x=state["torch"].rand(
            batch, 3, size, size, generator=state["gen"], device=device
        ),
        out_size=int_param(params, "out_size"),
    )
    return state


def _resize(state: Any, x: Any) -> Any:
    return state["torch"].nn.functional.interpolate(
        x,
        size=(state["out_size"], state["out_size"]),
        mode="bilinear",
        align_corners=False,
        antialias=False,
    )


def _resize_warmup(state: Any) -> None:
    _resize(state, state["x"][:1])
    synchronize(state["torch"], state["device"])


def _resize_compute(state: Any) -> Any:
    y = _resize(state, state["x"])
    for _ in range(state["repeats"] - 1):
        y = _resize(state, state["x"])
    synchronize(state["torch"], state["device"])
    return y


def _resize_check(state: Any, y: Any) -> Check:
    """Every image resized again in float64 on the same device, a few images at a
    time (tester r1 D8: not only image 0). A wrong shape is a failed check, not an
    exception (D9)."""
    x = state["x"]
    want = (x.shape[0], x.shape[1], state["out_size"], state["out_size"])
    guard = shape_guard(y, want, x.dtype) or _nonfinite(state["torch"], y)
    if guard is not None:
        return guard
    per_image = 3 * (x.shape[2] ** 2 + state["out_size"] ** 2)
    step = max(1, _CHECK_CHUNK // per_image)
    worst = 0.0
    for start in range(0, x.shape[0], step):
        part = slice(start, start + step)
        expected = _resize(state, x[part].double())
        worst = worse(worst, float((y[part].double() - expected).abs().max()))
    return Check(
        ok=worst <= 1e-5,
        checksum=_sums_digest(y, (2, 3)),
        exact=False,
        detail=f"all {x.shape[0]} images vs float64 recompute: max error {worst:.3e}",
    )


KERNELS: dict[str, Kernel] = {
    "gpu.matmul": Kernel(
        _matmul_prepare, _matmul_compute, _matmul_check, _matmul_warmup
    ),
    "gpu.conv2d": Kernel(_conv_prepare, _conv_compute, _conv_check, _conv_warmup),
    "gpu.elementwise": Kernel(
        _elementwise_prepare,
        _elementwise_compute,
        _elementwise_check,
        _elementwise_warmup,
    ),
    "gpu.resize": Kernel(
        _resize_prepare, _resize_compute, _resize_check, _resize_warmup
    ),
}
