"""V0.9 G2 (0.9.2, D214): the job-side kernels, at tiny sizes, in-process.

Tiny params go straight to :func:`runner.execute` (the 5 s window is the
family layer's rule, not the kernel's). numpy / torch are imported through
``pytest.importorskip`` inside the tests, so a plain install collects this file
and skips what it cannot run.
"""

from __future__ import annotations

import json
import time
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from meshforge.bench.workloads.kernels import cpu as cpu_kernels
from meshforge.bench.workloads.kernels import runner
from meshforge.bench.workloads.kernels._support import (
    Check,
    Kernel,
    KernelParamsError,
    Unavailable,
    digest,
)

pytestmark = pytest.mark.unit

TINY_CPU: dict[str, dict[str, Any]] = {
    "cpu.matmul": {"n": 64, "repeats": 2, "dtype": "float64"},
    "cpu.primes": {"limit": 1_000_000, "repeats": 1},
    "cpu.sort": {"n": 10_000, "repeats": 2},
    "cpu.zlib": {"size_mb": 1, "level": 6, "repeats": 1},
    "cpu.sha256": {"size_mb": 2, "chunk_kb": 64},
    "cpu.integrate": {"points_m": 1, "repeats": 1},
    "memory.copy": {"size_mb": 2, "repeats": 2},
    "memory.groupby": {"rows_m": 1, "groups": 100, "repeats": 1},
}
NEEDS_NUMPY = {
    "cpu.matmul",
    "cpu.sort",
    "cpu.integrate",
    "memory.copy",
    "memory.groupby",
}
EXACT = {
    "cpu.primes",
    "cpu.sort",
    "cpu.zlib",
    "cpu.sha256",
    "memory.copy",
    "memory.groupby",
}
TINY_TORCH: dict[str, dict[str, Any]] = {
    "gpu.matmul": {"n": 64, "repeats": 2, "dtype": "float32"},
    "gpu.conv2d": {"batch": 2, "channels": 4, "size": 8, "repeats": 2},
    "gpu.elementwise": {"elements_m": 1, "ops": 8, "repeats": 2},
    "gpu.resize": {"batch": 2, "size": 16, "out_size": 24, "repeats": 2},
    "ml.cnn_infer": {"batch": 2, "width": 8, "depth": 3, "image": 32, "batches": 3},
    "ml.cnn_train": {"batch": 16, "width": 8, "depth": 2, "image": 32, "steps": 40},
    "ml.transformer_infer": {
        "batch": 2,
        "seq_len": 8,
        "width": 64,
        "depth": 1,
        "batches": 2,
    },
}


def _needs(family: str) -> None:
    if family in NEEDS_NUMPY:
        pytest.importorskip("numpy", reason="this kernel needs numpy (agent extra)")


def test_every_family_has_a_kernel_module() -> None:
    from meshforge.bench.workloads import list_families

    names = {f.name for f in list_families()}
    assert set(runner.KERNEL_MODULES) == names
    assert set(TINY_CPU) | set(TINY_TORCH) == names


@pytest.mark.parametrize("family", sorted(TINY_CPU))
def test_cpu_kernels_run_and_pass_their_check(family: str) -> None:
    _needs(family)
    document = runner.execute(family, TINY_CPU[family], 11)
    assert document["status"] == "ok", document
    assert document["verified"] is True
    assert document["device"] == "cpu"
    assert document["compute_ns"] > 0
    assert document["checksum"].startswith("sha256:")
    assert document["checksum_exact"] is (family in EXACT)
    assert document["schema_version"] == runner.RESULT_SCHEMA_VERSION
    json.dumps(document, allow_nan=False)


@pytest.mark.parametrize("family", sorted(TINY_CPU))
def test_same_seed_same_checksum_other_seed_other_checksum(family: str) -> None:
    _needs(family)
    first = runner.execute(family, TINY_CPU[family], 3)["checksum"]
    assert runner.execute(family, TINY_CPU[family], 3)["checksum"] == first
    other = runner.execute(family, TINY_CPU[family], 4)["checksum"]
    if family == "cpu.primes":
        # The primes below a limit do not depend on a seed (it only picks the
        # positions the check verifies by trial division).
        assert other == first
    else:
        assert other != first


def test_primes_known_count() -> None:
    document = runner.execute("cpu.primes", {"limit": 1_000_000, "repeats": 1}, 0)
    assert "78498 primes" in document["check_detail"]


