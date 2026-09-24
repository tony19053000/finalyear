"""The S7 benchmark child process: one measurement kind, driven over stdin/stdout.

Why a child process at all: a broken CUDA stack can hang or hard-crash the
interpreter (the inventory learned this in 0.2.2), BLAS thread counts are read
from the environment **at import time**, and a pure-Python benchmark can only
use all cores through separate processes. So the parent
(:mod:`spikes._s7.worker`) spawns one child per benchmark case - with the thread
environment it wants - and drives it:

.. code-block:: text

    parent -> {"op": "setup", "spec": {...}}   child -> {"ok": true, "info": {...}}
    parent -> {"op": "rep"}                    child -> {"ok": true, "sample": {...}}
    parent -> {"op": "bye"}                    child exits 0

One JSON document per line, in both directions. A failure is answered with
``{"ok": false, "failure_type": ..., "message": ...}`` and the child stays
alive; the parent decides whether to retry or give up. ``--probe`` prints the
machine's backend capabilities (torch / numpy / CUDA) and exits, so the parent
can choose the case list without importing torch itself.

**Frozen spike copy (G7, brief §5g D33).** The maintained kernels are
``meshforge.agent.benchmarks.kernels`` / ``.child``. This file keeps its own copy
because its tests patch ``child._module`` and ``child.KINDS`` on *this* module
(which a re-export cannot honour) and it still needs the pure-Python fallback that
the agent deliberately does not ship. ``scores``/``sizing`` are shared re-exports.

Run directly (debugging)::

    echo '{"op":"setup","spec":{"kind":"cpu_python","target_s":0.05}}
    {"op":"rep"}' | python lab/spikes/_s7/child.py
"""

from __future__ import annotations

import argparse
import array
import functools
import importlib
import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Final

_LAB_DIR: Final = Path(__file__).resolve().parents[2]
if str(_LAB_DIR) not in sys.path:
    sys.path.insert(0, str(_LAB_DIR))

from spikes._s7 import scores, sizing  # noqa: E402

#: Cap on the repetition count inside one timed batch (GPU / stream kernels).
MAX_ITERS: Final = 1000
#: Start sizes for calibration.
START_MATMUL_N: Final = 128
START_SAXPY_ELEMENTS: Final = 50_000
#: ``array`` type codes by dtype name (the stdlib fallback uses doubles only).
_TYPECODE: Final = {"float64": "d", "float32": "f"}
_ITEMSIZE: Final = {"float64": 8, "float32": 4, "float16": 2, "bfloat16": 2}


