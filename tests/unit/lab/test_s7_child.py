"""Unit tests for the S7 benchmark child (0.2.11).

The stdlib benchmarks run for real (they need nothing installed); the torch and
numpy paths run against small fakes, so the GPU code - which no CI machine and
not even Laptop 1 can run today (torch is not installed, ADR-0011) - is still
covered: size choice from free VRAM, event timing, and every failure type.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from spikes._s7 import child, sizing

_REAL_MODULE = child._module


@pytest.fixture(autouse=True)
def _no_real_backends(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Every test here sees torch and numpy as not installed unless it fakes them.

    Without this the stdlib-path tests imported the *real* torch in the pytest
    process whenever the venv had it. Under ``filterwarnings = error`` that
    import raises half-way (torch warns when numpy is absent), and the repeated
    half-imports crashed the interpreter at exit: a green summary, then exit 139.
    It also made ``backend == "python_fallback"`` depend on what is installed.
    ``sys.modules[name] = None`` is the standard way to make an import fail.
    """
    for name in ("torch", "numpy"):
        monkeypatch.setitem(sys.modules, name, None)
    _REAL_MODULE.cache_clear()
    yield
    _REAL_MODULE.cache_clear()


# --------------------------------------------------------------- stdlib paths
def test_cpu_python_fallback_measures_and_labels_itself() -> None:
    bench = child.CpuPython({"kind": "cpu_python", "target_s": 0.01})
    info = bench.setup()
    assert info["backend"] == "python_fallback"
    assert info["formula"] == "2 * elements / elapsed_s"
    assert "not peak FLOPS" in info["caveat"]
    assert info["elements"] >= 1
    assert info["calibration"][-1]["size"] == info["elements"]
    sample = bench.rep()
    assert sample["python_mflops"] > 0
    assert sample["elapsed_s"] > 0
    assert "gflops" not in sample  # never the name of a real BLAS score


def test_memory_bandwidth_stdlib_reports_only_copy() -> None:
    bench = child.MemoryBandwidth({"kind": "mem_bw", "bytes": 4 * 2**20})
    info = bench.setup()
    assert info["backend"] == "python_fallback"
    assert info["ops"] == ["copy"]
    assert info["unsupported_ops"] == ["add", "scale", "triad"]
    assert info["streams_per_op"]["triad"] == 3
    assert any("memcpy" in note for note in info["notes"])
    sample = bench.rep()
    assert sample["copy_gb_s"] > 0
    assert sample["elapsed_s"] == pytest.approx(sample["copy_s"])
    assert "triad_gb_s" not in sample


def test_memory_bandwidth_batches_iters_to_the_target_and_reports_them() -> None:
    bench = child.MemoryBandwidth(
        {"kind": "mem_bw", "bytes": 2 * 2**20, "target_s": 0.05}
    )
    info = bench.setup()
    assert 1 <= info["iters"] <= child.MAX_ITERS
    assert info["single_pass_s"] > 0
    # 2 MiB memcpy is microseconds: the batch must have been grown well past 1.
    assert info["iters"] > 1
    sample = bench.rep()
    assert sample["iters"] == info["iters"]
    assert sample["copy_gb_s"] == pytest.approx(
        2 * info["elements"] * info["itemsize"] * info["iters"] / sample["copy_s"] / 1e9
    )


def test_memory_bandwidth_stdlib_is_flagged_single_threaded_lower_bound() -> None:
    bench = child.MemoryBandwidth({"kind": "mem_bw", "bytes": 2**20, "threads": 8})
    info = bench.setup()
    assert info["threads_requested"] == 8
    assert info["threads_effective"] == 1
    assert any("single-threaded" in n and "lower bound" in n for n in info["notes"])


def test_iters_for_is_bounded() -> None:
    assert child._iters_for(0.001, 0.2) == 200
    assert child._iters_for(0.0, 0.2) == child.MAX_ITERS
    assert child._iters_for(5.0, 0.2) == 1


def test_cpu_matmul_without_any_backend_is_a_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child, "_module", lambda _name: None)
    with pytest.raises(child.BenchError) as exc:
        child.CpuMatmul({"kind": "cpu_matmul"}).setup()
    assert exc.value.failure_type == "no_blas_backend"
    assert "cpu_python" in str(exc.value)


# ------------------------------------------------------------------ fake numpy
class _FakeArray:
    def __init__(self, size: int) -> None:
        self.size = size


