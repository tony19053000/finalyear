# ruff: noqa: T201 - an example program: its output is the point
"""MeshForge example: a deterministic CPU workload (V0.4, roadmap 0.4.14).

Runs the same computation on any laptop and writes byte-identical results, so
`compute run --node X` and `compute run --local` can be compared by digest
(V0.4 success criterion 1). Everything is exact integer arithmetic on numbers
from a seeded `random.Random` (Mersenne Twister: the same sequence on every OS
and Python >= 3.2) - no floating-point reductions, whose results can depend on
the CPU's vector width.

numpy is optional: when it is importable the prime sieve and the histogram use
it; the pure-Python path gives the same bytes (the example's test checks both).

Usage: python main.py [--size N] [--seed S] [--out DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
from pathlib import Path


def primes_upto(n: int, use_numpy: bool) -> list[int]:
    if n < 2:
        return []
    if use_numpy:
        import numpy as np

        sieve = np.ones(n + 1, dtype=bool)
        sieve[:2] = False
        for p in range(2, int(n**0.5) + 1):
            if sieve[p]:
                sieve[p * p :: p] = False
        return [int(i) for i in np.flatnonzero(sieve)]
    flags = bytearray([1]) * (n + 1)
    flags[0:2] = b"\x00\x00"
    for p in range(2, int(n**0.5) + 1):
        if flags[p]:
            flags[p * p :: p] = bytes(len(range(p * p, n + 1, p)))
    return [i for i, f in enumerate(flags) if f]


def histogram(values: list[int], buckets: int, top: int, use_numpy: bool) -> list[int]:
    if use_numpy:
        import numpy as np

        arr = np.asarray(values, dtype=np.int64)
        return [int(c) for c in np.bincount(arr * buckets // top, minlength=buckets)]
    counts = [0] * buckets
    for v in values:
        counts[v * buckets // top] += 1
    return counts


def collatz_steps(n: int) -> int:
    steps = 0
    while n != 1:
        n = n // 2 if n % 2 == 0 else 3 * n + 1
        steps += 1
    return steps


def compute(size: int, seed: int, use_numpy: bool) -> dict[str, object]:
    rng = random.Random(seed)  # noqa: S311 - seeded on purpose: reproducible, not secret
    radius = 1 << 20
    inside = 0
    for _ in range(size):
        x, y = rng.randrange(radius), rng.randrange(radius)
        inside += x * x + y * y < radius * radius
    values = [rng.randrange(1_000_000) for _ in range(size)]
    primes = primes_upto(size * 10, use_numpy)
    longest = max(range(1, size + 1), key=lambda n: (collatz_steps(n), -n))
    return {
        "seed": seed,
        "size": size,
        # pi ~= 4 * inside / size, kept as an exact fraction
        "pi_fraction": [4 * inside, size],
        "prime_count": len(primes),
        "prime_sum": sum(primes),
        "histogram": histogram(values, 16, 1_000_000, use_numpy),
        "values_sha256": hashlib.sha256(
            ",".join(map(str, sorted(values))).encode()
        ).hexdigest(),
        "longest_collatz": [longest, collatz_steps(longest)],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=200_000)
    parser.add_argument("--seed", type=int, default=1234)
    parser.add_argument("--out", type=Path, default=Path("results"))
    parser.add_argument("--no-numpy", action="store_true", help="Force pure Python.")
    args = parser.parse_args(argv)
    try:
        import numpy  # noqa: F401

        use_numpy = not args.no_numpy
    except ImportError:
        use_numpy = False
    started = time.monotonic()
    result = compute(args.size, args.seed, use_numpy)
    args.out.mkdir(parents=True, exist_ok=True)
    # The result file holds only the computation - no timings, no hostnames -
    # so it is byte-identical wherever it runs ("\n" line ends on Windows too).
    (args.out / "result.json").write_text(
        json.dumps(result, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"done: {result['prime_count']} primes, pi ~ "
        f"{result['pi_fraction'][0] / result['pi_fraction'][1]:.5f}, "
        f"{time.monotonic() - started:.2f} s ({'numpy' if use_numpy else 'pure Python'})",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
