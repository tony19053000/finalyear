"""Run one benchmark kernel and describe the run (V0.9 G2, D214).

Two callers, one code path:

* the **job entrypoint** ``run_workload.py`` (generated into every benchmark job
  folder) calls :func:`main`, which reads ``workload_params.json`` from the job's
  working directory and writes ``results/workload.json``;
* :meth:`meshforge.bench.workloads.base.Family.run` calls :func:`execute`
  in-process (tests, local calibration).

Timing rule (D213, brief §5c): ``compute_ns`` is ``time.perf_counter_ns()``
around the kernel's ``compute`` **only** - data generation (``prepare``), the
warm-up and the self-check are measured separately and never included. It is a
monotonic duration measured where the work runs, never a difference of
wall-clock timestamps.

``results/workload.json`` (``schema_version`` 1)::

    {
        "schema_version": 1,
        "family": "cpu.matmul",
        "params": {...},
        "seed": 7,
        "status": "ok" | "check_failed" | "unavailable" | "bad_params",
        "reason": null | "...",
        "device": "cpu",
        "compute_ns": 123,
        "compute_ms": 0.000123,
        "prepare_ms": ...,
        "warmup_ms": ...,
        "check_ms": ...,
        "checksum": "sha256:...",
        "checksum_exact": true,
        "verified": true,
        "check_detail": "...",
        "libraries": {...},
        "platform": {...},
    }

Exit codes: 0 ok, :data:`EXIT_BAD_PARAMS` (2), :data:`EXIT_UNAVAILABLE` (3),
:data:`EXIT_CHECK_FAILED` (4). Anything unexpected is not caught: the job fails
with its traceback in the job log (``user_error``), which is the honest outcome
for a bug in a kernel.
"""

from __future__ import annotations

import importlib
import json
import os
import platform
import sys
import time
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from ._support import KernelParamsError, Unavailable, canonical_json

__all__ = [
    "DEFAULT_DEVICE",
    "EXIT_BAD_PARAMS",
    "EXIT_CHECK_FAILED",
    "EXIT_UNAVAILABLE",
    "KERNEL_MODULES",
    "MAX_SEED",
    "PARAMS_FILE",
    "PARAM_LIMITS",
    "PARAM_MINIMA",
    "RESULT_FILE",
    "RESULT_SCHEMA_VERSION",
    "check_params",
    "check_seed",
    "execute",
    "main",
]

RESULT_SCHEMA_VERSION: Final = 1
PARAMS_FILE: Final = "workload_params.json"
RESULT_FILE: Final = "results/workload.json"

EXIT_BAD_PARAMS: Final = 2
EXIT_UNAVAILABLE: Final = 3
EXIT_CHECK_FAILED: Final = 4

KERNEL_MODULES: Final[dict[str, str]] = {
    "cpu.matmul": "cpu",
    "cpu.primes": "cpu",
    "cpu.sort": "cpu",
    "cpu.zlib": "cpu",
    "cpu.sha256": "cpu",
    "cpu.integrate": "cpu",
    "memory.copy": "memory",
    "memory.groupby": "memory",
    "gpu.matmul": "gpu",
    "gpu.conv2d": "gpu",
    "gpu.elementwise": "gpu",
    "gpu.resize": "gpu",
    "ml.cnn_infer": "ml",
    "ml.cnn_train": "ml",
    "ml.transformer_infer": "ml",
}
"""Family name -> the kernel module that implements it (the module is the only
one, besides ``_support`` and this one, copied into that family's job folder)."""

DEFAULT_DEVICE: Final[dict[str, str]] = {
    "cpu": "cpu",
    "memory": "cpu",
    "gpu": "cuda",
    "ml": "cuda",
}
"""Kernel module -> the device a job always uses."""


MAX_SEED: Final = 2**32 - 1