class _FakeRng:
    def random(self, shape: tuple[int, int], dtype: object) -> _FakeArray:
        return _FakeArray(shape[0])


class _FakeNumpy:
    __version__ = "2.3.1"
    float32 = "f4"
    float64 = "f8"

    def __init__(self) -> None:
        self.random = type("R", (), {"default_rng": lambda _s, _seed: _FakeRng()})()
        self.matmuls = 0

    def empty(self, shape: tuple[int, int], dtype: object) -> _FakeArray:
        return _FakeArray(shape[0])

    def matmul(self, a: _FakeArray, b: _FakeArray, out: _FakeArray) -> _FakeArray:
        self.matmuls += 1
        return out


def test_cpu_matmul_uses_numpy_when_torch_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeNumpy()
    monkeypatch.setattr(
        child, "_module", lambda name: fake if name == "numpy" else None
    )
    bench = child.CpuMatmul(
        {"kind": "cpu_matmul", "dtype": "float64", "threads": 4, "target_s": 0.0001}
    )
    info = bench.setup()
    assert info["backend"] == "numpy"
    assert info["backend_version"] == "2.3.1"
    assert info["dtype"] == "float64"
    assert info["flops"] == 2 * info["n"] ** 3
    assert info["formula"] == "2 * N^3 * iters / elapsed_s"
    assert info["threads_requested"] == 4
    sample = bench.rep()
    assert sample["gflops"] > 0
    assert fake.matmuls >= 2


def test_cpu_matmul_rejects_an_unknown_dtype(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        child, "_module", lambda name: _FakeNumpy() if name == "numpy" else None
    )
    with pytest.raises(child.BenchError) as exc:
        child.CpuMatmul({"kind": "cpu_matmul", "dtype": "float8"}).setup()
    assert exc.value.failure_type == "unsupported_dtype"


# ------------------------------------------------------------------ fake torch
class _FakeTensor:
    def __init__(self, n: int, dtype: str) -> None:
        self.n = n
        self.dtype = dtype

    def to(self, dtype: str) -> _FakeTensor:
        return _FakeTensor(self.n, dtype)

    def copy_(self, other: _FakeTensor) -> _FakeTensor:
        return self


class _FakeEvent:
    def __init__(self, enable_timing: bool = False) -> None:
        self.enabled = enable_timing

    def record(self) -> None:
        return None

    def elapsed_time(self, _other: _FakeEvent) -> float:
        return 2.0  # milliseconds


class _FakeCuda:
    def __init__(self, free: int, bf16: bool = True, broken: bool = False) -> None:
        self.free = free
        self.bf16 = bf16
        self.broken = broken
        self.syncs = 0
        self.Event = _FakeEvent

    def is_available(self) -> bool:
        if self.broken:
            raise RuntimeError("CUDA driver version is insufficient")
        return True

    def is_bf16_supported(self) -> bool:
        return self.bf16

    def device_count(self) -> int:
        return 1

    def mem_get_info(self, _index: int = 0) -> tuple[int, int]:
        return self.free, 4 * 2**30

    def get_device_properties(self, _index: int) -> Any:
        return type("P", (), {"multi_processor_count": 20})()

    def get_device_name(self, _index: int) -> str:
        return "NVIDIA GeForce RTX 3050 Laptop GPU"

    def get_device_capability(self, _index: int) -> tuple[int, int]:
        return (8, 6)

    def synchronize(self, _device: object = None) -> None:
        self.syncs += 1

    def max_memory_allocated(self, _index: int = 0) -> int:
        return 123 * 2**20


class _FakeMatmulBackend:
    allow_tf32 = True  # torch's default on Ampere: must be pinned off


class _FakeCudnnBackend:
    allow_tf32 = True


class _FakeBackends:
    def __init__(self) -> None:
        self.cuda = type("C", (), {"matmul": _FakeMatmulBackend()})()
        self.cudnn = _FakeCudnnBackend()