def _state_and_output(family: str, seed: int = 5) -> tuple[Kernel, Any, Any]:
    kernel = cpu_kernels.KERNELS.get(family)
    if kernel is None:
        from meshforge.bench.workloads.kernels import memory

        kernel = memory.KERNELS[family]
    state = kernel.prepare(TINY_CPU[family], seed, "cpu")
    return kernel, state, kernel.compute(state)


def test_checks_catch_a_wrong_matmul() -> None:
    pytest.importorskip("numpy")
    kernel, state, c = _state_and_output("cpu.matmul")
    c[3, 7] += 1.0
    assert not kernel.check(state, c).ok


def test_checks_catch_an_unsorted_array() -> None:
    pytest.importorskip("numpy")
    kernel, state, out = _state_and_output("cpu.sort")
    out[0], out[-1] = out[-1], out[0]
    assert not kernel.check(state, out).ok


def test_checks_catch_a_corrupted_zlib_stream() -> None:
    kernel, state, out = _state_and_output("cpu.zlib")
    other = cpu_kernels.KERNELS["cpu.zlib"].prepare(TINY_CPU["cpu.zlib"], 6, "cpu")
    assert not kernel.check(state, zlib.compress(other["data"])).ok
    assert kernel.check(state, out).ok


def test_checks_catch_a_wrong_sieve_and_digest() -> None:
    kernel, state, sieve = _state_and_output("cpu.primes")
    sieve[4] = 1
    assert not kernel.check(state, sieve).ok
    kernel, state, _out = _state_and_output("cpu.sha256")
    assert not kernel.check(state, "0" * 64).ok


def test_checks_catch_a_wrong_copy_and_groupby() -> None:
    pytest.importorskip("numpy")
    kernel, state, dst = _state_and_output("memory.copy")
    dst[10] ^= 1
    assert not kernel.check(state, dst).ok
    kernel, state, out = _state_and_output("memory.groupby")
    uniq, counts, sums, maxs = out
    counts = counts.copy()
    counts[0] += 1
    assert not kernel.check(state, (uniq, counts, sums, maxs)).ok


def test_checks_catch_a_wrong_integral() -> None:
    pytest.importorskip("numpy")
    kernel, state, value = _state_and_output("cpu.integrate")
    assert not kernel.check(state, value * (1 + 1e-6)).ok


def test_compute_time_excludes_prepare_warmup_and_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def slow(_s: float) -> Any:
        return lambda *_a: time.sleep(_s)

    def prepare(_p: Mapping[str, Any], _seed: int, _d: str) -> None:
        time.sleep(0.3)

    def check(_state: Any, _out: Any) -> Check:
        time.sleep(0.2)
        return Check(ok=True, checksum=digest(b"x"), exact=True)

    fake = Kernel(prepare=prepare, compute=slow(0.05), check=check, warmup=slow(0.2))
    monkeypatch.setitem(cpu_kernels.KERNELS, "cpu.sort", fake)
    document = runner.execute("cpu.sort", {}, 1)
    assert 50 <= document["compute_ms"] < 250, document
    assert document["prepare_ms"] >= 300
    assert document["warmup_ms"] >= 200
    assert document["check_ms"] >= 200


def test_a_failed_check_is_a_status_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = Kernel(
        prepare=lambda *_a: None,
        compute=lambda _s: None,
        check=lambda _s, _o: Check(
            ok=False, checksum=digest(b""), exact=True, detail="bad"
        ),
    )
    monkeypatch.setitem(cpu_kernels.KERNELS, "cpu.sort", fake)
    document = runner.execute("cpu.sort", {}, 1)
    assert document["status"] == "check_failed"
    assert document["verified"] is False and document["reason"] == "bad"


def test_unknown_family_and_bad_params_are_typed() -> None:
    with pytest.raises(KernelParamsError, match="unknown workload family"):
        runner.execute("cpu.nope", {}, 1)
    with pytest.raises(KernelParamsError, match="limit"):
        runner.execute("cpu.primes", {"limit": "many", "repeats": 1}, 1)
    with pytest.raises(KernelParamsError, match="chunk_kb"):
        runner.execute("cpu.sha256", {"size_mb": 1, "chunk_kb": 3}, 1)


def _torch_or_skip() -> Any:
    return pytest.importorskip("torch", reason="needs torch (uv sync --extra gpu)")


@pytest.mark.parametrize("family", sorted(TINY_TORCH))
def test_torch_kernels_run_on_the_cpu_device(family: str) -> None:
    """The GPU / ML kernels' maths, checked without CUDA (jobs always use cuda)."""
    _torch_or_skip()
    document = runner.execute(family, TINY_TORCH[family], 5, device="cpu")
    assert document["status"] == "ok", document
    assert document["libraries"]["torch"]
    assert document["checksum_exact"] is False


