"""Re-export: the S7 score arithmetic now lives in ``meshforge.agent.benchmarks.scores``.

G7 (brief §5g D33) moved it into the agent so the spike and the product share one
source of truth for every formula. ``lab`` may import ``meshforge``; ``src`` never
imports ``lab``.
"""

from __future__ import annotations

from meshforge.agent.benchmarks.scores import (
    MATMUL_FORMULA,
    MIN_JUDGE_S,
    SAXPY_FORMULA,
    SCORE_VERSION,
    STREAM_FORMULA,
    STREAM_STREAMS,
    THROTTLE_THRESHOLD,
    bytes_gb_s,
    matmul_flops,
    matmul_gflops,
    matmul_tflops,
    saxpy_mflops,
    slowdown,
    stability,
    stream_gb_s,
    throttling_suspected,
)

__all__ = [
    "MATMUL_FORMULA",
    "MIN_JUDGE_S",
    "SAXPY_FORMULA",
    "SCORE_VERSION",
    "STREAM_FORMULA",
    "STREAM_STREAMS",
    "THROTTLE_THRESHOLD",
    "bytes_gb_s",
    "matmul_flops",
    "matmul_gflops",
    "matmul_tflops",
    "saxpy_mflops",
    "slowdown",
    "stability",
    "stream_gb_s",
    "throttling_suspected",
]
