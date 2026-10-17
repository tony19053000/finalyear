"""CPU kernels: matmul, primes, sort, zlib, sha256, integrate (V0.9 G2, D214).

Job-side code: see :mod:`._support` for the rules (stdlib at import time,
numpy imported when a kernel runs). Every kernel is deterministic in
``(params, seed)``; ``check`` verifies an independent property of the output.
"""

from __future__ import annotations

import hashlib
import math
import random
import zlib
from collections.abc import Mapping
from typing import Any

from ._support import (
    Check,
    Kernel,
    KernelParamsError,
    choice_param,
    digest,
    floats_digest,
    int_param,
    require_numpy,
)

__all__ = ["KERNELS"]

MIB = 1024 * 1024

# --------------------------------------------------------------------- matmul


def _matmul_prepare(params: Mapping[str, Any], seed: int, _device: str) -> Any:
    np = require_numpy()
    n = int_param(params, "n")
    dtype = choice_param(params, "dtype", ("float64", "float32"))
    rng = np.random.default_rng(seed)
    a = rng.standard_normal((n, n), dtype=dtype)
    b = rng.standard_normal((n, n), dtype=dtype)
    return {
        "np": np,
        "a": a,
        "b": b,
        "repeats": int_param(params, "repeats"),
        "seed": seed,
        "dtype": dtype,
    }


def _matmul_warmup(state: Any) -> None:
    m = min(256, state["a"].shape[0])
    state["a"][:m, :m] @ state["b"][:m, :m]


def _matmul_compute(state: Any) -> Any:
    a, b = state["a"], state["b"]
    c = a @ b
    for _ in range(state["repeats"] - 1):
        c = a @ b
    return c


def _matmul_check(state: Any, c: Any) -> Check:
    """Freivalds: ``C x == A (B x)`` for a random ``x`` (O(n^2), not O(n^3))."""
    np = state["np"]
    x = np.random.default_rng(state["seed"] + 1).standard_normal(
        c.shape[0], dtype=state["dtype"]
    )
    lhs = c @ x
    rhs = state["a"] @ (state["b"] @ x)
    error = float(np.linalg.norm(lhs - rhs) / max(float(np.linalg.norm(rhs)), 1e-30))
    tolerance = 1e-9 if state["dtype"] == "float64" else 1e-3
    return Check(
        ok=error <= tolerance,
        checksum=digest(c.tobytes()),
        exact=False,
        detail=f"freivalds relative error {error:.3e} (tolerance {tolerance:g})",
    )


# --------------------------------------------------------------------- primes


def _primes_prepare(params: Mapping[str, Any], seed: int, _device: str) -> Any:
    return {
        "limit": int_param(params, "limit"),
        "repeats": int_param(params, "repeats"),
        "seed": seed,
    }


