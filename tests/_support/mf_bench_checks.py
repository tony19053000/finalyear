"""Shared helper of the V0.9 G2 tester r2 self-check tests (``tests/unit/bench``).

Split out of ``test_v09_g2_tester_r2_checks.py`` (G3 item 0.2, D225.4): run one
kernel in-process (prepare, warm-up, compute, optional tamper, check).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from meshforge.bench.workloads.kernels._support import Check


def run_kernel_check(
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
