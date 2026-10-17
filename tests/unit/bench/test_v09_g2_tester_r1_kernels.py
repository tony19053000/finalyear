"""V0.9 G2 tester round 1: do the self-checks and the timing tell the truth?

* mutation tests: corrupt a kernel's *output* in a realistic way and require the
  family's own ``check`` to notice (a check that passes wrong output makes the
  "verified" flag in every benchmark record worthless);
* timing isolation: ``compute_ns`` is ``perf_counter_ns`` around ``compute`` only,
  never the wall clock, never prepare / warm-up / check;
* the result document and ``Family.verify`` on every failure status.

Tiny parameters go straight to the kernels (the 5 s - 3 min window is a
``parse_params`` rule, not a kernel rule).
"""

from __future__ import annotations

import json
import math
import time
import zlib
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("numpy")

from meshforge.bench.workloads import WorkloadResult, get_family
from meshforge.bench.workloads.kernels import cpu, memory, runner
from meshforge.bench.workloads.kernels._support import (
    Check,
    Kernel,
    Unavailable,
)

pytestmark = pytest.mark.unit


def _run(
    mod: Any,
    family: str,
    params: Mapping[str, Any],
    mutate: Callable[[Any, Any], Any],
    seed: int = 1,
) -> Check:
    kernel = mod.KERNELS[family]
    state = kernel.prepare(params, seed, "cpu")
    if kernel.warmup:
        kernel.warmup(state)
    out = kernel.compute(state)
    check: Check = kernel.check(state, mutate(state, out))
    return check


def _identity(_state: Any, out: Any) -> Any:
    return out


# ---------------------------------------------------- unmutated runs are green


@pytest.mark.parametrize(
    ("mod", "family", "params"),
    [
        (cpu, "cpu.matmul", {"n": 96, "repeats": 2, "dtype": "float64"}),
        (cpu, "cpu.matmul", {"n": 96, "repeats": 1, "dtype": "float32"}),
        (cpu, "cpu.primes", {"limit": 200_000, "repeats": 1}),
        (cpu, "cpu.sort", {"n": 50_000, "repeats": 2}),
        (cpu, "cpu.zlib", {"size_mb": 1, "level": 9, "repeats": 1}),
        (cpu, "cpu.sha256", {"size_mb": 3, "chunk_kb": 4}),
        (cpu, "cpu.integrate", {"points_m": 1, "repeats": 1}),
        (memory, "memory.copy", {"size_mb": 2, "repeats": 2}),
        (memory, "memory.groupby", {"rows_m": 1, "groups": 50, "repeats": 1}),
    ],
)
def test_unmutated_tiny_runs_pass_their_own_check(
    mod: Any, family: str, params: Mapping[str, Any]
) -> None:
    check = _run(mod, family, params, _identity)
    assert check.ok, check.detail
    assert check.checksum.startswith("sha256:")


# ------------------------------------------------------------------ mutation