def _sieve(limit: int) -> bytearray:
    sieve = bytearray(b"\x01") * (limit + 1)
    sieve[0] = sieve[1] = 0
    for i in range(2, math.isqrt(limit) + 1):
        if sieve[i]:
            start = i * i
            sieve[start::i] = bytes((limit - start) // i + 1)
    return sieve


def _primes_warmup(_state: Any) -> None:
    _sieve(10_000)


def _primes_compute(state: Any) -> Any:
    sieve = _sieve(state["limit"])
    for _ in range(state["repeats"] - 1):
        sieve = _sieve(state["limit"])
    return sieve


_PRIME_COUNTS = {10**6: 78_498, 10**7: 664_579, 10**8: 5_761_455, 10**9: 50_847_534}
"""pi(x) for the limits whose prime count is a well-known constant."""


def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    return all(n % d for d in range(3, math.isqrt(n) + 1, 2))


def _primes_check(state: Any, sieve: bytearray) -> Check:
    """64 seeded positions against trial division; pi(x) where it is known."""
    limit = state["limit"]
    rng = random.Random(state["seed"])  # noqa: S311 - seeded synthetic data
    wrong = [
        n
        for n in (rng.randint(2, limit) for _ in range(64))
        if bool(sieve[n]) != _is_prime(n)
    ]
    count = sieve.count(1)
    known = _PRIME_COUNTS.get(limit)
    ok = not wrong and (known is None or known == count)
    return Check(
        ok=ok,
        checksum=digest(str(count).encode(), hashlib.sha256(sieve).digest()),
        exact=True,
        detail=f"{count} primes <= {limit}; sampled mismatches {wrong[:5]}"
        + ("" if known is None else f"; pi({limit}) = {known}"),
    )


# ----------------------------------------------------------------------- sort


def _sort_prepare(params: Mapping[str, Any], seed: int, _device: str) -> Any:
    np = require_numpy()
    n = int_param(params, "n")
    values = np.random.default_rng(seed).integers(0, 2**62, size=n, dtype=np.int64)
    return {"np": np, "values": values, "repeats": int_param(params, "repeats")}


def _sort_warmup(state: Any) -> None:
    state["np"].sort(state["values"][:65_536])


def _sort_compute(state: Any) -> Any:
    np, values = state["np"], state["values"]
    out = np.sort(values)
    for _ in range(state["repeats"] - 1):
        out = np.sort(values)
    return out


def _sort_check(state: Any, out: Any) -> Check:
    np = state["np"]
    ordered = bool(np.all(out[1:] >= out[:-1]))
    same_sum = int(out.sum()) == int(state["values"].sum())
    return Check(
        ok=ordered and same_sum and out.shape == state["values"].shape,
        checksum=digest(out.tobytes()),
        exact=True,
        detail=f"non-decreasing {ordered}, sum preserved {same_sum}",
    )


# ----------------------------------------------------------------------- zlib


def _text_block(seed: int) -> bytes:
    """1 MiB of word-like text: compressible the way logs and CSVs are."""
    rng = random.Random(seed)  # noqa: S311 - seeded synthetic data
    alphabet = b"abcdefghijklmnopqrstuvwxyz0123456789"
    words = [bytes(rng.choices(alphabet, k=rng.randint(2, 10))) for _ in range(4096)]
    seps = [b" ", b" ", b" ", b", ", b"\n", b";"]
    parts: list[bytes] = []
    size = 0
    while size < MIB:
        part = rng.choice(words) + rng.choice(seps)
        parts.append(part)
        size += len(part)
    return b"".join(parts)[:MIB]


def _zlib_prepare(params: Mapping[str, Any], seed: int, _device: str) -> Any:
    size_mb = int_param(params, "size_mb")
    block = _text_block(seed)
    # Blocks are 1 MiB apart, far beyond zlib's 32 KiB window: no free matches.
    return {
        "data": block * size_mb,
        "level": int_param(params, "level"),
        "repeats": int_param(params, "repeats"),
    }


def _zlib_warmup(state: Any) -> None:
    zlib.compress(state["data"][:65_536], state["level"])


def _zlib_compute(state: Any) -> Any:
    out = zlib.compress(state["data"], state["level"])
    for _ in range(state["repeats"] - 1):
        out = zlib.compress(state["data"], state["level"])
    return out


def _zlib_check(state: Any, out: bytes) -> Check:
    restored = zlib.decompress(out) == state["data"]
    ratio = len(state["data"]) / max(len(out), 1)
    return Check(
        ok=restored,
        checksum=digest(out),
        exact=True,
        detail=f"round trip {restored}, ratio {ratio:.2f}, zlib {zlib.ZLIB_RUNTIME_VERSION}",
    )


# --------------------------------------------------------------------- sha256


def _sha256_prepare(params: Mapping[str, Any], seed: int, _device: str) -> Any:
    chunk = int_param(params, "chunk_kb") * 1024
    if chunk <= 0 or MIB % chunk:
        raise KernelParamsError("chunk_kb must divide 1024")
    return {
        "block": random.Random(seed).randbytes(MIB),  # noqa: S311 - seeded synthetic data
        "chunk": chunk,
        "size_mb": int_param(params, "size_mb"),
    }


def _sha256_stream(block: bytes, size_mb: int, chunk: int) -> str:
    view = memoryview(block)
    hasher = hashlib.sha256()
    for _ in range(size_mb):
        for offset in range(0, MIB, chunk):
            hasher.update(view[offset : offset + chunk])
    return hasher.hexdigest()


def _sha256_warmup(state: Any) -> None:
    _sha256_stream(state["block"], 1, state["chunk"])


def _sha256_compute(state: Any) -> Any:
    return _sha256_stream(state["block"], state["size_mb"], state["chunk"])


def _sha256_check(state: Any, out: str) -> Check:
    """The same stream hashed in whole 1 MiB updates must give the same digest."""
    other = _sha256_stream(state["block"], state["size_mb"], MIB)
    vector = hashlib.sha256(b"abc").hexdigest().startswith("ba7816bf")
    return Check(
        ok=other == out and vector,
        checksum="sha256:" + out,
        exact=True,
        detail=f"re-chunked digest equal {other == out}, test vector {vector}",
    )


# ------------------------------------------------------------------ integrate

_INTEGRATE_CHUNK = 1 << 20


def _integrate_prepare(params: Mapping[str, Any], seed: int, _device: str) -> Any:
    np = require_numpy()
    rng = random.Random(seed)  # noqa: S311 - seeded synthetic data
    intervals = int_param(params, "points_m") * 1_000_000
    return {
        "np": np,
        "a": 0.1 + 0.4 * rng.random(),
        "b": 1.0 + 9.0 * rng.random(),
        "t": 20.0,
        "n": intervals,
        "repeats": int_param(params, "repeats"),
    }


def _simpson(state: Any) -> float:
    """Composite Simpson of ``exp(-a x) cos(b x)`` on ``[0, t]``, in chunks."""
    np, a, b, t, n = state["np"], state["a"], state["b"], state["t"], state["n"]
    h = t / n
    total = 0.0
    for start in range(0, n + 1, _INTEGRATE_CHUNK):
        i = np.arange(start, min(start + _INTEGRATE_CHUNK, n + 1), dtype=np.float64)
        x = i * h
        weights = np.where(i % 2 == 1, 4.0, 2.0)
        if start == 0:
            weights[0] = 1.0
        if i[-1] == n:
            weights[-1] = 1.0
        total += float(np.dot(weights, np.exp(-a * x) * np.cos(b * x)))
    return float(total * h / 3.0)


def _integrate_warmup(state: Any) -> None:
    _simpson({**state, "n": 1000})


def _integrate_compute(state: Any) -> Any:
    value = _simpson(state)
    for _ in range(state["repeats"] - 1):
        value = _simpson(state)
    return value


def _integrate_check(state: Any, value: float) -> Check:
    a, b, t = state["a"], state["b"], state["t"]
    exact = (a + math.exp(-a * t) * (b * math.sin(b * t) - a * math.cos(b * t))) / (
        a * a + b * b
    )
    error = abs(value - exact) / abs(exact)
    return Check(
        ok=error <= 1e-8,
        checksum=floats_digest([round(value, 10)]),
        exact=False,
        detail=f"relative error vs the analytic integral {error:.3e}",
    )


KERNELS: dict[str, Kernel] = {
    "cpu.matmul": Kernel(
        _matmul_prepare, _matmul_compute, _matmul_check, _matmul_warmup
    ),
    "cpu.primes": Kernel(
        _primes_prepare, _primes_compute, _primes_check, _primes_warmup
    ),
    "cpu.sort": Kernel(_sort_prepare, _sort_compute, _sort_check, _sort_warmup),
    "cpu.zlib": Kernel(_zlib_prepare, _zlib_compute, _zlib_check, _zlib_warmup),
    "cpu.sha256": Kernel(
        _sha256_prepare, _sha256_compute, _sha256_check, _sha256_warmup
    ),
    "cpu.integrate": Kernel(
        _integrate_prepare, _integrate_compute, _integrate_check, _integrate_warmup
    ),
}
