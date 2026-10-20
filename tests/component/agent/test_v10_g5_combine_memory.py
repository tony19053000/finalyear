"""V0.10 G5 (D283 pin 5): the built-in combines stream - a huge CSV merge runs
in bounded memory, as the separate process the agent starts
(``python -m meshforge.agent.execution.combine``).

The process's peak resident memory (``ru_maxrss``, Linux) over 3 x 64 MiB of
CSV parts must stay within a small margin of the same process over a tiny
input: whole-file reads would need >= 64 MiB more.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.skipif(
        not sys.platform.startswith("linux"), reason="ru_maxrss in KiB is Linux's"
    ),
]

PART_BYTES = 64 * 1024 * 1024
MARGIN_BYTES = 24 * 1024 * 1024
HEADER = b"id,label,score\n"

_WRAPPER = """
import resource, sys
from meshforge.agent.execution.combine import main
code = main(sys.argv[1:])
print("MAXRSS_KIB", resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
sys.exit(code)
"""


def _parts(root: Path, sizes: list[int]) -> tuple[Path, bytes]:
    """Parts with one ``big.csv`` each; returns the parts folder and the
    SHA-256 of the expected merged file (header once, rows in chunk order)."""
    base = root / "parts"
    base.mkdir()
    digest = hashlib.sha256(HEADER)
    for index, size in enumerate(sizes):
        part = base / f"{index:05d}"
        part.mkdir()
        row = f"{index},label-{index},0.5\n".encode()
        block = row * (1024 * 1024 // len(row) + 1)
        with (part / "big.csv").open("wb") as fh:
            fh.write(HEADER)
            left = size
            while left > 0:
                piece = block[: min(len(block), left)]
                piece = piece[: len(piece) - (len(piece) % len(row))] or row
                fh.write(piece)
                digest.update(piece)
                left -= len(piece)
    return base, digest.digest()


def _run(root: Path, parts: Path) -> tuple[int, int]:
    out = root / "out"
    out.mkdir()
    result = root / "result.json"
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            _WRAPPER,
            "merge-csv",
            str(parts),
            str(out),
            str(result),
            "--max-output-bytes",
            str(1 << 40),
            "--json-max-bytes",
            str(1 << 20),
        ],
        capture_output=True,
        check=False,
        timeout=240,
    )
    assert done.returncode == 0, done.stderr.decode(errors="replace")[-2000:]
    assert json.loads(result.read_text())["ok"] is True
    line = [ln for ln in done.stdout.decode().splitlines() if ln.startswith("MAXRSS")]
    return int(line[-1].split()[1]) * 1024, (out / "big.csv").stat().st_size


def test_huge_csv_merge_runs_in_bounded_memory(tmp_path: Path) -> None:
    small = tmp_path / "small"
    small.mkdir()
    parts, _ = _parts(small, [1024, 1024, 1024])
    baseline, _ = _run(small, parts)

    big = tmp_path / "big"
    big.mkdir()
    parts, expected = _parts(big, [PART_BYTES] * 3)
    peak, size = _run(big, parts)
    assert size >= 3 * PART_BYTES - 3 * 64  # every row is there, header once
    with (big / "out" / "big.csv").open("rb") as fh:
        got = hashlib.sha256()
        while block := fh.read(1 << 20):
            got.update(block)
    assert got.digest() == expected
    assert peak - baseline < MARGIN_BYTES, (peak, baseline)
