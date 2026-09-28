"""V0.4 G5 0.4.14: the pytorch-train example is reproducible on the CPU, and
``compare.py`` says IDENTICAL only for identical bytes (never for a tolerance
match). Skips without torch (a plain `uv sync` has none)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("torch", reason="needs torch (uv sync --extra gpu)")

EXAMPLE = Path(__file__).resolve().parents[3] / "workloads/examples/pytorch-train"


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *args], cwd=cwd, capture_output=True, text=True, timeout=120
    )


def test_two_cpu_runs_are_byte_identical_and_compare_says_so(tmp_path: Path) -> None:
    for out in ("a", "b"):
        done = _run(
            str(EXAMPLE / "main.py"),
            "--steps",
            "60",
            "--device",
            "cpu",
            "--out",
            out,
            cwd=tmp_path,
        )
        assert done.returncode == 0, done.stderr
    for name in ("metrics.json", "weights.bin"):
        assert (tmp_path / "a" / name).read_bytes() == (
            tmp_path / "b" / name
        ).read_bytes()
    same = _run(str(EXAMPLE / "compare.py"), "a", "b", cwd=tmp_path)
    assert same.returncode == 0 and same.stdout.startswith("IDENTICAL")
    # nudge one weight: within tolerance, never "identical"
    weights = bytearray((tmp_path / "b/weights.bin").read_bytes())
    weights[0] ^= 0x01  # the lowest mantissa bit of the first float
    (tmp_path / "b/weights.bin").write_bytes(bytes(weights))
    close = _run(str(EXAMPLE / "compare.py"), "a", "b", cwd=tmp_path)
    assert close.returncode == 0 and close.stdout.startswith("WITHIN TOLERANCE")
    other = _run(
        str(EXAMPLE / "main.py"),
        "--steps",
        "60",
        "--seed",
        "8",
        "--device",
        "cpu",
        "--out",
        "c",
        cwd=tmp_path,
    )
    assert other.returncode == 0
    far = _run(str(EXAMPLE / "compare.py"), "a", "c", cwd=tmp_path)
    assert far.returncode == 1 and far.stdout.startswith("DIFFERENT")
