"""V0.15 G2 performance bound (brief §5b): ``compare`` on a 20 000-row
synthetic dataset (8 terms, 15 families) finishes in < 15 min single
threaded; a harness-sized dataset (4 terms, 1 200 rows - every term above
the D348 floor) in < 2 min.

Both run the real child process (``python -m meshforge.controller.ml.train
compare``) on a real Parquet file; the 20 000-row case is ``slow`` (minutes:
``--run-slow``). The measured times are in docs/briefs/progress/V0.15-G2.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")

from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml.schema import TERM_NAMES

pytestmark = [pytest.mark.component]


def _compare(dataset: Path, out: Path, limit_s: float) -> dict[str, float]:
    started = time.monotonic()
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "compare",
            "--dataset",
            str(dataset),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        timeout=limit_s + 60,
        check=False,
    )
    wall = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    timings: dict[str, float] = json.loads((out / "timings.json").read_text())
    print(f"compare: {timings} wall {wall:.1f} s")  # noqa: T201 - the recorded number
    assert wall < limit_s, timings
    return timings


@pytest.mark.timeout(240)
def test_a_harness_sized_dataset_in_under_two_minutes(tmp_path: Path) -> None:
    rows = synthetic_rows(
        "signal",
        families=5,
        groups_per_family=6,
        runs_per_group=10,
        terms=("transfer", "env", "compute", "output"),
    )
    assert len(rows) == 1200
    timings = _compare(parquet_file(rows, tmp_path / "h.parquet"), tmp_path / "o", 120)
    assert timings["rows"] == 1200


@pytest.mark.slow
@pytest.mark.timeout(1200)
def test_twenty_thousand_rows_in_under_fifteen_minutes(tmp_path: Path) -> None:
    rows = synthetic_rows(
        "signal",
        families=15,
        groups_per_family=10,
        runs_per_group=17,
        terms=TERM_NAMES,
    )
    assert len(rows) == 20_400
    _compare(parquet_file(rows, tmp_path / "big.parquet"), tmp_path / "o", 900)