@pytest.mark.parametrize("family", sorted(TINY_TORCH))
def test_torch_kernels_without_cuda_are_unavailable(family: str) -> None:
    torch = _torch_or_skip()
    if torch.cuda.is_available():
        pytest.skip("this host has CUDA; the no-CUDA path cannot be observed")
    with pytest.raises(Unavailable, match="no CUDA device"):
        runner.execute(family, TINY_TORCH[family], 5)


def test_cnn_training_repeats_exactly_on_the_cpu() -> None:
    _torch_or_skip()
    params = TINY_TORCH["ml.cnn_train"]
    first = runner.execute("ml.cnn_train", params, 9, device="cpu")
    again = runner.execute("ml.cnn_train", params, 9, device="cpu")
    assert first["checksum"] == again["checksum"]
    assert "->" in first["check_detail"]


def _write_request(workdir: Path, document: Any) -> None:
    (workdir / runner.PARAMS_FILE).write_text(json.dumps(document), encoding="utf-8")


def _result(workdir: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((workdir / runner.RESULT_FILE).read_text())
    return data


def test_main_runs_and_writes_the_result(tmp_path: Path) -> None:
    _write_request(
        tmp_path, {"family": "cpu.primes", "params": TINY_CPU["cpu.primes"], "seed": 2}
    )
    assert runner.main(tmp_path) == 0
    result = _result(tmp_path)
    assert result["status"] == "ok" and result["verified"] is True
    text = (tmp_path / runner.RESULT_FILE).read_bytes()
    assert b"\r\n" not in text and text.endswith(b"\n")


@pytest.mark.parametrize(
    "document",
    [
        "not an object",
        {"params": {}, "seed": 1},
        {"family": "cpu.primes", "params": [], "seed": 1},
        {"family": "cpu.primes", "params": {}, "seed": True},
        {"family": "cpu.nope", "params": {}, "seed": 1},
        {"family": "cpu.primes", "params": {"limit": 1.5, "repeats": 1}, "seed": 1},
    ],
)
def test_main_refuses_bad_requests_with_exit_2(tmp_path: Path, document: Any) -> None:
    _write_request(tmp_path, document)
    assert runner.main(tmp_path) == runner.EXIT_BAD_PARAMS
    assert _result(tmp_path)["status"] == "bad_params"


def test_main_without_a_request_file_is_exit_2(tmp_path: Path) -> None:
    assert runner.main(tmp_path) == runner.EXIT_BAD_PARAMS
    assert _result(tmp_path)["status"] == "bad_params"


def test_main_with_invalid_json_is_exit_2(tmp_path: Path) -> None:
    (tmp_path / runner.PARAMS_FILE).write_text("{", encoding="utf-8")
    assert runner.main(tmp_path) == runner.EXIT_BAD_PARAMS


def test_main_reports_unavailable_with_exit_3(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def prepare(*_a: Any) -> None:
        raise Unavailable("no CUDA device is available on this node")

    monkeypatch.setitem(
        cpu_kernels.KERNELS,
        "cpu.sort",
        Kernel(prepare=prepare, compute=lambda _s: None, check=lambda _s, _o: None),  # type: ignore[arg-type,return-value]
    )
    _write_request(tmp_path, {"family": "cpu.sort", "params": {}, "seed": 1})
    assert runner.main(tmp_path) == runner.EXIT_UNAVAILABLE
    result = _result(tmp_path)
    assert result["status"] == "unavailable"
    assert result["reason"] == "no CUDA device is available on this node"
    assert result["compute_ms"] is None and result["verified"] is False


def test_main_reports_a_failed_check_with_exit_4(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = Kernel(
        prepare=lambda *_a: None,
        compute=lambda _s: None,
        check=lambda _s, _o: Check(
            ok=False, checksum=digest(b""), exact=True, detail="no"
        ),
    )
    monkeypatch.setitem(cpu_kernels.KERNELS, "cpu.sort", fake)
    _write_request(tmp_path, {"family": "cpu.sort", "params": {}, "seed": 1})
    assert runner.main(tmp_path) == runner.EXIT_CHECK_FAILED
    assert _result(tmp_path)["status"] == "check_failed"


def test_digest_is_unambiguous() -> None:
    assert digest(b"ab", b"c") != digest(b"a", b"bc")
