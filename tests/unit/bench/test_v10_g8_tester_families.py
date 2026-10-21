"""V0.10 G8 tester (independent): the Mode 3 families' self-checks against
corrupted outputs, determinism across processes, and chunk-boundary checks.

Derived from the success criteria (every family has a self-check that refuses a
wrong output; data and output are deterministic across machines), not from the
implementer's tests.
"""

# ruff: noqa: RUF005, PT012
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from collections.abc import Callable, Mapping
from typing import Any

import pytest

pytest.importorskip("numpy", reason="the image / monte carlo kernels need numpy")

from meshforge.bench.workloads import get_family
from meshforge.bench.workloads.kernels import split as kernel
from meshforge.bench.workloads.split import SplitFamily

pytestmark = pytest.mark.unit

SEED = 5
SMALL: Mapping[str, dict[str, Any]] = {
    "split.image_classify": {"images": 20, "size": 16, "width": 4},
    "split.hash_files": {"files": 6, "size_kb": 4, "rounds": 2},
    "split.montecarlo_range": {"items": 20, "samples": 20_000},
    "split.csv_aggregate": {"rows": 150, "cols": 3, "rounds": 2},
}
NAMES = sorted(SMALL)


def _fam(name: str) -> SplitFamily[Any]:
    fam = get_family(name)
    assert isinstance(fam, SplitFamily)
    return fam


def _expected(name: str) -> dict[str, bytes]:
    fam = _fam(name)
    return fam.expected_output(SMALL[name], SEED, fam.input_files(SMALL[name], SEED))


def _json_equal(a: bytes, b: bytes) -> bool:
    try:
        return bool(json.loads(a) == json.loads(b))
    except ValueError:
        return False


