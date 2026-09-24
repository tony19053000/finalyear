"""Re-export: S7's problem-size rules now live in ``meshforge.agent.benchmarks.sizing``.

G7 (brief §5g D33) moved them into the agent (one source of truth); the spike's
``FULL``/``QUICK`` profiles moved with them. ``src`` never imports ``lab``.
"""

from __future__ import annotations

from meshforge.agent.benchmarks.sizing import (
    CACHE_MULTIPLE,
    DEFAULT_MEMORY_BYTES,
    FULL,
    GPU_N_MULTIPLE,
    MAX_CPU_N,
    MAX_GPU_N,
    MAX_GPU_N_QUICK,
    MIN_CPU_N,
    MIN_GPU_N,
    MIN_MEMORY_BYTES,
    QUICK,
    QUICK_MEMORY_BYTES,
    RAM_FRACTION,
    VRAM_FRACTION,
    Profile,
    calibrate,
    gpu_matmul_n,
    max_matmul_n,
    memory_buffer_bytes,
    profile,
    round_down_to,
)

__all__ = [
    "CACHE_MULTIPLE",
    "DEFAULT_MEMORY_BYTES",
    "FULL",
    "GPU_N_MULTIPLE",
    "MAX_CPU_N",
    "MAX_GPU_N",
    "MAX_GPU_N_QUICK",
    "MIN_CPU_N",
    "MIN_GPU_N",
    "MIN_MEMORY_BYTES",
    "QUICK",
    "QUICK_MEMORY_BYTES",
    "RAM_FRACTION",
    "VRAM_FRACTION",
    "Profile",
    "calibrate",
    "gpu_matmul_n",
    "max_matmul_n",
    "memory_buffer_bytes",
    "profile",
    "round_down_to",
]
