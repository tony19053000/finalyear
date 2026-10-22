"""V0.15 G2 (D353): ``compute planner ml compare`` - the CLI side.

The CLI never imports the controller (import-linter): it checks the extras
with ``find_spec``, runs ``python -m meshforge.controller.ml.train compare``
with this interpreter and prints the report it wrote. Here the child process
is faked (``subprocess.run``); the real child runs in
``tests/component/controller/test_v015_g2_ml_compare_pg.py``.

* a missing library -> exit 2 with the install hint, no child started;
* a missing dataset file -> exit 2;
* the child's argv (flags passed through) and its exit codes (2 -> 2,
  anything else -> 1, its stderr shown);
* the table, the header lines and ``--json``; ``--out`` keeps the report,
  without it a temporary directory is used and removed.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from meshforge.cli import planner_commands as pc
from meshforge.cli.app import app

pytestmark = pytest.mark.unit

REPORT: dict[str, Any] = {
    "report_version": 1,
    "dataset": {
        "digest": "sha256:" + "ab" * 32,
        "dataset_version": 1,
        "feature_version": 1,
        "rows": 600,
        "sidecar": "matched",
    },
    "failure_classifier": {
        "node_failures": 3,
        "min_events": 50,
        "status": "not_trained",
    },
    "terms": [
        {
            "term": "compute",
            "status": "trained",
            "rows": 400,
            "families": 5,
            "shipped": {"config": {"name": "lightgbm(n_estimators=200,num_leaves=7)"}},
            "pooled": {
                "male_model": 0.081,
                "male_calibration": 0.2,
                "coverage": 0.9,
                "usable_rate": 0.75,
            },
            "vs_calibration": {"verdict": "better"},
        },
        {"term": "env", "status": "insufficient_data", "rows": 12, "families": 1},
    ],
}


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> CliRunner:
    monkeypatch.setenv("COLUMNS", "200")  # one table row per term
    return CliRunner()


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    path = tmp_path / "d.parquet"
    path.write_bytes(b"PAR1")
    return path


class _Child:
    def __init__(self, code: int = 0, stderr: str = "") -> None:
        self.code = code
        self.stderr = stderr
        self.argv: list[str] = []

    def __call__(self, argv: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        self.argv = argv
        assert kw.get("check") is False and kw.get("capture_output") is True
        if self.code == 0:
            out = Path(argv[argv.index("--out") + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / "report.json").write_text(json.dumps(REPORT))
            (out / "report.md").write_text("# ML comparison report\n")
            (out / "timings.json").write_text(json.dumps({"total_s": 1.5, "rows": 600}))
        return subprocess.CompletedProcess(argv, self.code, "", self.stderr)


@pytest.fixture
def child(monkeypatch: pytest.MonkeyPatch) -> _Child:
    fake = _Child()
    monkeypatch.setattr("meshforge.cli.planner_commands.subprocess.run", fake)
    monkeypatch.setattr(pc, "ml_missing_libraries", list)
    return fake


def test_missing_extra_is_exit_2_with_the_hint_and_no_child(
    runner: CliRunner, dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started: list[Any] = []
    monkeypatch.setattr(
        "meshforge.cli.planner_commands.subprocess.run",
        lambda *a, **k: started.append(a),
    )
    real = importlib.util.find_spec
    monkeypatch.setattr(
        "importlib.util.find_spec",
        lambda name, *a: None if name in ("sklearn", "xgboost") else real(name, *a),
    )
    result = runner.invoke(app, ["planner", "ml", "compare", "--dataset", str(dataset)])
    assert result.exit_code == 2
    assert "sklearn, xgboost" in result.output and "--extra ml" in result.output
    assert started == []


def test_a_missing_dataset_is_exit_2(
    runner: CliRunner, child: _Child, tmp_path: Path
) -> None:
    result = runner.invoke(
        app, ["planner", "ml", "compare", "--dataset", str(tmp_path / "none.parquet")]
    )
    assert result.exit_code == 2 and "no such dataset" in result.output
    assert child.argv == []


def test_the_child_gets_this_interpreter_and_every_flag(
    runner: CliRunner, child: _Child, dataset: Path, tmp_path: Path
) -> None:
    out = tmp_path / "rep"
    result = runner.invoke(
        app,
        [
            "planner", "ml", "compare", "--dataset", str(dataset), "--terms", "compute,env",
            "--algorithms", "ridge", "--with-family", "--min-rows", "50",
            "--min-families", "2", "--min-effect", "0.05", "--out", str(out),
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert child.argv[:4] == [
        sys.executable,
        "-m",
        "meshforge.controller.ml.train",
        "compare",
    ]
    argv = child.argv
    assert argv[argv.index("--dataset") + 1] == str(dataset)
    assert argv[argv.index("--out") + 1] == str(out)
    assert argv[argv.index("--terms") + 1] == "compute,env"
    assert argv[argv.index("--algorithms") + 1] == "ridge"
    assert argv[argv.index("--min-rows") + 1] == "50"
    assert argv[argv.index("--min-families") + 1] == "2"
    assert argv[argv.index("--min-effect") + 1] == "0.05"
    assert "--with-family" in argv
    assert (out / "report.json").exists()  # --out keeps it
    assert f"wrote {out / 'report.json'}" in result.output
    for text in ("compute", "trained", "lightgbm", "better", "90.0%", "75.0%", "0.081"):
        assert text in result.output
    assert "failure classifier: not trained, 3 < 50 events" in result.output
    assert "insufficient data" in result.output  # words wrap, not_x -> not x


def test_without_out_the_report_lives_in_a_removed_temporary_directory(
    runner: CliRunner, child: _Child, dataset: Path
) -> None:
    result = runner.invoke(app, ["planner", "ml", "compare", "--dataset", str(dataset)])
    assert result.exit_code == 0, result.output
    scratch = Path(child.argv[child.argv.index("--out") + 1])
    assert not scratch.exists()
    assert "wrote" not in result.output


def test_json_prints_the_report(
    runner: CliRunner, child: _Child, dataset: Path
) -> None:
    result = runner.invoke(
        app, ["planner", "ml", "compare", "--dataset", str(dataset), "--json"]
    )
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["terms"][0]["vs_calibration"]["verdict"] == "better"
    assert "schema_version" in doc


@pytest.mark.parametrize(("code", "expected"), [(2, 2), (1, 1), (7, 1)])
def test_child_failures_map_to_exit_codes(
    runner: CliRunner, child: _Child, dataset: Path, code: int, expected: int
) -> None:
    child.code = code
    child.stderr = (
        "d.parquet: dataset_version 99 / feature_version 1 is not one this code knows"
    )
    result = runner.invoke(app, ["planner", "ml", "compare", "--dataset", str(dataset)])
    assert result.exit_code == expected
    assert "dataset_version 99" in result.output


def test_the_table_survives_a_malformed_report() -> None:
    table = pc.ml_compare_table({"terms": [None, {"term": "x", "pooled": "bad"}]})
    assert table.row_count == 1
    assert pc.ml_compare_lines({})[-1].startswith("held-out")
