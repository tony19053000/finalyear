"""G7-A (0.3.4, D30-D33): the lifted kernels and the child protocol, in-process.

torch and numpy are blocked in ``sys.modules`` for every test (the exit-139 rule);
GPU paths run against ``mf_bench_fakes.FakeTorch``.
"""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from mf_bench_fakes import FakeTorch, block_backends

from meshforge.agent.benchmarks import child, kernels, sizing
from meshforge.agent.benchmarks.kernels import BenchError

pytestmark = pytest.mark.unit

_REAL_MODULE = kernels._module


@pytest.fixture(autouse=True)
def _blocked(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    block_backends(monkeypatch)
    yield
    _REAL_MODULE.cache_clear()


def _with_torch(monkeypatch: pytest.MonkeyPatch, torch: FakeTorch) -> None:
    monkeypatch.setattr(
        kernels, "_module", lambda name: torch if name == "torch" else None
    )


# ---------------------------------------------------------------- no backend
@pytest.mark.parametrize("cls", [kernels.CpuMatmul, kernels.MemoryBandwidth])
def test_host_kernels_without_blas_are_typed_no_blas_backend(cls: Any) -> None:
    with pytest.raises(BenchError) as exc:
        cls({"kind": cls.kind}).setup()
    assert exc.value.failure_type == "no_blas_backend"


@pytest.mark.parametrize("cls", [kernels.GpuMatmul, kernels.GpuCopy])
def test_gpu_kernels_without_torch_are_typed(cls: Any) -> None:
    with pytest.raises(BenchError) as exc:
        cls({"kind": cls.kind}).setup()
    assert exc.value.failure_type == "torch_not_installed"


def test_no_pure_python_fallback_is_shipped() -> None:
    assert set(kernels.KINDS) == {"cpu_matmul", "mem_bw", "gpu_matmul", "gpu_copy"}


def test_capabilities_without_backends() -> None:
    caps = kernels.capabilities()
    assert caps["torch"] is None and caps["numpy"] is None
    assert caps["cuda"]["failure_type"] == "torch_not_installed"


def test_module_is_cached_including_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs.txt"
    pkg = tmp_path / "g7_half_failing_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text(
        f"with open({str(runs)!r}, 'a') as f:\n    f.write('x')\n"
        "raise RuntimeError('import raised half-way')\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "g7_half_failing_pkg", raising=False)
    for _ in range(3):
        assert kernels._module("g7_half_failing_pkg") is None
    assert runs.read_text(encoding="utf-8") == "x"


# ---------------------------------------------------------------------- GPU
def test_gpu_matmul_sizes_from_free_vram_and_pins_tf32_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    torch = FakeTorch(free=3 * 2**30)
    _with_torch(monkeypatch, torch)
    bench = kernels.GpuMatmul(
        {"kind": "gpu_matmul", "dtype": "float32", "max_n": 4096, "target_s": 0.01}
    )
    info = bench.setup()
    assert info["n"] == sizing.gpu_matmul_n(3 * 2**30, 4, max_n=4096)
    assert info["allow_tf32"] is False and info["cudnn_allow_tf32"] is False
    assert info["device_name"] == torch.cuda.names[0]
    assert info["backend"] == "torch-cuda"
    assert info["backend_version"] == "2.14.0+cu130"
    assert bench.rep()["value"] > 0


@pytest.mark.parametrize("cls", [kernels.GpuMatmul, kernels.GpuCopy])
def test_gpu_kernel_refuses_an_index_torch_does_not_have(
    monkeypatch: pytest.MonkeyPatch, cls: Any
) -> None:
    _with_torch(monkeypatch, FakeTorch())
    with pytest.raises(BenchError) as exc:
        cls({"kind": cls.kind, "device_index": 1}).setup()
    assert exc.value.failure_type == "device_mismatch"


def test_gpu_matmul_without_enough_free_vram(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_torch(monkeypatch, FakeTorch(free=2**20))
    with pytest.raises(BenchError) as exc:
        kernels.GpuMatmul({"kind": "gpu_matmul"}).setup()
    assert exc.value.failure_type == "insufficient_vram"


def test_cuda_unavailable_is_typed(monkeypatch: pytest.MonkeyPatch) -> None:
    _with_torch(monkeypatch, FakeTorch(available=False))
    with pytest.raises(BenchError) as exc:
        kernels.GpuCopy({"kind": "gpu_copy"}).setup()
    assert exc.value.failure_type == "cuda_unavailable"


def test_capabilities_report_devices_and_device_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _with_torch(monkeypatch, FakeTorch(["GPU A", "GPU B"]))
    monkeypatch.setenv("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
    caps = kernels.capabilities()
    assert caps["cuda_device_order"] == "PCI_BUS_ID"
    assert [d["device_name"] for d in caps["cuda"]["devices"]] == ["GPU A", "GPU B"]
    assert [d["device_index"] for d in caps["cuda"]["devices"]] == [0, 1]


# ------------------------------------------------------------------ protocol
def _serve(lines: list[str]) -> list[dict[str, Any]]:
    out = io.StringIO()
    assert child.serve(io.StringIO("\n".join(lines) + "\n"), out) == 0
    return [json.loads(line) for line in out.getvalue().splitlines()]


def test_protocol_answers_failures_and_keeps_serving() -> None:
    answers = _serve(
        [
            "garbage",
            "[1]",
            '{"op": "rep"}',
            '{"op": "nope"}',
            '{"op": "setup", "spec": {"kind": "cpu_python"}}',
            '{"op": "setup", "spec": {"kind": "cpu_matmul"}}',
            '{"op": "probe"}',
            '{"op": "bye"}',
            '{"op": "probe"}',
        ]
    )
    assert [a.get("failure_type") for a in answers[:6]] == [
        "bad_request",
        "bad_request",
        "not_set_up",
        "unknown_op",
        "unknown_kind",
        "no_blas_backend",
    ]
    assert answers[6]["ok"] is True and "priority" in answers[6]["info"]
    assert len(answers) == 7  # bye ends the loop


def test_probe_mode_prints_one_json_line(capsys: pytest.CaptureFixture[str]) -> None:
    assert child.main(["--probe"]) == 0
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is True
    assert answer["info"]["priority"]["lowered"] is False


def test_fault_spec_parsing_ignores_malformed_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        child.FAULT_ENV, "mem_bw:rep:hang, bad ,x:y:explode,*:setup:exit"
    )
    assert child._faults() == [("mem_bw", "rep", "hang"), ("*", "setup", "exit")]
