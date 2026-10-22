"""V0.15 G2 (D353): ``python -m meshforge.controller.ml.train compare`` exit
codes - 2 for a missing ``ml`` extra (monkeypatched: the check runs before
any ML import), an unknown dataset version, an unknown term or algorithm or
bad arguments; 1 for an unreadable file; 0 with the three files written."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml import train as train_pkg
from meshforge.controller.ml.dataset import FEATURE_METADATA_KEY
from meshforge.controller.ml.train import __main__ as train_main

pytestmark = pytest.mark.unit


def _args(dataset: Path, out: Path, *extra: str) -> list[str]:
    return ["compare", "--dataset", str(dataset), "--out", str(out), *extra]


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    rows = synthetic_rows("signal", families=3, groups_per_family=2, runs_per_group=4)
    return parquet_file(rows, tmp_path / "d.parquet")


def test_missing_extra_is_exit_2_before_any_ml_import(
    dataset: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    real = train_pkg.importlib.util.find_spec
    monkeypatch.setattr(
        train_pkg.importlib.util,
        "find_spec",
        lambda name, *a: None if name == "lightgbm" else real(name, *a),
    )
    assert train_main.main(_args(dataset, tmp_path / "o")) == 2
    err = capsys.readouterr().err
    assert "lightgbm" in err and "--extra ml" in err
    assert not (tmp_path / "o").exists()


def test_an_unknown_feature_version_is_exit_2(
    dataset: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("sklearn", reason="needs the ml extra")
    table = pq.read_table(dataset)
    meta = dict(table.schema.metadata)
    meta[FEATURE_METADATA_KEY] = b"7"
    other = tmp_path / "v7.parquet"
    pq.write_table(table.replace_schema_metadata(meta), other)
    assert train_main.main(_args(other, tmp_path / "o")) == 2
    assert "feature_version 7" in capsys.readouterr().err


def test_an_unreadable_file_is_exit_1(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("sklearn", reason="needs the ml extra")
    bad = tmp_path / "bad.parquet"
    bad.write_bytes(b"not parquet")
    assert train_main.main(_args(bad, tmp_path / "o")) == 1
    assert "cannot read the dataset" in capsys.readouterr().err


@pytest.mark.parametrize(
    "extra", [("--terms", "compute,nope"), ("--algorithms", "ridge,catboost")]
)
def test_unknown_terms_or_algorithms_are_exit_2(
    dataset: Path, tmp_path: Path, extra: tuple[str, str]
) -> None:
    pytest.importorskip("sklearn", reason="needs the ml extra")
    assert train_main.main(_args(dataset, tmp_path / "o", *extra)) == 2


def test_bad_arguments_are_exit_2() -> None:
    with pytest.raises(SystemExit) as exc:
        train_main.main(["compare", "--dataset", "x"])  # no --out
    assert exc.value.code == 2


def test_success_writes_three_files_and_one_json_line(
    dataset: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("sklearn", reason="needs the ml extra")
    out = tmp_path / "o"
    code = train_main.main(
        _args(dataset, out, "--min-rows", "10", "--algorithms", "ridge")
    )
    assert code == 0
    line = json.loads(capsys.readouterr().out)
    assert line["report"] == str(out / "report.json")
    assert {p.name for p in out.iterdir()} == {
        "report.json",
        "report.md",
        "timings.json",
    }
    doc = json.loads((out / "report.json").read_text())
    assert doc["settings"]["min_rows"] == 10
    assert doc["settings"]["algorithms"] == ["ridge"]