class BenchError(Exception):
    """A measurement could not be made. ``failure_type`` is a short category."""

    def __init__(self, failure_type: str, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type


@functools.cache
def _module(name: str) -> Any | None:
    """Import ``name`` once; None if it is missing or its import raised.

    Cached on purpose, failures included: a native package whose import raised
    half-way (torch does, e.g. when a warning is promoted to an error) leaves its
    C extension loaded, and re-running its ``__init__`` against that extension
    corrupts interpreter state - the process then segfaults at shutdown (exit
    139) long after every result was reported. One attempt per process is the
    only safe number.
    """
    try:
        return importlib.import_module(name)
    except Exception:  # noqa: BLE001 - a broken install must not crash the child
        return None


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _timed(fn: Callable[[], object]) -> float:
    start = time.perf_counter()
    fn()
    return time.perf_counter() - start


def _iters_for(single_s: float, target_s: float) -> int:
    """Repetitions per timed batch so that one batch lasts about ``target_s``."""
    return max(1, min(MAX_ITERS, int(target_s / max(single_s, 1e-6))))


# --------------------------------------------------------------------- CPU matmul
class CpuMatmul:
    """``C = A @ B`` in float32/float64 on the CPU, torch or numpy.

    ``N`` is calibrated so one multiplication takes at least ``target_s``;
    score = ``2 * N^3 / elapsed`` (:mod:`spikes._s7.scores`).
    """

    kind = "cpu_matmul"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.dtype = str(spec.get("dtype", "float32"))
        self.threads = int(spec.get("threads", 1))
        self.target_s = float(spec.get("target_s", 0.3))
        self.max_n = int(spec.get("max_n", sizing.MAX_CPU_N))
        self.n = 0
        self.backend = ""
        self.version = ""
        self._run: Callable[[], object] = lambda: None

    def setup(self) -> dict[str, Any]:
        torch = _module("torch")
        numpy = _module("numpy")
        if torch is not None:
            self.backend, self.version = "torch", str(torch.__version__)
            torch.set_num_threads(self.threads)
            make, mul = self._torch_ops(torch)
        elif numpy is not None:
            self.backend, self.version = "numpy", str(numpy.__version__)
            make, mul = self._numpy_ops(numpy)
        else:
            raise BenchError(
                "no_blas_backend",
                "neither torch nor numpy is installed; the pure-Python fallback "
                "(kind cpu_python) measures interpreter throughput instead",
            )
        state: dict[str, Any] = {}

        def measure(n: int) -> float:
            a, b, c = make(n)
            state["run"] = lambda: mul(a, b, c)
            return _timed(state["run"])

        self.n, trace = sizing.calibrate(
            measure,
            start=min(START_MATMUL_N, self.max_n),
            target_s=self.target_s,
            max_size=self.max_n,
            exponent=3.0,
        )
        self._run = state["run"]
        return {
            "backend": self.backend,
            "backend_version": self.version,
            "dtype": self.dtype,
            "n": self.n,
            "iters": 1,
            "threads_requested": self.threads,
            "threads_effective": self._effective_threads(torch),
            "flops": scores.matmul_flops(self.n),
            "formula": scores.MATMUL_FORMULA,
            "calibration": trace,
            "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
        }

    def _effective_threads(self, torch: Any | None) -> int | None:
        if self.backend == "torch" and torch is not None:
            return int(torch.get_num_threads())
        value = os.environ.get("OMP_NUM_THREADS")
        return int(value) if value and value.isdigit() else None

    def _torch_ops(self, torch: Any) -> tuple[Callable[[int], Any], Callable[..., Any]]:
        dtype = getattr(torch, self.dtype, None)
        if dtype is None:
            raise BenchError("unsupported_dtype", f"torch has no dtype {self.dtype}")

        def make(n: int) -> Any:
            a = torch.rand((n, n), dtype=dtype)
            return a, torch.rand((n, n), dtype=dtype), torch.empty((n, n), dtype=dtype)

        def mul(a: Any, b: Any, c: Any) -> Any:
            return torch.mm(a, b, out=c)

        return make, mul

    def _numpy_ops(self, numpy: Any) -> tuple[Callable[[int], Any], Callable[..., Any]]:
        dtype = getattr(numpy, self.dtype, None)
        if dtype is None:
            raise BenchError("unsupported_dtype", f"numpy has no dtype {self.dtype}")
        rng = numpy.random.default_rng(7)

        def make(n: int) -> Any:
            a = rng.random((n, n), dtype=dtype)
            return a, rng.random((n, n), dtype=dtype), numpy.empty((n, n), dtype=dtype)

        def mul(a: Any, b: Any, c: Any) -> Any:
            return numpy.matmul(a, b, out=c)

        return make, mul

    def rep(self) -> dict[str, Any]:
        elapsed = _timed(self._run)
        return {
            "elapsed_s": elapsed,
            "gflops": scores.matmul_gflops(self.n, elapsed),
        }


# ------------------------------------------------------------------- CPU fallback
class CpuPython:
    """Pure-stdlib fallback: SAXPY (``y = a*x + y``) over an ``array('d')``.

    This is **not** peak FLOPS: it measures interpreter-bound throughput (about
    three orders of magnitude below a BLAS matmul). It exists so that a laptop
    without torch or numpy still produces a comparable number, and it is
    reported under its own metric name (``python_mflops``) so it can never be
    mistaken for, or averaged with, a real GFLOPS score.
    """

    kind = "cpu_python"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.target_s = float(spec.get("target_s", 0.3))
        self.max_elements = int(spec.get("max_elements", 20_000_000))
        self.elements = 0
        self._x = array.array("d")
        self._y = array.array("d")

    def _saxpy(self) -> None:
        x, y = self._x, self._y
        a = 1.0000001
        for i in range(len(x)):
            y[i] = a * x[i] + y[i]

    def setup(self) -> dict[str, Any]:
        def measure(n: int) -> float:
            self._x = array.array("d", [1.0]) * n
            self._y = array.array("d", [2.0]) * n
            self.elements = n
            return _timed(self._saxpy)

        self.elements, trace = sizing.calibrate(
            measure,
            start=min(START_SAXPY_ELEMENTS, self.max_elements),
            target_s=self.target_s,
            max_size=self.max_elements,
            exponent=1.0,
            growth=16,
        )
        return {
            "backend": "python_fallback",
            "backend_version": sys.version.split()[0],
            "dtype": "float64",
            "elements": self.elements,
            "kernel": "y[i] = a * x[i] + y[i]",
            "formula": scores.SAXPY_FORMULA,
            "caveat": (
                "interpreter-bound throughput, not peak FLOPS; install torch "
                "for a comparable GFLOPS number"
            ),
            "calibration": trace,
        }

    def rep(self) -> dict[str, Any]:
        elapsed = _timed(self._saxpy)
        return {
            "elapsed_s": elapsed,
            "python_mflops": scores.saxpy_mflops(self.elements, elapsed),
        }


# ------------------------------------------------------------- memory bandwidth
class MemoryBandwidth:
    """STREAM-like copy / scale / add / triad over buffers larger than the cache.

    torch or numpy run all four kernels in float64 with pre-allocated outputs
    (no allocation inside the timed region). Without them only ``copy`` is
    measurable at memory speed (a ``bytearray`` slice assignment is a memcpy);
    the other kernels would time the interpreter, so they are reported as
    unsupported instead of as a wrong number.

    The score is meant to be the **machine's** bandwidth ceiling, which one core
    usually cannot saturate. With torch the kernels run on ``threads`` cores
    (``torch.set_num_threads``, the parent also sets ``OMP_NUM_THREADS``); numpy
    element-wise kernels and the stdlib memcpy are single-threaded whatever the
    environment says, so those numbers are a *lower bound* and say so in
    ``notes``. Each repetition runs every kernel ``iters`` times (calibrated so
    a batch lasts about ``target_s``), as STREAM recommends.
    """

    kind = "mem_bw"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.bytes = int(spec.get("bytes", sizing.DEFAULT_MEMORY_BYTES))
        self.threads = int(spec.get("threads", 1))
        self.target_s = float(spec.get("target_s", 0.2))
        self.itemsize = 8
        self.elements = max(1, self.bytes // self.itemsize)
        self.iters = 1
        self.backend = ""
        self.threads_effective: int | None = None
        self.ops: dict[str, Callable[[], object]] = {}
        self.notes: list[str] = []

    def setup(self) -> dict[str, Any]:
        torch = _module("torch")
        numpy = _module("numpy")
        if torch is not None:
            self.backend = f"torch {torch.__version__}"
            torch.set_num_threads(self.threads)
            self.threads_effective = int(torch.get_num_threads())
            self._torch_ops(torch)
        elif numpy is not None:
            self.backend = f"numpy {numpy.__version__}"
            self.threads_effective = 1
            self.notes.append(
                "numpy backend: element-wise kernels are single-threaded, so "
                "this is a lower bound of the machine's bandwidth"
            )
            self._numpy_ops(numpy)
        else:
            self.backend = "python_fallback"
            self.threads_effective = 1
            self._stdlib_ops()
        for run in self.ops.values():  # warm the pages (first touch is page faults)
            run()
        slowest = max(_timed(run) for run in self.ops.values())
        self.iters = _iters_for(slowest, self.target_s)
        return {
            "backend": self.backend,
            "bytes_per_buffer": self.elements * self.itemsize,
            "elements": self.elements,
            "itemsize": self.itemsize,
            "iters": self.iters,
            "single_pass_s": slowest,
            "threads_requested": self.threads,
            "threads_effective": self.threads_effective,
            "omp_num_threads": os.environ.get("OMP_NUM_THREADS"),
            "ops": sorted(self.ops),
            "unsupported_ops": sorted(set(scores.STREAM_STREAMS) - set(self.ops)),
            "formula": scores.STREAM_FORMULA,
            "streams_per_op": {k: v[0] for k, v in scores.STREAM_STREAMS.items()},
            "notes": self.notes,
        }

    def _torch_ops(self, torch: Any) -> None:
        n, dtype = self.elements, torch.float64
        a, b, c = (torch.full((n,), float(i + 1), dtype=dtype) for i in range(3))
        q = 3.0
        # Every kernel is fused and writes into a pre-allocated buffer, so the
        # traffic really is the 2/2/3/3 streams the score counts.
        self.ops = {
            "copy": lambda: c.copy_(a),
            "scale": lambda: torch.mul(c, q, out=b),
            "add": lambda: torch.add(a, b, out=c),
            "triad": lambda: torch.add(b, c, alpha=q, out=a),
        }

    def _numpy_ops(self, numpy: Any) -> None:
        n = self.elements
        a, b, c, d = (
            numpy.full(n, float(i + 1), dtype=numpy.float64) for i in range(4)
        )
        q = 3.0

        def triad() -> None:
            # numpy has no fused a = b + q*c, so this moves 5 streams while the
            # score counts 3: the numpy triad GB/s is a lower bound (see notes).
            numpy.multiply(c, q, out=d)
            numpy.add(b, d, out=a)

        self.ops = {
            "copy": lambda: numpy.copyto(c, a),
            "scale": lambda: numpy.multiply(c, q, out=b),
            "add": lambda: numpy.add(a, b, out=c),
            "triad": triad,
        }
        self.notes.append(
            "numpy triad is not fused (two passes): its GB/s is a lower bound"
        )

    def _stdlib_ops(self) -> None:
        self.itemsize = 1
        self.elements = self.bytes
        src = bytearray(self.bytes)
        dst = bytearray(self.bytes)
        view = memoryview(dst)

        def copy() -> None:
            view[:] = src

        self.ops = {"copy": copy}
        self.notes.append(
            "stdlib fallback: only `copy` (a bytearray memcpy) runs at memory "
            "speed; scale/add/triad would measure the interpreter, so they are "
            "not reported"
        )
        self.notes.append(
            "python backend: single-threaded memcpy, a lower bound of the "
            "machine's bandwidth"
        )

    def rep(self) -> dict[str, Any]:
        sample: dict[str, Any] = {}
        total = 0.0
        for name in sorted(self.ops):
            run = self.ops[name]

            def batch(run: Callable[[], object] = run) -> None:
                for _ in range(self.iters):
                    run()

            elapsed = _timed(batch)
            total += elapsed
            sample[f"{name}_gb_s"] = scores.stream_gb_s(
                name, self.elements * self.iters, self.itemsize, elapsed
            )
            sample[f"{name}_s"] = elapsed
        sample["elapsed_s"] = total
        sample["iters"] = self.iters
        return sample


# --------------------------------------------------------------------------- GPU
def _torch_cuda() -> Any:
    torch = _module("torch")
    if torch is None:
        raise BenchError("torch_not_installed", "torch is not installed")
    try:
        available = bool(torch.cuda.is_available())
    except Exception as exc:
        raise BenchError("cuda_broken", _describe(exc)) from exc
    if not available:
        raise BenchError("cuda_unavailable", "torch.cuda.is_available() is False")
    return torch


def _gpu_dtype(torch: Any, name: str) -> Any:
    dtype = getattr(torch, name, None)
    if dtype is None:
        raise BenchError("unsupported_dtype", f"torch has no dtype {name}")
    if name == "bfloat16" and not bool(torch.cuda.is_bf16_supported()):
        raise BenchError("unsupported_dtype", "this GPU does not support bfloat16")
    return dtype


def _pin_precision(torch: Any) -> dict[str, Any]:
    """Turn TF32 off so ``float32`` really means fp32 (comparable across GPUs).

    Ampere+ cards otherwise run fp32 matmuls on tensor cores at reduced
    precision when ``allow_tf32`` is on, which would make the fp32 score of
    one laptop incomparable with an older card's. The state is recorded.
    """
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    return {
        "allow_tf32": bool(torch.backends.cuda.matmul.allow_tf32),
        "cudnn_allow_tf32": bool(torch.backends.cudnn.allow_tf32),
        "float32_matmul_precision": str(torch.get_float32_matmul_precision()),
    }


def _max_memory_allocated(torch: Any, index: int) -> int | None:
    try:
        return int(torch.cuda.max_memory_allocated(index))
    except Exception:  # noqa: BLE001 - a missing statistic is not a failure
        return None


def _device_info(torch: Any, index: int) -> dict[str, Any]:
    free, total = torch.cuda.mem_get_info(index)
    props = torch.cuda.get_device_properties(index)
    return {
        # Same key every kernel's setup info carries, so the report's backend
        # column and the tests can read it without knowing the kernel kind.
        "backend": f"torch-cuda {torch.__version__}",
        "device_index": index,
        "device_name": torch.cuda.get_device_name(index),
        "capability": ".".join(str(v) for v in torch.cuda.get_device_capability(index)),
        "vram_free_bytes": int(free),
        "vram_total_bytes": int(total),
        "multi_processor_count": getattr(props, "multi_processor_count", None),
        "torch_version": str(torch.__version__),
        "cuda_version": str(torch.version.cuda),
    }


class GpuMatmul:
    """``C = A @ B`` on the GPU in fp32 / fp16 / bf16.

    ``N`` comes from the *free* VRAM (three matrices in at most half of it), the
    batch size ``iters`` from a calibration run, so one timed batch lasts about
    ``target_s``. Timing is wall clock around ``torch.cuda.synchronize()`` with
    ``torch.cuda.Event`` timings recorded as an independent cross-check.
    """

    kind = "gpu_matmul"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.dtype_name = str(spec.get("dtype", "float32"))
        self.index = int(spec.get("device_index", 0))
        self.target_s = float(spec.get("target_s", 0.2))
        self.fraction = float(spec.get("vram_fraction", sizing.VRAM_FRACTION))
        self.max_n = int(spec.get("max_n", sizing.MAX_GPU_N))
        self.n = 0
        self.iters = 1
        self._torch: Any = None
        self._mats: tuple[Any, Any, Any] | None = None
        self._events: tuple[Any, Any] | None = None

    def setup(self) -> dict[str, Any]:
        torch = _torch_cuda()
        self._torch = torch
        dtype = _gpu_dtype(torch, self.dtype_name)
        info = _device_info(torch, self.index)
        itemsize = _ITEMSIZE.get(self.dtype_name, 4)
        precision = _pin_precision(torch)
        self.n = sizing.gpu_matmul_n(
            info["vram_free_bytes"], itemsize, fraction=self.fraction, max_n=self.max_n
        )
        if self.n == 0:
            raise BenchError(
                "insufficient_vram",
                f"only {info['vram_free_bytes'] / 2**20:.0f} MiB VRAM free: not "
                f"enough for three {sizing.MIN_GPU_N}^2 {self.dtype_name} matrices",
            )
        try:
            device = torch.device("cuda", self.index)
            # Generated directly in the target dtype: no fp32 copy that would
            # double the peak allocation and could push a small card into OOM.
            a = torch.rand((self.n, self.n), dtype=dtype, device=device)
            b = torch.rand((self.n, self.n), dtype=dtype, device=device)
            c = torch.empty((self.n, self.n), dtype=dtype, device=device)
            self._mats = (a, b, c)
            self._events = (
                torch.cuda.Event(enable_timing=True),
                torch.cuda.Event(enable_timing=True),
            )
            torch.mm(a, b, out=c)
            torch.cuda.synchronize(device)
            single = _timed(lambda: (torch.mm(a, b, out=c), torch.cuda.synchronize()))
        except Exception as exc:
            raise BenchError("gpu_setup_failed", _describe(exc)) from exc
        self.iters = _iters_for(single, self.target_s)
        return {
            **info,
            **precision,
            "dtype": self.dtype_name,
            "n": self.n,
            "max_n": self.max_n,
            "iters": self.iters,
            "single_matmul_s": single,
            "flops": scores.matmul_flops(self.n, self.iters),
            "formula": scores.MATMUL_FORMULA,
            "vram_fraction": self.fraction,
            "matrix_bytes": 3 * self.n**2 * itemsize,
            "max_memory_allocated_bytes": _max_memory_allocated(torch, self.index),
        }

    def rep(self) -> dict[str, Any]:
        torch, mats, events = self._torch, self._mats, self._events
        if mats is None or events is None:
            raise BenchError("not_set_up", "setup() did not complete")
        a, b, c = mats
        start, end = events
        try:
            torch.cuda.synchronize()
            wall_start = time.perf_counter()
            start.record()
            for _ in range(self.iters):
                torch.mm(a, b, out=c)
            end.record()
            torch.cuda.synchronize()
            wall = time.perf_counter() - wall_start
            event_s = start.elapsed_time(end) / 1000.0
        except Exception as exc:
            raise BenchError("gpu_rep_failed", _describe(exc)) from exc
        return {
            "elapsed_s": wall,
            "tflops": scores.matmul_tflops(self.n, wall, self.iters),
            "event_s": event_s,
            "event_tflops": scores.matmul_tflops(self.n, event_s, self.iters),
        }


class GpuCopy:
    """Device-to-device copy bandwidth (``b.copy_(a)``), read + write counted."""

    kind = "gpu_copy"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.bytes = int(spec.get("bytes", 256 * 2**20))
        self.index = int(spec.get("device_index", 0))
        self.target_s = float(spec.get("target_s", 0.2))
        self.iters = 1
        self._torch: Any = None
        self._buffers: tuple[Any, Any] | None = None

    def setup(self) -> dict[str, Any]:
        torch = _torch_cuda()
        self._torch = torch
        info = _device_info(torch, self.index)
        budget = int(info["vram_free_bytes"] * sizing.VRAM_FRACTION / 2)
        self.bytes = max(2**20, min(self.bytes, budget))
        elements = self.bytes // 4
        if elements < 1:
            raise BenchError("insufficient_vram", "not enough free VRAM for a copy")
        try:
            device = torch.device("cuda", self.index)
            a = torch.zeros(elements, dtype=torch.float32, device=device)
            b = torch.zeros(elements, dtype=torch.float32, device=device)
            self._buffers = (a, b)
            b.copy_(a)
            torch.cuda.synchronize(device)
            single = _timed(lambda: (b.copy_(a), torch.cuda.synchronize()))
        except Exception as exc:
            raise BenchError("gpu_setup_failed", _describe(exc)) from exc
        self.iters = _iters_for(single, self.target_s)
        self.bytes = elements * 4
        return {
            **info,
            "bytes_per_buffer": self.bytes,
            "iters": self.iters,
            "single_copy_s": single,
            "formula": "2 * bytes * iters / elapsed_s (read + write)",
            "max_memory_allocated_bytes": _max_memory_allocated(torch, self.index),
        }

    def rep(self) -> dict[str, Any]:
        torch, buffers = self._torch, self._buffers
        if buffers is None:
            raise BenchError("not_set_up", "setup() did not complete")
        a, b = buffers
        try:
            torch.cuda.synchronize()
            start = time.perf_counter()
            for _ in range(self.iters):
                b.copy_(a)
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - start
        except Exception as exc:
            raise BenchError("gpu_rep_failed", _describe(exc)) from exc
        return {
            "elapsed_s": elapsed,
            "device_copy_gb_s": scores.bytes_gb_s(2 * self.bytes * self.iters, elapsed),
        }


KINDS: Final[dict[str, type[Any]]] = {
    cls.kind: cls for cls in (CpuMatmul, CpuPython, MemoryBandwidth, GpuMatmul, GpuCopy)
}


# ----------------------------------------------------------------------- probing
def capabilities() -> dict[str, Any]:
    """What this machine can measure (used by the parent to pick the cases)."""
    out: dict[str, Any] = {
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "numpy": None,
        "torch": None,
        "cuda": {"available": False},
    }
    numpy = _module("numpy")
    if numpy is not None:
        out["numpy"] = str(numpy.__version__)
    torch = _module("torch")
    if torch is None:
        out["cuda"] = {
            "available": False,
            "failure_type": "torch_not_installed",
            "error": "torch is not installed (see ADR-0011)",
        }
        return out
    out["torch"] = str(torch.__version__)
    out["torch_cuda_build"] = str(torch.version.cuda)
    try:
        if not torch.cuda.is_available():
            out["cuda"] = {
                "available": False,
                "failure_type": "cuda_unavailable",
                "error": "torch.cuda.is_available() is False",
            }
            return out
        devices = [_device_info(torch, i) for i in range(torch.cuda.device_count())]
        out["cuda"] = {
            "available": True,
            "devices": devices,
            "bf16_supported": bool(torch.cuda.is_bf16_supported()),
        }
    except Exception as exc:  # noqa: BLE001 - a broken CUDA stack is data
        out["cuda"] = {
            "available": False,
            "failure_type": "cuda_broken",
            "error": _describe(exc),
        }
    return out


# ---------------------------------------------------------------------- protocol
def _reply(payload: dict[str, Any], out: Any) -> None:
    out.write(json.dumps(payload, allow_nan=False) + "\n")
    out.flush()


def _dispatch(request: dict[str, Any], bench: Any) -> tuple[dict[str, Any], Any]:
    """Handle one request; returns ``(reply, benchmark)``. Raises :class:`BenchError`."""
    op = str(request.get("op"))
    if op == "setup":
        spec = dict(request.get("spec") or {})
        kind = str(spec.get("kind"))
        if kind not in KINDS:
            raise BenchError("unknown_kind", f"unknown benchmark kind {kind!r}")
        bench = KINDS[kind](spec)
        return {"ok": True, "info": bench.setup()}, bench
    if op == "rep":
        if bench is None:
            raise BenchError("not_set_up", "rep before setup")
        return {"ok": True, "sample": bench.rep()}, bench
    if op == "probe":
        return {"ok": True, "info": capabilities()}, bench
    raise BenchError("unknown_op", f"unknown op {op!r}")


def serve(stdin: Any, stdout: Any) -> int:
    """Read request lines, answer one line each, until EOF or ``bye``."""
    bench: Any = None
    for line in stdin:
        text = line.strip()
        if not text:
            continue
        try:
            request = json.loads(text)
        except ValueError as exc:
            _reply(
                {"ok": False, "failure_type": "bad_request", "message": _describe(exc)},
                stdout,
            )
            continue
        if not isinstance(request, dict):
            _reply(
                {
                    "ok": False,
                    "failure_type": "bad_request",
                    "message": "request is not a JSON object",
                },
                stdout,
            )
            continue
        if request.get("op") == "bye":
            return 0
        try:
            reply, bench = _dispatch(request, bench)
        except BenchError as exc:
            reply = {
                "ok": False,
                "failure_type": exc.failure_type,
                "message": str(exc),
            }
        except Exception as exc:  # noqa: BLE001 - any failure is data, not a crash
            reply = {
                "ok": False,
                "failure_type": "benchmark_error",
                "message": _describe(exc),
            }
        _reply(reply, stdout)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lab/spikes/_s7/child.py",
        description="S7 benchmark child process (driven by lab/spikes/s7_hardware.py).",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="print backend/CUDA capabilities as JSON and exit",
    )
    args = parser.parse_args(argv)
    # The protocol owns the real stdout. Everything else that prints (a torch
    # warning, a numpy deprecation, a stray print() in a BLAS wrapper) goes to
    # stderr, where the parent keeps it as diagnostics instead of parsing it.
    proto = sys.stdout
    sys.stdout = sys.stderr
    try:
        if args.probe:
            _reply({"ok": True, "info": capabilities()}, proto)
            return 0
        return serve(sys.stdin, proto)
    finally:
        sys.stdout = proto


if __name__ == "__main__":
    raise SystemExit(main())
