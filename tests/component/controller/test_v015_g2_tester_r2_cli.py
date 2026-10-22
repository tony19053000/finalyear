"""V0.15 G2 tester round 2: the real child on corrupt values (T1-D4 fix) -
a one-line message and exit 1/2, never exit 0, never a traceback."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.dataset import parquet_bytes, sort_key

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]

CHILD = [sys.executable, "-m", "meshforge.controller.ml.train", "compare"]
CLI = [sys.executable, "-m", "meshforge.cli", "planner", "ml", "compare"]


def _run(argv: list[str], tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env["XDG_CONFIG_HOME"] = str(tmp_path / "cfg")
    env["MESHFORGE_DATA_DIR"] = str(tmp_path / "none")
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
        env=env,
        cwd=str(tmp_path),
    )


def _write(tmp_path: Path, column: str, value: float) -> Path:
    rows: list[dict[str, Any]] = synthetic_rows(
        "signal", families=4, groups_per_family=4, runs_per_group=6
    )
    rows[5][column] = value
    path = tmp_path / "d.parquet"
    path.write_bytes(parquet_bytes(sorted(rows, key=sort_key)))
    return path


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("y", float("nan")),
        ("y", float("inf")),
        ("raw_s", -1.0),
        ("raw_s", 0.0),
        ("actual_s", float("nan")),
        ("actual_s", -2.0),
        ("load_cpu_percent", float("inf")),
    ],
)
@pytest.mark.parametrize("entry", ["child", "cli"])
def test_corrupt_values_exit_nonzero_with_one_clear_line(
    tmp_path: Path, column: str, value: float, entry: str
) -> None:
    path = _write(tmp_path, column, value)
    out = tmp_path / "o"
    base = CHILD if entry == "child" else CLI
    proc = _run(
        [
            *base,
            "--dataset",
            str(path),
            "--out",
            str(out),
            "--min-rows",
            "50",
            "--algorithms",
            "ridge",
        ],
        tmp_path,
    )
    assert proc.returncode in (1, 2), (proc.returncode, proc.stderr)
    assert "Traceback" not in proc.stderr + proc.stdout
    assert "never writes" in proc.stderr + proc.stdout
    assert not (out / "report.json").exists()