PARAM_LIMITS: Final[dict[str, dict[str, Any]]] = {
    "cpu.matmul": {"n": 4096, "repeats": 500, "dtype": ("float64", "float32")},
    "cpu.primes": {"limit": 400_000_000, "repeats": 100},
    "cpu.sort": {"n": 50_000_000, "repeats": 500},
    "cpu.zlib": {"size_mb": 256, "level": (1, 6, 9), "repeats": 100},
    "cpu.sha256": {"size_mb": 400_000, "chunk_kb": (4, 64, 1024)},
    "cpu.integrate": {"points_m": 4000, "repeats": 50},
    "memory.copy": {"size_mb": 1024, "repeats": 5000},
    "memory.groupby": {"rows_m": 40, "groups": 1_000_000, "repeats": 100},
    "gpu.matmul": {"n": 12288, "repeats": 5000, "dtype": ("float32", "float16")},
    "gpu.conv2d": {"batch": 256, "channels": 512, "size": 256, "repeats": 5000},
    "gpu.elementwise": {"elements_m": 256, "ops": 64, "repeats": 5000},
    "gpu.resize": {"batch": 256, "size": 1024, "out_size": 2048, "repeats": 5000},
    "ml.cnn_infer": {
        "batch": 512,
        "width": 256,
        "depth": 12,
        "image": (32, 64),
        "batches": 100_000,
    },
    "ml.cnn_train": {
        "batch": 512,
        "width": 256,
        "depth": 12,
        "image": (32, 64),
        "steps": 50_000,
    },
    "ml.transformer_infer": {
        "batch": 256,
        "seq_len": 2048,
        "width": 1024,
        "depth": 24,
        "batches": 100_000,
    },
}
"""Job-side parameter limits (D6 of tester r1): an integer limit means
``1 <= value <= limit``, a tuple lists the allowed values. The **maxima and choices
equal the family models'** (a unit test holds them together); the minimum is 1, not
the family's, because the 5 s - 3 min window is the library's rule and tiny runs
are how the kernels are tested. A damaged ``workload_params.json`` therefore ends
as ``bad_params`` (exit 2), never as a traceback or a huge allocation."""

PARAM_MINIMA: Final[dict[tuple[str, str], int]] = {("cpu.primes", "limit"): 2}
"""Job-side minima above 1 (tester r2 D3): a sieve below 2 has no primes to
check. Every minimum is <= the family model's (a unit test holds that)."""

_MULTIPLE_OF: Final[dict[tuple[str, str], int]] = {
    ("ml.transformer_infer", "width"): 64
}


def check_seed(seed: object) -> int:
    """``seed`` as an int in ``0..MAX_SEED`` (bool, float, str, None refused)."""
    if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed <= MAX_SEED:
        raise KernelParamsError(f"seed: must be an integer 0..{MAX_SEED}, got {seed!r}")
    return seed


def check_params(family: str, params: Mapping[str, Any]) -> None:
    """Refuse unknown keys and values outside :data:`PARAM_LIMITS` (keys that are
    absent are reported by the kernel itself when it reads them)."""
    limits = PARAM_LIMITS.get(family)
    if limits is None:
        raise KernelParamsError(f"unknown workload family {family!r}")
    for name, value in params.items():
        if name not in limits:
            raise KernelParamsError(f"{name}: unknown parameter for {family}")
        limit = limits[name]
        if isinstance(limit, tuple):
            if isinstance(value, bool) or value not in limit:
                raise KernelParamsError(
                    f"{name}: expected one of {list(limit)}, got {value!r}"
                )
            continue
        if isinstance(value, bool) or not isinstance(value, int):
            raise KernelParamsError(f"{name}: expected an integer, got {value!r}")
        low = PARAM_MINIMA.get((family, name), 1)
        if not low <= value <= limit:
            raise KernelParamsError(f"{name}: must be {low}..{limit}, got {value}")
        multiple = _MULTIPLE_OF.get((family, name), 1)
        if value % multiple:
            raise KernelParamsError(f"{name}: must be a multiple of {multiple}")


def _ms(ns: int) -> float:
    return round(ns / 1e6, 6)


def libraries() -> dict[str, str | None]:
    """Versions of what the measured code ran on (only what was imported)."""
    found: dict[str, str | None] = {
        "python": platform.python_version(),
        "zlib": zlib.ZLIB_RUNTIME_VERSION,
        "numpy": None,
        "torch": None,
        "cuda": None,
        "gpu": None,
    }
    numpy = sys.modules.get("numpy")
    if numpy is not None:
        found["numpy"] = str(numpy.__version__)
    torch = sys.modules.get("torch")
    if torch is not None:
        found["torch"] = str(torch.__version__)
        found["cuda"] = torch.version.cuda
        if torch.cuda.is_available():
            found["gpu"] = str(torch.cuda.get_device_name(0))
    return found


def _platform() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
    }


