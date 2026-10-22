"""V0.15 G2: the nested comparison on synthetic truths, edge cases and
determinism (D337, D338, D347, D348, D351, D352; brief §5b tests).

Synthetic truths (``mf_ml_synthetic``, real G1-schema rows):

(i) the residual is a known function of load and hardware -> a learned model
    is selected and ``better`` than calibration on held-out families;
(ii) the residual is a per-node constant plus noise -> never ``better``
    (calibration's node factor is the truth);
(iii) a held-out family with jobs ~1000x larger than the rest -> mostly OOD,
    mostly not usable.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml.train import __main__ as train_main
from meshforge.controller.ml.train.compare import (
    CompareSettings,
    TermResult,
    compare,
    compare_term,
)
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.report import canonical_json, markdown, report

pytestmark = pytest.mark.unit

SMALL = CompareSettings(terms=("compute",), min_rows=50)


def _run(rows: list[dict[str, Any]], settings: CompareSettings = SMALL) -> TermResult:
    return compare_term(table_from_rows(rows), "compute", settings)


@pytest.fixture(scope="module")
def truth_i() -> TermResult:
    return _run(
        synthetic_rows("signal", families=6, groups_per_family=5, runs_per_group=10)
    )


def test_truth_i_a_learned_model_wins_on_held_out_families(truth_i: TermResult) -> None:
    assert truth_i.status == "trained"
    assert truth_i.final is not None and truth_i.final.selected.learned
    assert all(f.selected.learned for f in truth_i.folds)
    assert truth_i.verdict["verdict"] == "better"
    assert truth_i.verdict["high"] < 0.0
    assert truth_i.pooled["male_model"] < 0.5 * truth_i.pooled["male_calibration"]
    assert not truth_i.would_reject["any"]
    # what drives the residual is what the importance finds
    top = {name for name, _ in truth_i.importance[:2]}
    assert "load_cpu_percent" in top


def test_truth_i_reports_both_interval_methods(truth_i: TermResult) -> None:
    assert truth_i.quantile is not None
    assert 0.0 <= truth_i.quantile["coverage"] <= 1.0
    assert truth_i.pooled["usable_rate"] > 0.8
    assert truth_i.final_q is not None and math.isfinite(truth_i.final_q)


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_truth_ii_node_noise_is_never_better_than_calibration(seed: int) -> None:
    result = _run(
        synthetic_rows(
            "node_noise",
            families=6,
            groups_per_family=5,
            runs_per_group=10,
            noise=0.3,
            seed=seed,
        )
    )
    assert result.verdict["verdict"] != "better", result.verdict
    if result.status == "trained":  # a learned pick must at least not separate
        assert result.verdict["verdict"] in ("not_separable", "worse")


@pytest.mark.parametrize("seed", [0, 1])
def test_pure_noise_with_enough_families_ships_no_learned_model(seed: int) -> None:
    """D357 at the compare level: y independent of every feature, 6 families
    (the family floor is met) -> a baseline ships, so the verdict is
    `no_learned_model` - never `better`, never a bare `not_separable`."""
    import random

    rows = synthetic_rows(
        "node_noise", families=6, groups_per_family=5, runs_per_group=10, seed=seed
    )
    rng = random.Random(seed + 1000)  # noqa: S311 - test data
    for row in rows:
        row["y"] = rng.gauss(0.0, 0.3)
        row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    result = _run(rows)
    assert result.final is not None and not result.final.selected.learned
    assert result.verdict["verdict"] == "no_learned_model"
    assert result.verdict["reason"] == "no_learned_model_ships"
    assert result.verdict["families"] == 6  # the bootstrap numbers stay


def test_truth_iii_a_far_family_is_out_of_distribution() -> None:
    result = _run(
        synthetic_rows(
            "signal",
            families=5,
            groups_per_family=5,
            runs_per_group=8,
            far_family=True,
        )
    )
    far = result.per_family["far"]
    assert far["ood_rate"] > 0.9
    assert far["usable_rate"] < 0.1
    near = [v for k, v in result.per_family.items() if k != "far"]
    assert all(part["ood_rate"] < 0.5 for part in near)
    assert result.pooled["ood_by_reason"].get("range", 0) >= far["rows"]


def test_conformal_coverage_on_a_large_truth_i_set() -> None:
    result = _run(
        synthetic_rows("signal", families=8, groups_per_family=6, runs_per_group=25),
        CompareSettings(terms=("compute",), algorithms=("ridge", "lightgbm")),
    )
    assert result.pooled["rows"] == 1200
    assert 0.85 <= result.pooled["coverage"] <= 0.95, result.pooled


# ------------------------------------------------------------------ edges


def test_the_floor_gives_insufficient_data() -> None:
    rows = synthetic_rows("signal", families=6, groups_per_family=2, runs_per_group=5)
    result = compare_term(table_from_rows(rows), "compute", CompareSettings())
    assert result.status == "insufficient_data"  # 60 rows < 200
    assert result.rows == 60 and result.families == 6
    two = synthetic_rows("signal", families=2, groups_per_family=20, runs_per_group=10)
    assert compare_term(table_from_rows(two), "compute", CompareSettings()).status == (
        "insufficient_data"  # 400 rows, but 2 families < 3
    )


def test_an_empty_dataset_reports_every_term_insufficient() -> None:
    table = table_from_rows([])
    results = compare(table, CompareSettings())
    assert [r.status for r in results] == ["insufficient_data"] * 8
    doc = report(
        table, CompareSettings(), results, node_failures=None, sidecar="missing"
    )
    assert doc["failure_classifier"]["status"] == "unknown"
    assert "insufficient data" in markdown(doc)


def test_one_family_with_the_floor_lowered_still_answers() -> None:
    rows = synthetic_rows("signal", families=1, groups_per_family=6, runs_per_group=10)
    result = compare_term(
        table_from_rows(rows),
        "compute",
        CompareSettings(terms=("compute",), min_rows=10, min_families=1),
    )
    (fold,) = result.folds
    assert fold.train_rows == 0 and fold.selected.algorithm == "calibration"
    assert fold.inner_k is None and math.isinf(fold.q)
    # one family: no spread (or no learned model ships at all, D357)
    assert result.verdict["verdict"] in ("not_separable", "no_learned_model")
    assert result.status in ("trained", "no_learned_model")


def test_an_all_nan_feature_column_and_a_constant_target() -> None:
    nan = synthetic_rows(
        "signal",
        families=4,
        groups_per_family=4,
        runs_per_group=6,
        nan_column="load_cpu_percent",
    )
    result = _run(nan)
    assert result.status in ("trained", "no_learned_model")
    assert math.isfinite(result.pooled["male_model"])
    const = synthetic_rows(
        "signal", families=4, groups_per_family=4, runs_per_group=6, constant_y=True
    )
    flat = _run(const)
    assert math.isfinite(flat.pooled["male_model"])
    assert flat.pooled["male_model"] <= flat.pooled["male_raw"]


def test_unknown_terms_are_refused() -> None:
    with pytest.raises(ValueError, match="nope"):
        compare(table_from_rows([]), CompareSettings(terms=("nope",)))


# ------------------------------------------------------------------ determinism


def _main(dataset: Path, out: Path, *extra: str) -> bytes:
    code = train_main.main(
        [
            "compare",
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            "--min-rows",
            "50",
            "--algorithms",
            "ridge,tree,lightgbm,xgboost",
            *extra,
        ]
    )
    assert code == 0
    return (out / "report.json").read_bytes()


def test_the_same_file_twice_and_a_shuffled_file_give_byte_identical_reports(
    tmp_path: Path,
) -> None:
    rows = synthetic_rows(
        "signal",
        families=4,
        groups_per_family=4,
        runs_per_group=6,
        terms=("compute", "env"),
    )
    canonical = parquet_file(rows, tmp_path / "d.parquet")
    shuffled = parquet_file(rows, tmp_path / "s.parquet", shuffle_seed=11)
    first = _main(canonical, tmp_path / "r1")
    second = _main(canonical, tmp_path / "r2")
    third = _main(shuffled, tmp_path / "r3")
    assert first == second == third
    doc = json.loads(first)
    assert doc["dataset"]["sidecar"] == "missing"
    assert set(doc["libraries"]) >= {"numpy", "scikit-learn", "lightgbm", "xgboost"}
    assert "seconds" not in first.decode() and "wall" not in first.decode()
    assert (tmp_path / "r1" / "report.md").read_text().startswith("# ML comparison")
    assert json.loads((tmp_path / "r1" / "timings.json").read_text())["rows"] == len(
        rows
    )


def test_the_sidecar_feeds_the_failure_classifier_line(tmp_path: Path) -> None:
    rows = synthetic_rows("signal", families=3, groups_per_family=2, runs_per_group=4)
    path = parquet_file(rows, tmp_path / "d.parquet")
    digest = table_from_rows(rows).digest
    side = tmp_path / "d.parquet.json"
    side.write_text(json.dumps({"digest": digest, "counts": {"node_failures": 12}}))
    _main(path, tmp_path / "a")
    doc = json.loads((tmp_path / "a" / "report.json").read_text())
    assert doc["failure_classifier"] == {
        "min_events": 50,
        "node_failures": 12,
        "status": "not_trained",
    }
    assert "not trained, 12 < 50 events" in (tmp_path / "a" / "report.md").read_text()
    side.write_text(json.dumps({"digest": digest, "counts": {"node_failures": 50}}))
    _main(path, tmp_path / "b")
    doc = json.loads((tmp_path / "b" / "report.json").read_text())
    assert doc["failure_classifier"]["status"] == "eligible"
    side.write_text(
        json.dumps({"digest": "sha256:other", "counts": {"node_failures": 99}})
    )
    _main(path, tmp_path / "c")
    doc = json.loads((tmp_path / "c" / "report.json").read_text())
    assert doc["dataset"]["sidecar"] == "digest_mismatch"
    assert doc["failure_classifier"]["status"] == "unknown"


def test_report_json_is_canonical() -> None:
    rows = synthetic_rows("signal", families=3, groups_per_family=3, runs_per_group=8)
    table = table_from_rows(rows)
    settings = CompareSettings(terms=("compute",), min_rows=10, algorithms=("ridge",))
    doc = report(
        table, settings, compare(table, settings), node_failures=0, sidecar="matched"
    )
    text = canonical_json(doc)
    assert text == canonical_json(json.loads(text))  # sorted, stable
    assert "NaN" not in text and "Infinity" not in text

    def floats(value: Any) -> list[float]:
        if isinstance(value, float):
            return [value]
        if isinstance(value, dict):
            return [f for v in value.values() for f in floats(v)]
        if isinstance(value, list):
            return [f for v in value for f in floats(v)]
        return []

    assert all(float(f"{f:.6g}") == f for f in floats(doc))  # 6 significant digits