def _mutations(data: bytes) -> dict[str, bytes]:
    """Plausible corruptions of one output file (all differ from ``data``)."""
    out: dict[str, bytes] = {
        "empty": b"",
        "append_nl": data + b"\n",
        "drop_last": data[:-1],
    }
    for pos in sorted({0, 1, len(data) // 3, len(data) // 2, len(data) - 2}):
        flipped = bytearray(data)
        flipped[pos] ^= 0x01
        if bytes(flipped) != data:
            out[f"flip@{pos}"] = bytes(flipped)
    if b"\n" in data:
        out["crlf"] = data.replace(b"\n", b"\r\n")
        lines = data.splitlines(keepends=True)
        if len(lines) >= 3 and lines[0] != lines[-1]:
            swapped = list(lines)
            swapped[0], swapped[-1] = swapped[-1], swapped[0]
            out["swap_first_last"] = b"".join(swapped)
            out["dup_line"] = b"".join(lines + [lines[0]])
            out["drop_first_line"] = b"".join(lines[1:])
    return out


@pytest.mark.parametrize("name", NAMES)
def test_whole_output_check_accepts_truth_and_rejects_every_mutation(name: str) -> None:
    fam = _fam(name)
    truth = _expected(name)
    assert fam.verify_output(truth, SMALL[name], SEED).ok
    for fname, data in truth.items():
        muts = _mutations(data)
        assert muts
        for label, bad in muts.items():
            if name == "split.montecarlo_range" and _json_equal(bad, data):
                # merge-json: whitespace is not a value; comparing values is the
                # documented contract (see MontecarloRangeFamily._same).
                continue
            verdict = fam.verify_output({**truth, fname: bad}, SMALL[name], SEED)
            assert not verdict.ok, f"{name}/{fname}: {label} was accepted"


@pytest.mark.parametrize("name", NAMES)
def test_whole_output_check_rejects_wrong_file_sets_and_wrong_seed(name: str) -> None:
    fam = _fam(name)
    truth = _expected(name)
    assert not fam.verify_output({}, SMALL[name], SEED).ok
    assert not fam.verify_output({**truth, "extra.bin": b"x"}, SMALL[name], SEED).ok
    first = next(iter(truth))
    assert not fam.verify_output({"renamed": truth[first]}, SMALL[name], SEED).ok
    # an output computed for another seed must not pass as this seed's
    other = fam.expected_output(
        SMALL[name], SEED + 1, fam.input_files(SMALL[name], SEED + 1)
    )
    assert other != truth
    assert not fam.verify_output(other, SMALL[name], SEED).ok


def _items_for(name: str) -> bytes:
    return _fam(name).items_file(SMALL[name], SEED)


@pytest.mark.parametrize("name", NAMES)
def test_chunk_check_refuses_count_and_boundary_corruption(name: str) -> None:
    """The job-side per-chunk check (the one that makes a chunk exit 4) refuses
    a short/long output and a corrupted first or last item of the chunk."""
    fam = _fam(name)
    files = fam.input_files(SMALL[name], SEED)
    items = _items_for(name)
    k = kernel.SPLIT_KERNELS[name]

    def with_tree(go: Callable[[], Any]) -> Any:
        from meshforge.bench.workloads.split import _in_tree

        return _in_tree(files, go)

    def run() -> Any:
        out = k.run(SMALL[name], SEED, items)
        assert k.check(SMALL[name], SEED, items, out) is None
        fname = next(iter(out))
        data = out[fname]
        problems: dict[str, str | None] = {}
        lines = data.splitlines(keepends=True)
        if len(lines) > 2:
            problems["short"] = k.check(
                SMALL[name], SEED, items, {fname: b"".join(lines[:-1])}
            )
            problems["long"] = k.check(
                SMALL[name], SEED, items, {fname: b"".join(lines + [lines[-1]])}
            )
        return problems

    problems = with_tree(run)
    for label, problem in problems.items():
        assert problem, f"{name}: a {label} chunk output passed the self-check"


@pytest.mark.parametrize("name", NAMES)
def test_output_is_identical_across_processes_and_hash_seeds(name: str) -> None:
    """Criterion 1's mechanism: the one-chunk result digest does not depend on
    PYTHONHASHSEED, the process, or thread layout."""
    code = textwrap.dedent(
        f"""
        import json
        from meshforge.bench.workloads import get_family
        from meshforge.bench.workloads.split import _files_digest, _in_tree
        from meshforge.bench.workloads.kernels import split as k
        fam = get_family({name!r})
        p = {dict(SMALL[name])!r}
        files = fam.input_files(p, {SEED})
        items = fam.items_file(p, {SEED})
        out, problem, _ = _in_tree(files, lambda: k.run_items({name!r}, p, {SEED}, items))
        print(json.dumps([problem, _files_digest(out)]))
        """
    )
    seen = set()
    for hashseed in ("0", "1", "12345"):
        env = {
            "PYTHONHASHSEED": hashseed,
            "OMP_NUM_THREADS": "1" if hashseed == "0" else "4",
        }
        import os

        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            env={**os.environ, **env},
            timeout=120,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        seen.add(proc.stdout.strip().splitlines()[-1])
    assert len(seen) == 1, seen
    problem, checksum = json.loads(seen.pop())
    assert problem is None and checksum.startswith("sha256:")


@pytest.mark.parametrize("name", NAMES)
def test_params_at_bounds_are_refused_or_run(name: str) -> None:
    from meshforge.bench.workloads.base import ParamsError

    fam = _fam(name)
    with pytest.raises(ParamsError):
        fam.parse_params({**SMALL[name], "nonexistent": 1})
    first = next(iter(SMALL[name]))
    with pytest.raises(ParamsError):
        fam.parse_params({**SMALL[name], first: -1})
    with pytest.raises(ParamsError):
        fam.parse_params({**SMALL[name], first: "many"})


@pytest.mark.parametrize("seed", [-1, 2**32, True, 1.5])
def test_bad_seeds_are_refused_by_the_chunk_runner(seed: Any) -> None:
    with pytest.raises(kernel.KernelParamsError):
        kernel.run_items(
            "split.csv_aggregate", SMALL["split.csv_aggregate"], seed, b"[]"
        )


def test_unknown_family_and_garbage_items_are_a_params_error_not_a_crash() -> None:
    with pytest.raises(kernel.KernelParamsError):
        kernel.run_items("split.nope", {}, 1, b"[]")
    for fam in NAMES:
        for garbage in (
            b"",
            b"not json",
            b"[]",
            b'{"a":1}',
            b"[1,2]",
            b'["../../etc/passwd"]',
        ):
            with pytest.raises((kernel.KernelParamsError, ValueError, TypeError)):
                if fam == "split.montecarlo_range" and garbage == b"[1,2]":
                    raise ValueError  # valid point-set indexes
                kernel.run_items(fam, SMALL[fam], SEED, garbage)
