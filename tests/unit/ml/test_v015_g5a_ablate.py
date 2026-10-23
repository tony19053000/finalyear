"""V0.15 G5a (D372): feature-group ablations and permutation importance.

* :func:`without_group` makes exactly that group's columns missing;
* on the synthetic truth where the residual follows the live load, dropping
  the **load** group hurts (``delta`` > 0 on every held-out family), and the
  family group is ``skipped`` when the full run has no family feature;
* the importance is the mean of 5 seeded permutations and names its repeats
  and its folds; ``importance_repeats`` 0 = none;
* ``compare --ablations`` writes a byte-identical report twice (D352) with
  an ``ablations`` block; the CLI table renders it.
"""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
import numpy as np
from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.cli.planner_commands import ml_ablation_table
from meshforge.controller.ml.schema import FEATURES, NUMERIC_FEATURES, FeatureGroup
from meshforge.controller.ml.train import __main__ as train_main
from meshforge.controller.ml.train.ablate import (
    ABLATION_GROUPS,
    ablate_term,
    without_group,
)
from meshforge.controller.ml.train.compare import CompareSettings, compare_term
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.report import REPORT_VERSION, ablation_rows

pytestmark = pytest.mark.unit

SETTINGS = CompareSettings(
    terms=("compute",),
    algorithms=("ridge",),
    min_rows=60,
    bootstrap_resamples=200,
    quantile_alternative=False,
    min_verdict_families=5,
)


@pytest.fixture(scope="module")
def table():  # type: ignore[no-untyped-def]
    return table_from_rows(
        synthetic_rows("signal", families=5, groups_per_family=3, runs_per_group=8)
    )


def test_without_group_blanks_exactly_that_group(table) -> None:  # type: ignore[no-untyped-def]
    dropped = without_group(table, FeatureGroup.LOAD)
    load = {f.name for f in FEATURES if f.group == FeatureGroup.LOAD}
    for j, name in enumerate(NUMERIC_FEATURES):
        if name in load:
            assert np.isnan(dropped.X[:, j]).all()
        else:
            assert np.array_equal(dropped.X[:, j], table.X[:, j], equal_nan=True)
    assert dropped.digest == table.digest and dropped.y is table.y
    assert ABLATION_GROUPS[-1] == FeatureGroup.FAMILY and len(ABLATION_GROUPS) == 7


def test_dropping_the_load_matters_and_family_is_skipped(table) -> None:  # type: ignore[no-untyped-def]
    full = compare_term(table, "compute", SETTINGS)
    rows = {r["group"]: r for r in ablate_term(table, full, SETTINGS)}
    assert list(rows) == [g.value for g in ABLATION_GROUPS]
    load = rows["load"]
    assert load["status"] == "evaluated" and load["families"] == 5
    assert all(d > 0 for d in load["per_family"].values())
    assert load["verdict"] == "matters" and load["low"] > 0
    assert load["delta_male_pooled"] > 0
    family = rows["family"]
    assert family["status"] == "skipped"
    assert family["reason"].startswith("not_in_full_model")
    # cache / network columns are all-missing in this data: nothing changes
    assert rows["cache"]["delta_male_pooled"] == pytest.approx(0.0, abs=1e-12)


def test_family_ablation_runs_when_the_full_model_has_family(table) -> None:  # type: ignore[no-untyped-def]
    settings = replace(SETTINGS, with_family=True)
    full = compare_term(table, "compute", settings)
    family = ablate_term(table, full, settings)[-1]
    assert family["group"] == "family" and family["status"] == "evaluated"


def test_insufficient_terms_are_skipped(table) -> None:  # type: ignore[no-untyped-def]
    full = compare_term(table, "env", SETTINGS)
    assert full.status == "insufficient_data"
    rows = ablate_term(table, full, SETTINGS)
    assert {r["status"] for r in rows} == {"skipped"}


def test_importance_is_the_mean_of_its_repeats(table) -> None:  # type: ignore[no-untyped-def]
    five = compare_term(table, "compute", SETTINGS)
    assert five.importance_repeats == 5 and five.importance
    assert five.importance_folds == 5
    top = dict(five.importance)
    assert top["load_cpu_percent"] > 0  # the signal's live load
    none = compare_term(table, "compute", replace(SETTINGS, importance_repeats=0))
    assert none.importance == () and none.importance_repeats == 0
    one = compare_term(table, "compute", replace(SETTINGS, importance_repeats=1))
    assert dict(one.importance) != top  # really a different number of draws


def test_compare_with_ablations_is_byte_identical_and_rendered(
    tmp_path: Path,
) -> None:
    dataset = parquet_file(
        synthetic_rows("signal", families=5, groups_per_family=3, runs_per_group=8),
        tmp_path / "d.parquet",
    )
    texts = []
    for name in ("a", "b"):
        out = tmp_path / name
        code = train_main.main(
            [
                "compare",
                "--dataset",
                str(dataset),
                "--out",
                str(out),
                "--terms",
                "compute",
                "--algorithms",
                "ridge",
                "--min-rows",
                "60",
                "--ablations",
            ]
        )
        assert code == 0
        texts.append((out / "report.json").read_text(encoding="utf-8"))
    assert texts[0] == texts[1]
    doc = json.loads(texts[0])
    assert doc["report_version"] == REPORT_VERSION == 2
    assert doc["settings"]["ablations"] is True
    assert doc["settings"]["importance_repeats"] == 5
    assert doc["terms"][0]["importance_repeats"] == 5
    groups = doc["ablations"]["terms"]["compute"]
    assert [g["group"] for g in groups] == [g.value for g in ABLATION_GROUPS]
    md = (tmp_path / "a" / "report.md").read_text(encoding="utf-8")
    assert "## Feature-group ablations" in md and "| compute | load |" in md
    assert len(ablation_rows(doc)) == 7
    table = ml_ablation_table(doc)
    assert table.row_count == 7
    assert not math.isnan(groups[1]["delta_male_pooled"])


def test_without_the_flag_there_is_no_ablation_block(tmp_path: Path) -> None:
    dataset = parquet_file(
        synthetic_rows("signal", families=3, groups_per_family=2, runs_per_group=4),
        tmp_path / "d.parquet",
    )
    out = tmp_path / "o"
    assert (
        train_main.main(
            [
                "compare",
                "--dataset",
                str(dataset),
                "--out",
                str(out),
                "--terms",
                "compute",
            ]
        )
        == 0
    )
    doc = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert "ablations" not in doc and doc["settings"]["ablations"] is False
    assert ml_ablation_table(doc).row_count == 0
