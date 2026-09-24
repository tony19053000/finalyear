"""The measurement kernels, lifted from spike S7 (``lab/spikes/_s7/child.py``, D33).

**Only the benchmark child process imports this module** (:mod:`.child`). The
agent process never does: torch and numpy are imported *lazily*, inside a kernel's
``setup()``, through :func:`_module` - which is why this file may be imported on a
base install and by unit tests that block both libraries in ``sys.modules``.

Four kernels, one per metric family (brief §5g D31):

=================  ==========================================  ====================
kind               what                                        score
=================  ==========================================  ====================
``cpu_matmul``     ``C = A @ B`` in fp32/fp64, torch else numpy  GFLOP/s
``mem_bw``         STREAM kernels over buffers >> cache          GB/s
``gpu_matmul``     ``C = A @ B`` on one CUDA device, TF32 off    TFLOP/s
``gpu_copy``       device-to-device ``b.copy_(a)``               GB/s (read + write)
=================  ==========================================  ====================

Differences from the spike's copy (which keeps its own, frozen with its 69 tests):
no pure-Python fallback (D30: without a BLAS the CPU metrics are *unavailable*, not
an interpreter-speed number), ``backend`` / ``backend_version`` are separate fields
on every kernel, and ``mem_bw`` can be restricted to the ops the agent scores
(``triad``) so the run stays inside its budget.
"""

from __future__ import annotations

import functools
import importlib
import os
import sys
import time
from collections.abc import Callable
from typing import Any, Final

from meshforge.agent.benchmarks import scores, sizing

__all__ = [
    "KINDS",
    "MAX_ITERS",
    "BenchError",
    "CpuMatmul",
    "GpuCopy",
    "GpuMatmul",
    "MemoryBandwidth",
    "capabilities",
]

#: Cap on the repetition count inside one timed batch (GPU / stream kernels).
MAX_ITERS: Final = 1000
#: Start size of the CPU matmul calibration.
START_MATMUL_N: Final = 128
_ITEMSIZE: Final = {"float64": 8, "float32": 4, "float16": 2, "bfloat16": 2}


class BenchError(Exception):
    """A measurement could not be made. ``failure_type`` is a short snake_case code."""

    def __init__(self, failure_type: str, message: str) -> None:
        super().__init__(message)
        self.failure_type = failure_type


