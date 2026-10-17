"""Memory kernels: array copy, numpy groupby (V0.9 G2, D214).

Job-side code (rules in :mod:`._support`). Both are bandwidth-bound: the hint
scales them with the node's ``mem_triad_gb_s`` score.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._support import Check, Kernel, digest, int_param, require_numpy

__all__ = ["KERNELS"]

MIB = 1024 * 1024

# ----------------------------------------------------------------------- copy


def _copy_prepare(params: Mapping[str, Any], seed: int, _device: str) -> Any:
    np = require_numpy()
    count = int_param(params, "size_mb") * MIB // 8
    src = np.random.default_rng(seed).integers(0, 2**62, size=count, dtype=np.int64)
    return {
        "np": np,
        "src": src,
        "dst": np.empty_like(src),
        "repeats": int_param(params, "repeats"),
        "seed": seed,
    }


def _copy_warmup(state: Any) -> None:
    # One full copy: every destination page is touched (page faults) before timing.
    state["np"].copyto(state["dst"], state["src"])


def _copy_compute(state: Any) -> Any:
    np, src, dst = state["np"], state["src"], state["dst"]
    for _ in range(state["repeats"]):
        np.copyto(dst, src)
    return dst


def _copy_check(state: Any, dst: Any) -> Check:
    np = state["np"]
    equal = bool(np.array_equal(dst, state["src"]))
    return Check(
        ok=equal,
        checksum=digest(
            str(int(dst.sum(dtype=np.uint64))).encode(), str(dst.size).encode()
        ),
        exact=True,
        detail=f"destination equals source {equal}",
    )


# -------------------------------------------------------------------- groupby


def _groupby_prepare(params: Mapping[str, Any], seed: int, _device: str) -> Any:
    np = require_numpy()
    rows = int_param(params, "rows_m") * 1_000_000
    groups = int_param(params, "groups")
    rng = np.random.default_rng(seed)
    return {
        "np": np,
        "keys": rng.integers(0, groups, size=rows, dtype=np.int64),
        "values": rng.standard_normal(rows),
        "groups": groups,
        "repeats": int_param(params, "repeats"),
    }


def _groupby(np: Any, keys: Any, values: Any) -> tuple[Any, Any, Any, Any]:
    """Sort-based group-by (what pandas does): key, count, sum, max per group."""
    order = np.argsort(keys, kind="stable")
    k = keys[order]
    v = values[order]
    starts = np.flatnonzero(np.concatenate(([True], k[1:] != k[:-1])))
    counts = np.diff(np.concatenate((starts, [k.size])))
    return k[starts], counts, np.add.reduceat(v, starts), np.maximum.reduceat(v, starts)


def _groupby_warmup(state: Any) -> None:
    _groupby(state["np"], state["keys"][:65_536], state["values"][:65_536])


def _groupby_compute(state: Any) -> Any:
    np, keys, values = state["np"], state["keys"], state["values"]
    out = _groupby(np, keys, values)
    for _ in range(state["repeats"] - 1):
        out = _groupby(np, keys, values)
    return out


def _groupby_check(state: Any, out: tuple[Any, Any, Any, Any]) -> Check:
    np = state["np"]
    uniq, counts, sums, _maxs = out
    expected = np.bincount(state["keys"], minlength=state["groups"])
    counts_ok = bool(np.array_equal(expected[uniq], counts)) and int(
        counts.sum()
    ) == int(state["keys"].size)
    total = float(state["values"].sum())
    sum_error = abs(float(sums.sum()) - total)
    sums_ok = sum_error <= 1e-6 * max(1.0, float(np.abs(state["values"]).sum()))
    return Check(
        ok=counts_ok and sums_ok,
        checksum=digest(uniq.tobytes(), counts.tobytes()),
        exact=True,
        detail=f"counts match bincount {counts_ok}, sum error {sum_error:.3e}",
    )


KERNELS: dict[str, Kernel] = {
    "memory.copy": Kernel(_copy_prepare, _copy_compute, _copy_check, _copy_warmup),
    "memory.groupby": Kernel(
        _groupby_prepare, _groupby_compute, _groupby_check, _groupby_warmup
    ),
}
