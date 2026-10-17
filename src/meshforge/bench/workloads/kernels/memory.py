"""Memory kernels: array copy, numpy groupby (V0.9 G2, D214).

Job-side code (rules in :mod:`._support`). Both are bandwidth-bound: the hint
scales them with the node's ``mem_triad_gb_s`` score.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ._support import Check, Kernel, digest, int_param, require_numpy, shape_guard

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


_POISON = -1
"""Written over the destination after the warm-up. The source holds only values
``0 .. 2**62``, so a destination still holding it was never copied by the timed
loop (tester r1 D2)."""


def _copy_warmup(state: Any) -> None:
    # One full copy: every destination page is touched (page faults) before
    # timing; then the destination is poisoned so only the timed loop can make
    # it equal to the source.
    state["np"].copyto(state["dst"], state["src"])
    state["dst"].fill(_POISON)


def _copy_compute(state: Any) -> Any:
    np, src, dst = state["np"], state["src"], state["dst"]
    for _ in range(state["repeats"]):
        np.copyto(dst, src)
    return dst


def _copy_check(state: Any, dst: Any) -> Check:
    np = state["np"]
    guard = shape_guard(dst, state["src"].shape, state["src"].dtype)
    if guard is not None:
        return guard
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


def _groupby_check(state: Any, out: Any) -> Check:
    """Every group's key, count, sum and max against an independent scatter
    recompute on the unsorted input (``bincount`` with and without weights,
    ``maximum.at``) - not just totals (tester r1 D1). About one pass over the
    rows plus a scatter; recorded in ``check_ms``."""
    np = state["np"]
    keys, values, groups = state["keys"], state["values"], state["groups"]
    if not isinstance(out, tuple) or len(out) != 4:
        return Check(
            False, digest(b"arity"), True, "output is not (keys, counts, sums, max)"
        )
    uniq, counts, sums, maxs = out
    finite = all(
        bool(np.isfinite(a).all()) for a in (sums, maxs) if hasattr(a, "dtype")
    )
    if not finite:
        return Check(False, digest(b"non-finite"), True, "non-finite sums or max")
    counts_ref = np.bincount(keys, minlength=groups)
    present = np.flatnonzero(counts_ref)
    sums_ref = np.bincount(keys, weights=values, minlength=groups)[present]
    maxs_ref = np.full(groups, -np.inf)
    np.maximum.at(maxs_ref, keys, values)
    maxs_ref = maxs_ref[present]
    shapes = all(
        getattr(a, "shape", None) == present.shape for a in (uniq, counts, sums, maxs)
    )
    keys_ok = shapes and bool(np.array_equal(uniq, present))
    counts_ok = keys_ok and bool(np.array_equal(counts, counts_ref[present]))
    # Sums are float: the two summation orders differ by rounding only.
    scale = np.bincount(keys, weights=np.abs(values), minlength=groups)[present]
    sums_ok = keys_ok and bool(
        np.all(
            np.abs(sums - sums_ref)
            <= 1e-12 * scale * max(1.0, np.log2(len(keys))) + 1e-300
        )
    )
    maxs_ok = keys_ok and bool(np.array_equal(maxs, maxs_ref))  # max is exact
    return Check(
        ok=counts_ok and sums_ok and maxs_ok,
        checksum=digest(uniq.tobytes(), counts.tobytes())
        if shapes
        else digest(b"shape"),
        exact=True,
        detail=f"per group vs scatter recompute: keys {keys_ok}, counts {counts_ok}, "
        f"sums {sums_ok}, max {maxs_ok}",
    )


KERNELS: dict[str, Kernel] = {
    "memory.copy": Kernel(_copy_prepare, _copy_compute, _copy_check, _copy_warmup),
    "memory.groupby": Kernel(
        _groupby_prepare, _groupby_compute, _groupby_check, _groupby_warmup
    ),
}