@functools.cache
def _module(name: str) -> Any | None:
    """Import ``name`` once; None if it is missing or its import raised.

    Cached on purpose, failures included (the exit-139 lesson, SESSION_HANDOFF §2):
    a native package whose import raised half-way leaves its C extension loaded, and
    re-running its ``__init__`` against it corrupts the interpreter - the process
    then segfaults at shutdown. One attempt per process is the only safe number.
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


def _blas() -> tuple[str, Any]:
    """``("torch", torch)`` if importable, else ``("numpy", numpy)``; else a typed error."""
    torch = _module("torch")
    if torch is not None:
        return "torch", torch
    numpy = _module("numpy")
    if numpy is not None:
        return "numpy", numpy
    raise BenchError(
        "no_blas_backend",
        "neither torch nor numpy is importable: no real CPU/memory score is possible",
    )


# --------------------------------------------------------------------- CPU matmul
class CpuMatmul:
    """``C = A @ B`` in float32/float64 on the CPU, torch or numpy.

    ``N`` is calibrated so one multiplication takes at least ``target_s`` and is
    capped by ``max_n`` (the runner derives it from available RAM);
    score = ``2 * N^3 / elapsed`` (:mod:`.scores`).
    """

    kind = "cpu_matmul"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.dtype = str(spec.get("dtype", "float32"))
        self.threads = int(spec.get("threads", 1))
        self.target_s = float(spec.get("target_s", 0.2))
        self.max_n = max(1, int(spec.get("max_n", sizing.MAX_CPU_N)))
        self.n = 0
        self.backend = ""
        self.version = ""
        self._run: Callable[[], object] = lambda: None

    def setup(self) -> dict[str, Any]:
        self.backend, lib = _blas()
        self.version = str(lib.__version__)
        if self.backend == "torch":
            lib.set_num_threads(self.threads)
            make, mul = self._torch_ops(lib)
        else:
            make, mul = self._numpy_ops(lib)
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
            "threads_requested": self.threads,
            "threads_effective": self._effective_threads(lib),
            "formula": scores.MATMUL_FORMULA,
            "calibration": trace,
            "lower_bound": False,
        }

    def _effective_threads(self, lib: Any) -> int | None:
        if self.backend == "torch":
            return int(lib.get_num_threads())
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
        return {"elapsed_s": elapsed, "value": scores.matmul_gflops(self.n, elapsed)}


# ------------------------------------------------------------- memory bandwidth
class MemoryBandwidth:
    """STREAM-like kernels (``copy``/``scale``/``add``/``triad``) over large buffers.

    float64, pre-allocated outputs, pages touched before timing, ``iters`` kernel
    passes per repetition calibrated to ``target_s``. ``spec["ops"]`` restricts the
    set (the agent scores ``triad`` only); the score of a repetition is the first
    op's GB/s. torch runs on ``threads`` cores; numpy's element-wise kernels are
    single-threaded and its triad is two passes, so a numpy result is a **lower
    bound** (``lower_bound = True``, S7 already flagged it).
    """

    kind = "mem_bw"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.bytes = int(spec.get("bytes", sizing.DEFAULT_MEMORY_BYTES))
        self.threads = int(spec.get("threads", 1))
        self.target_s = float(spec.get("target_s", 0.1))
        wanted = spec.get("ops") or sorted(scores.STREAM_STREAMS)
        self.wanted = [str(op) for op in wanted]
        self.itemsize = 8
        self.elements = max(1, self.bytes // self.itemsize)
        self.iters = 1
        self.backend = ""
        self.version = ""
        self.threads_effective: int | None = None
        self.lower_bound = False
        self.ops: dict[str, Callable[[], object]] = {}

    def setup(self) -> dict[str, Any]:
        unknown = sorted(set(self.wanted) - set(scores.STREAM_STREAMS))
        if unknown:
            raise BenchError("bad_request", f"unknown stream ops {unknown}")
        self.backend, lib = _blas()
        self.version = str(lib.__version__)
        if self.backend == "torch":
            lib.set_num_threads(self.threads)
            self.threads_effective = int(lib.get_num_threads())
            ops = self._torch_ops(lib)
        else:
            self.threads_effective = 1
            self.lower_bound = True
            ops = self._numpy_ops(lib)
        self.ops = {name: ops[name] for name in self.wanted}
        for run in self.ops.values():  # warm the pages (first touch is page faults)
            run()
        slowest = max(_timed(run) for run in self.ops.values())
        self.iters = _iters_for(slowest, self.target_s)
        return {
            "backend": self.backend,
            "backend_version": self.version,
            "bytes_per_buffer": self.elements * self.itemsize,
            "iters": self.iters,
            "ops": list(self.ops),
            "threads_requested": self.threads,
            "threads_effective": self.threads_effective,
            "formula": scores.STREAM_FORMULA,
            "lower_bound": self.lower_bound,
        }

    def _torch_ops(self, torch: Any) -> dict[str, Callable[[], object]]:
        n, dtype = self.elements, torch.float64
        a, b, c = (torch.full((n,), float(i + 1), dtype=dtype) for i in range(3))
        q = 3.0
        return {
            "copy": lambda: c.copy_(a),
            "scale": lambda: torch.mul(c, q, out=b),
            "add": lambda: torch.add(a, b, out=c),
            "triad": lambda: torch.add(b, c, alpha=q, out=a),
        }

    def _numpy_ops(self, numpy: Any) -> dict[str, Callable[[], object]]:
        n = self.elements
        a, b, c, d = (
            numpy.full(n, float(i + 1), dtype=numpy.float64) for i in range(4)
        )
        q = 3.0

        def triad() -> None:
            # No fused a = b + q*c in numpy: 5 streams move, the score counts 3.
            numpy.multiply(c, q, out=d)
            numpy.add(b, d, out=a)

        return {
            "copy": lambda: numpy.copyto(c, a),
            "scale": lambda: numpy.multiply(c, q, out=b),
            "add": lambda: numpy.add(a, b, out=c),
            "triad": triad,
        }

    def rep(self) -> dict[str, Any]:
        sample: dict[str, Any] = {}
        total = 0.0
        for name, run in self.ops.items():

            def batch(run: Callable[[], object] = run) -> None:
                for _ in range(self.iters):
                    run()

            elapsed = _timed(batch)
            total += elapsed
            sample[f"{name}_gb_s"] = scores.stream_gb_s(
                name, self.elements * self.iters, self.itemsize, elapsed
            )
        sample["elapsed_s"] = total
        sample["value"] = sample[f"{self.wanted[0]}_gb_s"]
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


def _pin_precision(torch: Any) -> dict[str, Any]:
    """TF32 off so ``float32`` really means fp32 (comparable across GPUs, D32)."""
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    return {
        "allow_tf32": bool(torch.backends.cuda.matmul.allow_tf32),
        "cudnn_allow_tf32": bool(torch.backends.cudnn.allow_tf32),
    }


def _device_info(torch: Any, index: int) -> dict[str, Any]:
    free, total = torch.cuda.mem_get_info(index)
    return {
        "backend": "torch-cuda",
        "backend_version": str(torch.__version__),
        "cuda_version": str(torch.version.cuda),
        "device_index": index,
        "device_name": str(torch.cuda.get_device_name(index)),
        "vram_free_bytes": int(free),
        "vram_total_bytes": int(total),
    }


def _check_device(torch: Any, index: int) -> None:
    count = int(torch.cuda.device_count())
    if not 0 <= index < count:
        raise BenchError(
            "device_mismatch", f"torch sees {count} CUDA device(s), not index {index}"
        )


class GpuMatmul:
    """``C = A @ B`` on one GPU in fp32 / fp16 (TF32 pinned off).

    ``N`` is the largest multiple of 256 whose three matrices fit in ``vram_fraction``
    of the **free** VRAM, capped at ``max_n``; ``iters`` per timed batch is
    calibrated to ``target_s``. Wall clock around ``torch.cuda.synchronize()``.
    """

    kind = "gpu_matmul"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.dtype_name = str(spec.get("dtype", "float32"))
        self.index = int(spec.get("device_index", 0))
        self.target_s = float(spec.get("target_s", 0.1))
        self.fraction = float(spec.get("vram_fraction", sizing.VRAM_FRACTION))
        self.max_n = int(spec.get("max_n", sizing.MAX_GPU_N))
        self.n = 0
        self.iters = 1
        self._torch: Any = None
        self._mats: tuple[Any, Any, Any] | None = None

    def setup(self) -> dict[str, Any]:
        torch = _torch_cuda()
        self._torch = torch
        _check_device(torch, self.index)
        dtype = getattr(torch, self.dtype_name, None)
        if dtype is None:
            raise BenchError(
                "unsupported_dtype", f"torch has no dtype {self.dtype_name}"
            )
        info = _device_info(torch, self.index)
        precision = _pin_precision(torch)
        itemsize = _ITEMSIZE.get(self.dtype_name, 4)
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
            a = torch.rand((self.n, self.n), dtype=dtype, device=device)
            b = torch.rand((self.n, self.n), dtype=dtype, device=device)
            c = torch.empty((self.n, self.n), dtype=dtype, device=device)
            self._mats = (a, b, c)
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
            "iters": self.iters,
            "formula": scores.MATMUL_FORMULA,
            "threads_effective": None,
            "lower_bound": False,
        }

    def rep(self) -> dict[str, Any]:
        torch, mats = self._torch, self._mats
        if mats is None:
            raise BenchError("not_set_up", "setup() did not complete")
        a, b, c = mats
        try:
            torch.cuda.synchronize()
            start = time.perf_counter()
            for _ in range(self.iters):
                torch.mm(a, b, out=c)
            torch.cuda.synchronize()
            elapsed = time.perf_counter() - start
        except Exception as exc:
            raise BenchError("gpu_rep_failed", _describe(exc)) from exc
        return {
            "elapsed_s": elapsed,
            "value": scores.matmul_tflops(self.n, elapsed, self.iters),
        }


class GpuCopy:
    """Device-to-device copy bandwidth (``b.copy_(a)``), read + write counted."""

    kind = "gpu_copy"

    def __init__(self, spec: dict[str, Any]) -> None:
        self.bytes = int(spec.get("bytes", 256 * 2**20))
        self.index = int(spec.get("device_index", 0))
        self.target_s = float(spec.get("target_s", 0.1))
        self.iters = 1
        self._torch: Any = None
        self._buffers: tuple[Any, Any] | None = None

    def setup(self) -> dict[str, Any]:
        torch = _torch_cuda()
        self._torch = torch
        _check_device(torch, self.index)
        info = _device_info(torch, self.index)
        budget = int(info["vram_free_bytes"] * sizing.VRAM_FRACTION / 2)
        self.bytes = min(self.bytes, budget)
        elements = self.bytes // 4
        if self.bytes < 2**20 or elements < 1:
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
            "formula": "2 * bytes * iters / elapsed_s (read + write)",
            "threads_effective": None,
            "lower_bound": False,
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
            "value": scores.bytes_gb_s(2 * self.bytes * self.iters, elapsed),
        }


KINDS: Final[dict[str, type[Any]]] = {
    cls.kind: cls for cls in (CpuMatmul, MemoryBandwidth, GpuMatmul, GpuCopy)
}


# ----------------------------------------------------------------------- probing
def capabilities() -> dict[str, Any]:
    """What this machine can measure; the runner plans its cases from this.

    ``cuda.devices`` lists what torch sees, in torch's index order - which is NVML's
    order only because the runner sets ``CUDA_DEVICE_ORDER=PCI_BUS_ID`` (D32).
    """
    out: dict[str, Any] = {
        "python": sys.version.split()[0],
        "cpu_count": os.cpu_count(),
        "numpy": None,
        "torch": None,
        "cuda_device_order": os.environ.get("CUDA_DEVICE_ORDER"),
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
            "error": "torch is not installed (ADR-0011)",
        }
        return out
    out["torch"] = str(torch.__version__)
    try:
        if not torch.cuda.is_available():
            out["cuda"] = {
                "available": False,
                "failure_type": "cuda_unavailable",
                "error": "torch.cuda.is_available() is False",
            }
            return out
        out["cuda"] = {
            "available": True,
            "devices": [
                _device_info(torch, i) for i in range(int(torch.cuda.device_count()))
            ],
        }
    except Exception as exc:  # noqa: BLE001 - a broken CUDA stack is data
        out["cuda"] = {
            "available": False,
            "failure_type": "cuda_broken",
            "error": _describe(exc),
        }
    return out
