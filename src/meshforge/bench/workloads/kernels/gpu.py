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
    Check,
    Kernel,
    choice_param,
    floats_digest,
    int_param,
    require_torch,
    synchronize,
)

__all__ = ["KERNELS"]


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


def _matmul_check(state: Any, c: Any) -> Check:
    """Freivalds in float32: ``C x`` vs ``A (B x)``."""
    torch = state["torch"]
    x = torch.randn(state["n"], 1, generator=state["gen"], device=state["device"])
    a, b = state["a"].float(), state["b"].float()
    lhs = c.float() @ x
    rhs = a @ (b @ x)
    error = float(
        torch.linalg.norm(lhs - rhs) / torch.linalg.norm(rhs).clamp_min(1e-30)
    )
    tolerance = 2e-3 if state["dtype_name"] == "float32" else 5e-2
    return Check(
        ok=error <= tolerance,
        checksum=_sums_digest(c, (0,)),
        exact=False,
        detail=f"freivalds relative error {error:.3e} (tolerance {tolerance:g})",
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
    """16 seeded output elements recomputed in float64 on the CPU."""
    torch = state["torch"]
    x = torch.nn.functional.pad(state["x"], (1, 1, 1, 1)).detach().cpu().double()
    w = state["w"].detach().cpu().double()
    out = y.detach().cpu().double()
    batch, channels, size, _ = state["x"].shape
    picks = torch.randint(
        0, 2**31 - 1, (16, 4), generator=torch.Generator().manual_seed(7)
    )
    worst = 0.0
    for row in picks.tolist():
        b, o, i, j = row[0] % batch, row[1] % channels, row[2] % size, row[3] % size
        patch = x[b, :, i : i + 3, j : j + 3]
        manual = float((patch * w[o]).sum())
        scale = float((patch.abs() * w[o].abs()).sum()) or 1.0
        worst = max(worst, abs(manual - float(out[b, o, i, j])) / scale)
    return Check(
        ok=worst <= 1e-2,
        checksum=_sums_digest(y, (0, 2, 3)),
        exact=False,
        detail=f"worst sampled relative error {worst:.3e}",
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


_CHECK_CHUNK = 1 << 22
"""Elements (or images x pixels) recomputed per step of a full-output check."""


def _elementwise_check(state: Any, y: Any) -> Check:
    """The **whole** output recomputed in float64 on the same device, in chunks
    (tester r1 D8: not only a prefix). Bandwidth-bound like the run itself;
    recorded in ``check_ms``."""
    torch, x, ops = state["torch"], state["x"], state["ops"]
    if tuple(y.shape) != tuple(x.shape):
        return Check(False, floats_digest([]), False, f"output shape {tuple(y.shape)}")
    worst = 0.0
    for start in range(0, x.numel(), _CHECK_CHUNK):
        part = slice(start, start + _CHECK_CHUNK)
        expected = _chain(torch, x[part].double(), ops)
        error = (y[part].double() - expected).abs() / (1.0 + expected.abs())
        worst = max(worst, float(error.max()))
    if worst != worst:  # NaN in the output
        worst = float("inf")
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
    if tuple(y.shape) != want:
        return Check(
            False, floats_digest([]), False, f"output shape {tuple(y.shape)} != {want}"
        )
    per_image = 3 * (x.shape[2] ** 2 + state["out_size"] ** 2)
    step = max(1, _CHECK_CHUNK // per_image)
    worst = 0.0
    for start in range(0, x.shape[0], step):
        part = slice(start, start + step)
        expected = _resize(state, x[part].double())
        worst = max(worst, float((y[part].double() - expected).abs().max()))
    if worst != worst:
        worst = float("inf")
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