def test_matmul_float64_catches_one_zeroed_element_and_one_zeroed_row() -> None:
    params = {"n": 512, "repeats": 1, "dtype": "float64"}

    def one(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[5, 7] = 0.0
        return c

    def row(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[5, :] = 0.0
        return c

    assert not _run(cpu, "cpu.matmul", params, one).ok
    assert not _run(cpu, "cpu.matmul", params, row).ok


def test_matmul_float32_catches_a_zeroed_row_and_a_transposed_result() -> None:
    params = {"n": 512, "repeats": 1, "dtype": "float32"}

    def row(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[5, :] = 0.0
        return c

    assert not _run(cpu, "cpu.matmul", params, row).ok
    assert not _run(cpu, "cpu.matmul", params, lambda _s, c: c.T.copy()).ok
    assert not _run(cpu, "cpu.matmul", params, lambda _s, c: c * 1.01).ok


def test_matmul_float32_catches_one_zeroed_element() -> None:
    def one(_s: Any, c: Any) -> Any:
        c = c.copy()
        c[5, 7] = 0.0
        return c

    assert not _run(
        cpu, "cpu.matmul", {"n": 1024, "repeats": 1, "dtype": "float32"}, one
    ).ok


def test_sort_catches_unsorted_dropped_duplicated_and_changed_values() -> None:
    params = {"n": 20_000, "repeats": 1}
    assert not _run(cpu, "cpu.sort", params, lambda s, _o: s["values"].copy()).ok
    assert not _run(cpu, "cpu.sort", params, lambda _s, o: o[::-1].copy()).ok

    def dup(_s: Any, o: Any) -> Any:
        o = o.copy()
        o[10] = o[11]
        return o

    assert not _run(cpu, "cpu.sort", params, dup).ok
    assert not _run(cpu, "cpu.sort", params, lambda _s, o: o[:-1]).ok


def test_sort_catches_a_sum_preserving_tamper() -> None:
    def tamper(_s: Any, o: Any) -> Any:
        o = o.copy()
        o[0] -= 1
        o[1] += 1
        return o

    assert not _run(cpu, "cpu.sort", {"n": 20_000, "repeats": 1}, tamper).ok


def test_zlib_catches_a_wrong_stream_and_a_truncated_stream() -> None:
    params = {"size_mb": 1, "level": 6, "repeats": 1}
    assert not _run(
        cpu, "cpu.zlib", params, lambda s, _o: zlib.compress(b"x" * len(s["data"]))
    ).ok
    with pytest.raises(zlib.error):  # a truncated stream is an error, never "ok"
        _run(cpu, "cpu.zlib", params, lambda _s, o: o[: len(o) // 2])


def test_sha256_catches_a_wrong_digest_a_bit_flip_and_a_truncated_one() -> None:
    params = {"size_mb": 2, "chunk_kb": 64}
    assert not _run(cpu, "cpu.sha256", params, lambda _s, _o: "0" * 64).ok
    assert not _run(cpu, "cpu.sha256", params, lambda _s, o: "1" + o[1:]).ok
    assert not _run(cpu, "cpu.sha256", params, lambda _s, o: o[:-1]).ok


def test_integrate_catches_a_one_part_in_a_million_error() -> None:
    params = {"points_m": 1, "repeats": 1}
    assert not _run(cpu, "cpu.integrate", params, lambda _s, v: v * (1 + 1e-6)).ok
    assert not _run(cpu, "cpu.integrate", params, lambda _s, v: -v).ok
    assert not _run(cpu, "cpu.integrate", params, lambda _s, v: float("nan")).ok


def test_primes_catches_a_flipped_prime_at_the_known_limit() -> None:
    params = {"limit": 10_000_000, "repeats": 1}

    def flip(_s: Any, sieve: Any) -> Any:
        sieve = bytearray(sieve)
        for n in range(9_999_999, 2, -1):
            if sieve[n]:
                sieve[n] = 0
                break
        return sieve

    assert not _run(cpu, "cpu.primes", params, flip).ok


def test_primes_catches_a_flipped_prime_at_an_arbitrary_limit() -> None:
    params = {"limit": 10_000_019, "repeats": 1}

    def flip(_s: Any, sieve: Any) -> Any:
        sieve = bytearray(sieve)
        for n in range(10_000_018, 2, -1):
            if sieve[n]:
                sieve[n] = 0
                break
        return sieve

    assert not _run(cpu, "cpu.primes", params, flip).ok


def test_copy_catches_a_corrupted_destination() -> None:
    def corrupt(_s: Any, dst: Any) -> Any:
        dst = dst.copy()
        dst[len(dst) // 2] ^= 1
        return dst

    assert not _run(memory, "memory.copy", {"size_mb": 2, "repeats": 1}, corrupt).ok


def test_copy_check_detects_that_the_timed_loop_copied_nothing() -> None:
    kernel = memory.KERNELS["memory.copy"]
    state = kernel.prepare({"size_mb": 2, "repeats": 1}, 1, "cpu")
    assert kernel.warmup is not None
    kernel.warmup(state)
    # the timed loop is skipped entirely; dst still holds the warm-up's copy
    assert not kernel.check(state, state["dst"]).ok


def test_groupby_catches_wrong_counts_and_a_wrong_total() -> None:
    params = {"rows_m": 1, "groups": 100, "repeats": 1}

    def counts(_s: Any, out: Any) -> Any:
        u, c, s, m = out
        c = c.copy()
        c[0] += 1
        c[1] -= 1
        return u, c, s, m

    def total(_s: Any, out: Any) -> Any:
        u, c, s, m = out
        s = s.copy()
        s[0] += 1000.0
        return u, c, s, m

    assert not _run(memory, "memory.groupby", params, counts).ok
    assert not _run(memory, "memory.groupby", params, total).ok


@pytest.mark.parametrize("what", ["max_zeroed", "sums_swapped"])
def test_groupby_catches_wrong_max_and_swapped_group_sums(what: str) -> None:
    def mutate(_s: Any, out: Any) -> Any:
        u, c, s, m = out
        if what == "max_zeroed":
            m = m.copy()
            m[:] = 0.0
        else:
            s = s.copy()
            s[[0, 1]] = s[[1, 0]]
        return u, c, s, m

    assert not _run(
        memory, "memory.groupby", {"rows_m": 1, "groups": 100, "repeats": 1}, mutate
    ).ok


# ----------------------------------------------------------- timing isolation


def _sleeping_kernel(
    prepare_s: float, warm_s: float, compute_s: float, check_s: float, ok: bool = True
) -> Kernel:
    def prepare(_p: Mapping[str, Any], _seed: int, _d: str) -> Any:
        time.sleep(prepare_s)
        return {}

    def warmup(_s: Any) -> None:
        time.sleep(warm_s)

    def compute(_s: Any) -> Any:
        time.sleep(compute_s)
        return 1

    def check(_s: Any, _o: Any) -> Check:
        time.sleep(check_s)
        return Check(ok=ok, checksum="sha256:" + "0" * 64, exact=True, detail="fake")

    return Kernel(prepare, compute, check, warmup)


def test_compute_ns_excludes_prepare_warmup_and_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(
        cpu.KERNELS, "cpu.sort", _sleeping_kernel(0.30, 0.25, 0.05, 0.30)
    )
    doc = runner.execute("cpu.sort", {}, 1)
    assert 0.045 <= doc["compute_ns"] / 1e9 < 0.20
    assert (
        doc["prepare_ms"] >= 280 and doc["warmup_ms"] >= 230 and doc["check_ms"] >= 280
    )
    assert doc["compute_ms"] == pytest.approx(doc["compute_ns"] / 1e6, abs=1e-6)
    assert isinstance(doc["compute_ns"], int)


def test_compute_ns_does_not_use_the_wall_clock_or_time_monotonic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall clock that jumps (NTP step, manual change) must not move the number,
    and the runner must not even look at it."""
    monkeypatch.setitem(cpu.KERNELS, "cpu.sort", _sleeping_kernel(0, 0, 0.05, 0))
    calls: list[str] = []

    def jumpy() -> float:
        calls.append("time.time")
        return -1e9

    monkeypatch.setattr(time, "time", jumpy)
    doc = runner.execute("cpu.sort", {}, 1)
    assert not calls
    assert 0.045 <= doc["compute_ns"] / 1e9 < 0.20


def test_compute_is_timed_once_so_a_slow_check_cannot_inflate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def prepare(*_a: Any) -> Any:
        seen.append("prepare")
        return {}

    def warmup(_s: Any) -> None:
        seen.append("warmup")

    def compute(_s: Any) -> Any:
        seen.append("compute")
        return 0

    def check(_s: Any, _o: Any) -> Check:
        seen.append("check")
        return Check(ok=True, checksum="sha256:" + "1" * 64, exact=True)

    monkeypatch.setitem(
        cpu.KERNELS, "cpu.sort", Kernel(prepare, compute, check, warmup)
    )
    runner.execute("cpu.sort", {}, 1)
    assert seen == ["prepare", "warmup", "compute", "check"]


def test_a_raising_compute_is_not_swallowed_and_check_never_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ran: list[str] = []

    def compute(_s: Any) -> Any:
        raise MemoryError("boom")

    def check(_s: Any, _o: Any) -> Check:
        ran.append("check")
        return Check(ok=True, checksum="sha256:0", exact=True)

    monkeypatch.setitem(
        cpu.KERNELS, "cpu.sort", Kernel(lambda *_a: {}, compute, check, None)
    )
    with pytest.raises(MemoryError):
        runner.execute("cpu.sort", {}, 1)
    assert not ran


def test_real_kernel_times_are_consistent_with_a_wall_measurement() -> None:
    start = time.perf_counter_ns()
    doc = runner.execute("cpu.sort", {"n": 200_000, "repeats": 3}, 3)
    wall_ms = (time.perf_counter_ns() - start) / 1e6
    parts = doc["compute_ms"] + doc["prepare_ms"] + doc["warmup_ms"] + doc["check_ms"]
    assert 0 < doc["compute_ms"] <= parts <= wall_ms + 1


# -------------------------------------------------- statuses and the verifier


def _doc_with(monkeypatch: pytest.MonkeyPatch, **kernel: Any) -> dict[str, Any]:
    monkeypatch.setitem(cpu.KERNELS, "cpu.sort", _sleeping_kernel(0, 0, 0, 0, **kernel))
    return runner.execute("cpu.sort", {"n": 1, "repeats": 1}, 1)


def test_a_failed_check_is_status_check_failed_and_verify_rejects_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    doc = _doc_with(monkeypatch, ok=False)
    assert doc["status"] == "check_failed" and doc["verified"] is False
    assert doc["reason"] == "fake"
    result = WorkloadResult.model_validate(doc)
    verdict = get_family("cpu.sort").verify(result)
    assert not verdict.ok
    assert any("check_failed" in p for p in verdict.problems)


def test_verify_rejects_each_kind_of_bad_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    family = get_family("cpu.sort")
    good = _doc_with(monkeypatch)
    good["params"] = family.params_dict({})
    assert family.verify(WorkloadResult.model_validate(good)).ok

    def bad(**override: Any) -> WorkloadResult:
        return WorkloadResult.model_validate({**good, **override})

    cases = {
        "schema": bad(schema_version=2),
        "family": bad(family="cpu.zlib"),
        "params": bad(params={"n": "lots"}),
        "status": bad(status="check_failed", reason="x"),
        "unverified": bad(verified=False),
        "zero time": bad(compute_ns=0),
        "no time": bad(compute_ns=None),
        "no checksum": bad(checksum=None),
        "weak checksum": bad(checksum="md5:abc"),
        "unavailable": bad(status="unavailable", verified=False, reason="no cuda"),
    }
    for label, result in cases.items():
        assert not family.verify(result).ok, label


def test_workload_result_rejects_negative_times_and_unknown_status() -> None:
    base = {
        "schema_version": 1,
        "family": "cpu.sort",
        "params": {},
        "seed": 1,
        "status": "ok",
        "verified": True,
    }
    WorkloadResult.model_validate(base)
    for extra in ({"compute_ns": -1}, {"compute_ms": -0.1}, {"status": "weird"}):
        with pytest.raises(ValueError):
            WorkloadResult.model_validate({**base, **extra})


def test_missing_numpy_is_a_typed_unavailable_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sys

    monkeypatch.setitem(sys.modules, "numpy", None)  # `import numpy` -> ImportError
    with pytest.raises(Unavailable, match="numpy"):
        runner.execute("cpu.sort", {"n": 1000, "repeats": 1}, 1)


# --------------------------------------------------------- the runner, in-proc


def _write_request(workdir: Path, doc: Any) -> None:
    (workdir / runner.PARAMS_FILE).write_text(json.dumps(doc), encoding="utf-8")


def _result(workdir: Path) -> dict[str, Any]:
    doc: dict[str, Any] = json.loads(
        (workdir / runner.RESULT_FILE).read_text(encoding="utf-8")
    )
    return doc


def test_main_exit_codes_and_result_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 0 ok
    ok_dir = tmp_path / "ok"
    ok_dir.mkdir()
    _write_request(
        ok_dir, {"family": "cpu.sort", "params": {"n": 5000, "repeats": 1}, "seed": 2}
    )
    assert runner.main(ok_dir) == 0
    doc = _result(ok_dir)
    assert doc["status"] == "ok" and doc["verified"] is True and doc["compute_ns"] > 0
    assert math.isfinite(doc["compute_ms"])
    # 2 bad params (several flavours), each leaves a parseable status file
    for label, body in {
        "unknown": {"family": "nope", "params": {}, "seed": 1},
        "type": {"family": "cpu.sort", "params": {"n": "5", "repeats": 1}, "seed": 1},
        "bool": {"family": "cpu.sort", "params": {"n": True, "repeats": 1}, "seed": 1},
        "missing key": {"family": "cpu.sort", "params": {"repeats": 1}, "seed": 1},
        "no params": {"family": "cpu.sort", "seed": 1},
        "float seed": {
            "family": "cpu.sort",
            "params": {"n": 5, "repeats": 1},
            "seed": 1.5,
        },
        "bool seed": {
            "family": "cpu.sort",
            "params": {"n": 5, "repeats": 1},
            "seed": True,
        },
    }.items():
        d = tmp_path / label.replace(" ", "_")
        d.mkdir()
        _write_request(d, body)
        assert runner.main(d) == runner.EXIT_BAD_PARAMS, label
        assert _result(d)["status"] == "bad_params", label
        assert _result(d)["verified"] is False and _result(d)["compute_ns"] is None
    # 3 unavailable
    monkeypatch.setitem(
        cpu.KERNELS,
        "cpu.sort",
        Kernel(
            lambda *_a: (_ for _ in ()).throw(Unavailable("no such device")),
            lambda s: s,
            lambda s, o: Check(True, "sha256:0", True),
        ),
    )
    un = tmp_path / "un"
    un.mkdir()
    _write_request(
        un, {"family": "cpu.sort", "params": {"n": 5, "repeats": 1}, "seed": 1}
    )
    assert runner.main(un) == runner.EXIT_UNAVAILABLE
    assert (
        _result(un)["status"] == "unavailable"
        and _result(un)["reason"] == "no such device"
    )
    # 4 check failed
    monkeypatch.setitem(cpu.KERNELS, "cpu.sort", _sleeping_kernel(0, 0, 0, 0, ok=False))
    bad = tmp_path / "bad"
    bad.mkdir()
    _write_request(
        bad, {"family": "cpu.sort", "params": {"n": 5, "repeats": 1}, "seed": 1}
    )
    assert runner.main(bad) == runner.EXIT_CHECK_FAILED
    assert _result(bad)["status"] == "check_failed"


def test_main_survives_an_unwritable_results_location(tmp_path: Path) -> None:
    d = tmp_path / "ro"
    d.mkdir()
    _write_request(
        d, {"family": "cpu.sort", "params": {"n": 5000, "repeats": 1}, "seed": 2}
    )
    (d / "results").write_text("i am a file, not a folder")
    with pytest.raises(
        OSError
    ):  # the failure is loud (job fails), never a silent success
        runner.main(d)


def test_result_json_is_strict_json_with_sorted_keys(tmp_path: Path) -> None:
    d = tmp_path / "j"
    d.mkdir()
    _write_request(
        d, {"family": "cpu.sort", "params": {"n": 5000, "repeats": 1}, "seed": 2}
    )
    runner.main(d)
    text = (d / runner.RESULT_FILE).read_text(encoding="utf-8")
    assert "NaN" not in text and "Infinity" not in text
    assert "\r" not in text and text.endswith("\n")
    parsed = json.loads(
        text, parse_constant=lambda c: pytest.fail(f"non-JSON constant {c}")
    )
    assert list(parsed) == sorted(parsed)
    assert set(parsed["libraries"]) >= {
        "python",
        "numpy",
        "torch",
        "cuda",
        "gpu",
        "zlib",
    }