class _FakeTorch:
    __version__ = "2.14.0+cu126"
    float32 = "fp32"
    float16 = "fp16"
    bfloat16 = "bf16"
    float64 = "fp64"

    def __init__(self, free: int = 3 * 2**30, **kwargs: Any) -> None:
        self.cuda = _FakeCuda(free, **kwargs)
        self.version = type("V", (), {"cuda": "12.6"})()
        self.backends = _FakeBackends()
        self.mms = 0
        self.rand_dtypes: list[str] = []
        self.num_threads = 1

    def device(self, kind: str, index: int) -> str:
        return f"{kind}:{index}"

    def set_num_threads(self, n: int) -> None:
        self.num_threads = n

    def get_num_threads(self) -> int:
        return self.num_threads

    def get_float32_matmul_precision(self) -> str:
        return "highest"

    def rand(self, shape: tuple[int, int], dtype: str, device: str = "") -> _FakeTensor:
        self.rand_dtypes.append(dtype)
        return _FakeTensor(shape[0], dtype)

    def full(self, shape: tuple[int], value: float, dtype: str) -> _FakeTensor:
        return _FakeTensor(shape[0], dtype)

    def mul(self, a: _FakeTensor, q: float, out: _FakeTensor) -> _FakeTensor:
        return out

    def add(
        self, a: _FakeTensor, b: _FakeTensor, alpha: float = 1.0, out: Any = None
    ) -> _FakeTensor:
        assert isinstance(out, _FakeTensor)  # the fake always passes out=
        return out

    def empty(
        self, shape: tuple[int, int], dtype: str, device: str = ""
    ) -> _FakeTensor:
        return _FakeTensor(shape[0], dtype)

    def zeros(self, n: int, dtype: str, device: str = "") -> _FakeTensor:
        return _FakeTensor(n, dtype)

    def mm(self, a: _FakeTensor, b: _FakeTensor, out: _FakeTensor) -> _FakeTensor:
        self.mms += 1
        return out


def _with_torch(monkeypatch: pytest.MonkeyPatch, torch: _FakeTorch) -> None:
    monkeypatch.setattr(
        child, "_module", lambda name: torch if name == "torch" else None
    )


def test_gpu_matmul_sizes_itself_from_free_vram_and_times_with_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = _FakeTorch(free=3 * 2**30)
    _with_torch(monkeypatch, torch)
    bench = child.GpuMatmul({"kind": "gpu_matmul", "dtype": "float32", "target_s": 0.2})
    info = bench.setup()
    assert info["n"] == sizing.gpu_matmul_n(3 * 2**30, 4)
    assert info["matrix_bytes"] <= 3 * 2**30 * 0.5
    assert info["device_name"].startswith("NVIDIA")
    assert info["capability"] == "8.6"
    assert info["cuda_version"] == "12.6"
    # every kernel kind records `backend` (found missing for GPU kernels the first
    # time the component suite ran with a real CUDA torch, 2026-09-20)
    assert info["backend"].startswith("torch-cuda ")
    assert 1 <= info["iters"] <= child.MAX_ITERS
    sample = bench.rep()
    assert sample["tflops"] > 0
    assert sample["event_s"] == pytest.approx(0.002)
    assert sample["event_tflops"] == pytest.approx(
        2 * info["n"] ** 3 * info["iters"] / 0.002 / 1e12
    )
    assert torch.cuda.syncs >= 2  # synchronised before and after the timed batch
    assert info["max_memory_allocated_bytes"] == 123 * 2**20


def test_gpu_matmul_pins_tf32_off_and_records_the_precision_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = _FakeTorch()
    _with_torch(monkeypatch, torch)
    info = child.GpuMatmul({"kind": "gpu_matmul", "dtype": "float32"}).setup()
    assert torch.backends.cuda.matmul.allow_tf32 is False
    assert torch.backends.cudnn.allow_tf32 is False
    assert info["allow_tf32"] is False
    assert info["cudnn_allow_tf32"] is False
    assert info["float32_matmul_precision"] == "highest"