def execute(
    family: str,
    params: Mapping[str, Any],
    seed: int,
    *,
    device: str | None = None,
) -> dict[str, Any]:
    """Run ``family`` once and return the ``workload.json`` document.

    Raises :class:`Unavailable` (no CUDA / torch / numpy) and
    :class:`KernelParamsError` (unknown family, bad params); a failed self-check
    is *not* an exception - it is ``status: "check_failed"``.
    """
    module_name = KERNEL_MODULES.get(family)
    if module_name is None:
        raise KernelParamsError(f"unknown workload family {family!r}")
    check_seed(seed)
    check_params(family, params)
    module = importlib.import_module(f".{module_name}", __package__)
    kernel = module.KERNELS[family]
    used = device or DEFAULT_DEVICE[module_name]

    started = time.perf_counter_ns()
    state = kernel.prepare(params, seed, used)
    prepared = time.perf_counter_ns()
    if kernel.warmup is not None:
        kernel.warmup(state)
    warmed = time.perf_counter_ns()
    begin = time.perf_counter_ns()
    output = kernel.compute(state)
    compute_ns = time.perf_counter_ns() - begin
    checked_at = time.perf_counter_ns()
    check = kernel.check(state, output)
    check_ns = time.perf_counter_ns() - checked_at

    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "family": family,
        "params": dict(params),
        "seed": seed,
        "status": "ok" if check.ok else "check_failed",
        "reason": None if check.ok else check.detail,
        "device": used,
        "compute_ns": compute_ns,
        "compute_ms": _ms(compute_ns),
        "prepare_ms": _ms(prepared - started),
        "warmup_ms": _ms(warmed - prepared),
        "check_ms": _ms(check_ns),
        "checksum": check.checksum,
        "checksum_exact": check.exact,
        "verified": check.ok,
        "check_detail": check.detail,
        "libraries": libraries(),
        "platform": _platform(),
    }


def _not_run(
    family: str, params: Mapping[str, Any], seed: int, status: str, reason: str
) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION,
        "family": family,
        "params": dict(params),
        "seed": seed,
        "status": status,
        "reason": reason,
        "device": None,
        "compute_ns": None,
        "compute_ms": None,
        "verified": False,
        "libraries": libraries(),
        "platform": _platform(),
    }


def _write(workdir: Path, document: Mapping[str, Any]) -> None:
    target = workdir / RESULT_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, sort_keys=True, indent=2, allow_nan=False) + "\n"
    target.write_text(text, encoding="utf-8", newline="\n")


def _read_request(workdir: Path) -> tuple[str, dict[str, Any], int]:
    raw = json.loads((workdir / PARAMS_FILE).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise KernelParamsError(f"{PARAMS_FILE} must hold a JSON object")
    family, params, seed = raw.get("family"), raw.get("params"), raw.get("seed")
    if not isinstance(family, str) or not isinstance(params, dict):
        raise KernelParamsError(f"{PARAMS_FILE} needs 'family' and 'params'")
    return family, params, check_seed(seed)


def main(workdir: Path | None = None) -> int:
    """The job entrypoint: one run, ``results/workload.json``, an exit code."""
    root = Path.cwd() if workdir is None else workdir
    try:
        family, params, seed = _read_request(root)
    except (OSError, ValueError) as exc:  # KernelParamsError and JSON errors too
        _write(root, _not_run("", {}, 0, "bad_params", str(exc)))
        print(f"benchmark: bad {PARAMS_FILE}: {exc}", file=sys.stderr)  # noqa: T201
        return EXIT_BAD_PARAMS
    print(f"benchmark: {family} {canonical_json(params)} seed={seed}", flush=True)  # noqa: T201
    try:
        document = execute(family, params, seed)
    except Unavailable as exc:
        _write(root, _not_run(family, params, seed, "unavailable", exc.reason))
        print(f"benchmark: unavailable here: {exc.reason}", file=sys.stderr)  # noqa: T201
        return EXIT_UNAVAILABLE
    except KernelParamsError as exc:
        _write(root, _not_run(family, params, seed, "bad_params", str(exc)))
        print(f"benchmark: bad parameters: {exc}", file=sys.stderr)  # noqa: T201
        return EXIT_BAD_PARAMS
    _write(root, document)
    print(  # noqa: T201
        f"benchmark: {document['status']} compute_ms={document['compute_ms']} "
        f"{document['check_detail']}",
        flush=True,
    )
    return 0 if document["verified"] else EXIT_CHECK_FAILED