def test_gpu_half_precision_matrices_are_generated_in_their_dtype(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No fp32 intermediate: the peak allocation is 3 x N^2 x 2 bytes, not more."""
    torch = _FakeTorch()
    _with_torch(monkeypatch, torch)
    child.GpuMatmul({"kind": "gpu_matmul", "dtype": "float16"}).setup()
    assert torch.rand_dtypes == ["fp16", "fp16"]


def test_gpu_matmul_respects_the_quick_size_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = _FakeTorch(free=8 * 2**30)  # room for N far above the quick cap
    _with_torch(monkeypatch, torch)
    full = child.GpuMatmul({"kind": "gpu_matmul", "dtype": "float32"}).setup()
    quick = child.GpuMatmul(
        {"kind": "gpu_matmul", "dtype": "float32", "max_n": sizing.MAX_GPU_N_QUICK}
    ).setup()
    assert full["n"] > sizing.MAX_GPU_N_QUICK
    assert quick["n"] == sizing.MAX_GPU_N_QUICK
    assert quick["max_n"] == sizing.MAX_GPU_N_QUICK


def test_gpu_matmul_refuses_to_run_without_enough_free_vram(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_torch(monkeypatch, _FakeTorch(free=1 * 2**20))  # < 3 x 256^2 fp32
    with pytest.raises(child.BenchError) as exc:
        child.GpuMatmul({"kind": "gpu_matmul"}).setup()
    assert exc.value.failure_type == "insufficient_vram"


def test_gpu_bfloat16_is_refused_on_a_card_without_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_torch(monkeypatch, _FakeTorch(bf16=False))
    with pytest.raises(child.BenchError) as exc:
        child.GpuMatmul({"kind": "gpu_matmul", "dtype": "bfloat16"}).setup()
    assert exc.value.failure_type == "unsupported_dtype"


def test_gpu_allocation_failure_is_recorded_not_raised_as_oom(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = _FakeTorch()

    def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("CUDA out of memory")

    torch.rand = boom  # type: ignore[assignment]
    _with_torch(monkeypatch, torch)
    with pytest.raises(child.BenchError) as exc:
        child.GpuMatmul({"kind": "gpu_matmul"}).setup()
    assert exc.value.failure_type == "gpu_setup_failed"
    assert "out of memory" in str(exc.value)


def test_gpu_copy_bandwidth(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_torch(monkeypatch, _FakeTorch())
    bench = child.GpuCopy({"kind": "gpu_copy", "bytes": 64 * 2**20})
    info = bench.setup()
    assert info["bytes_per_buffer"] <= 64 * 2**20
    sample = bench.rep()
    assert sample["device_copy_gb_s"] > 0


def test_gpu_rep_before_setup_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_torch(monkeypatch, _FakeTorch())
    with pytest.raises(child.BenchError) as exc:
        child.GpuMatmul({"kind": "gpu_matmul"}).rep()
    assert exc.value.failure_type == "not_set_up"


@pytest.mark.parametrize(
    ("torch_factory", "failure"),
    [
        (lambda: None, "torch_not_installed"),
        (lambda: _FakeTorch(broken=True), "cuda_broken"),
    ],
)
def test_missing_or_broken_cuda_stack_is_typed(
    monkeypatch: pytest.MonkeyPatch, torch_factory: Any, failure: str
) -> None:
    torch = torch_factory()
    monkeypatch.setattr(
        child, "_module", lambda name: torch if name == "torch" else None
    )
    with pytest.raises(child.BenchError) as exc:
        child._torch_cuda()
    assert exc.value.failure_type == failure


def test_cuda_reported_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    torch = _FakeTorch()
    torch.cuda.is_available = lambda: False  # type: ignore[method-assign]
    _with_torch(monkeypatch, torch)
    with pytest.raises(child.BenchError) as exc:
        child._torch_cuda()
    assert exc.value.failure_type == "cuda_unavailable"


def test_memory_bandwidth_with_torch_uses_all_requested_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1: the STREAM score is the machine's bandwidth, so torch gets every core."""
    torch = _FakeTorch()
    _with_torch(monkeypatch, torch)
    bench = child.MemoryBandwidth(
        {"kind": "mem_bw", "bytes": 2**20, "threads": 12, "target_s": 0.001}
    )
    info = bench.setup()
    assert torch.num_threads == 12
    assert info["threads_requested"] == 12
    assert info["threads_effective"] == 12
    assert info["ops"] == ["add", "copy", "scale", "triad"]
    assert not any("single-threaded" in n for n in info["notes"])
    sample = bench.rep()
    assert set(sample) >= {"copy_gb_s", "scale_gb_s", "add_gb_s", "triad_gb_s"}


# ------------------------------------------------------------- capabilities
def test_capabilities_on_this_machine_reports_torch_honestly() -> None:
    caps = child.capabilities()
    assert caps["python"]
    assert caps["cpu_count"]
    if caps["torch"] is None:
        assert caps["cuda"]["failure_type"] == "torch_not_installed"
        assert caps["cuda"]["available"] is False


def test_capabilities_with_a_working_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_torch(monkeypatch, _FakeTorch())
    caps = child.capabilities()
    assert caps["torch"] == "2.14.0+cu126"
    assert caps["torch_cuda_build"] == "12.6"
    assert caps["cuda"]["available"] is True
    assert caps["cuda"]["bf16_supported"] is True
    assert caps["cuda"]["devices"][0]["vram_total_bytes"] == 4 * 2**30


def test_capabilities_with_a_broken_cuda_stack(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_torch(monkeypatch, _FakeTorch(broken=True))
    caps = child.capabilities()
    assert caps["cuda"]["available"] is False
    assert caps["cuda"]["failure_type"] == "cuda_broken"
    assert "insufficient" in caps["cuda"]["error"]


# ------------------------------------------------------------------ protocol
def _serve(requests: list[str]) -> list[dict[str, Any]]:
    out = io.StringIO()
    code = child.serve(io.StringIO("\n".join(requests) + "\n"), out)
    assert code == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_protocol_setup_then_reps() -> None:
    answers = _serve(
        [
            '{"op": "setup", "spec": {"kind": "cpu_python", "target_s": 0.001}}',
            '{"op": "rep"}',
            '{"op": "bye"}',
        ]
    )
    assert answers[0]["ok"] and answers[0]["info"]["backend"] == "python_fallback"
    assert answers[1]["ok"] and answers[1]["sample"]["python_mflops"] > 0
    assert len(answers) == 2  # bye answers nothing, it just exits


def test_protocol_reports_failures_and_keeps_serving() -> None:
    answers = _serve(
        [
            "not json at all",
            "[1, 2, 3]",
            '{"op": "rep"}',
            '{"op": "nope"}',
            '{"op": "setup", "spec": {"kind": "does_not_exist"}}',
            "",
            '{"op": "probe"}',
        ]
    )
    types = [a.get("failure_type") for a in answers]
    assert types[:5] == [
        "bad_request",
        "bad_request",
        "not_set_up",
        "unknown_op",
        "unknown_kind",
    ]
    assert answers[5]["ok"] is True  # still alive after five bad requests
    assert "cuda" in answers[5]["info"]


def test_protocol_reports_a_setup_failure_without_dying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(child, "_module", lambda _name: None)
    answers = _serve(
        [
            '{"op": "setup", "spec": {"kind": "gpu_matmul"}}',
            '{"op": "setup", "spec": {"kind": "cpu_python", "target_s": 0.001}}',
        ]
    )
    assert answers[0] == {
        "ok": False,
        "failure_type": "torch_not_installed",
        "message": "torch is not installed",
    }
    assert answers[1]["ok"] is True


def test_probe_mode_prints_capabilities(capsys: pytest.CaptureFixture[str]) -> None:
    assert child.main(["--probe"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True
    assert "cuda" in answer["info"]


class _NoisyBench:
    """A benchmark whose library prints to stdout (as some BLAS wrappers do)."""

    kind = "noisy"

    def __init__(self, _spec: dict[str, Any]) -> None:
        pass

    def setup(self) -> dict[str, Any]:
        print("hello from a chatty library")  # noqa: T201 - the point of the test
        return {"backend": "noisy"}

    def rep(self) -> dict[str, Any]:
        print("rep noise")  # noqa: T201
        return {"elapsed_s": 0.001}


def test_main_keeps_stray_prints_off_the_protocol_stream(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setitem(child.KINDS, "noisy", _NoisyBench)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO('{"op": "setup", "spec": {"kind": "noisy"}}\n{"op": "rep"}\n'),
    )
    assert child.main([]) == 0
    captured = capsys.readouterr()
    answers = [json.loads(line) for line in captured.out.splitlines()]
    assert [a["ok"] for a in answers] == [True, True]
    assert answers[0]["info"] == {"backend": "noisy"}
    assert "chatty" in captured.err and "rep noise" in captured.err
    assert "chatty" not in captured.out


def test_unknown_module_import_failure_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    assert child._module("definitely_not_a_module_xyz") is None


def test_a_failed_import_is_attempted_once_per_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Re-running a half-failed native import is what corrupted the interpreter
    # (exit 139 at shutdown), so _module must never retry one.
    runs = tmp_path / "runs.txt"
    package = tmp_path / "s7_half_failing_pkg"
    package.mkdir()
    (package / "__init__.py").write_text(
        f"with open({str(runs)!r}, 'a') as f:\n"
        "    f.write('x')\n"
        "raise RuntimeError('import raised half-way')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "s7_half_failing_pkg", raising=False)
    for _ in range(3):
        assert child._module("s7_half_failing_pkg") is None
    assert runs.read_text(encoding="utf-8") == "x"
