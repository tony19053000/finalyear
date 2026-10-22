"""V0.15 G2 (D347, brief §4/§5b): no part fitted for an outer fold ever sees
the held-out family.

The whole nested loop runs with an instrumented observer: every fitted part
reports the rows it was fitted on (outer training side, every inner fold's
two sides, the preprocessing, every model, the calibration baseline, the
conformal residuals, the OOD test, the quantile models). Over random
datasets - including fingerprints recorded under two families - no reported
row of fold F belongs to a fingerprint whose family is F, inner folds never
share a fingerprint, and every kind of part was seen (so a new, unobserved
part cannot hide).
"""

from __future__ import annotations

import random
from collections import defaultdict

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

import numpy as np
from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.train.compare import CompareSettings, compare_term
from meshforge.controller.ml.train.data import table_from_rows

pytestmark = pytest.mark.unit

KINDS = {
    "outer_train",
    "inner_train",
    "inner_test",
    "prep",
    "model",
    "calibration",
    "conformal",
    "ood",
    "quantile",
}


@pytest.mark.parametrize("seed", range(5))
def test_no_fitted_part_sees_its_held_out_family(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 - reproducible test data
    rows = synthetic_rows(
        rng.choice(["signal", "node_noise"]),
        families=rng.randint(3, 5),
        groups_per_family=rng.randint(3, 5),
        runs_per_group=rng.randint(3, 6),
        noise=0.2,
        seed=seed,
    )
    # a few rows of some fingerprints recorded under another family: the
    # fingerprint still lands on one side only (its majority family)
    families = sorted({r["family"] for r in rows})
    for row in rows:
        if rng.random() < 0.1:
            row["family"] = rng.choice(families)
    table = table_from_rows(rows)
    term_rows = table.rows_of("compute")
    family_of_group = table.families_of(term_rows)
    events: list[tuple[str, str, np.ndarray]] = []
    result = compare_term(
        table,
        "compute",
        CompareSettings(
            terms=("compute",),
            min_rows=10,
            algorithms=("ridge", "knn", "tree", "forest", "lightgbm", "xgboost"),
        ),
        observer=lambda kind, fold, idx: events.append((kind, fold, np.array(idx))),
    )
    assert result.status != "insufficient_data"
    assert {kind for kind, _, _ in events} == KINDS
    held: dict[str, set[str]] = defaultdict(set)
    for group, family in family_of_group.items():
        held[family].add(group)
    folds = {fold for _, fold, _ in events if fold != "final"}
    assert folds == set(held)
    for kind, fold, idx in events:
        if fold == "final":
            continue
        groups = {table.group[i] for i in idx}
        assert not groups & held[fold], (kind, fold, sorted(groups & held[fold]))
    # inner folds: the two sides of one inner fold never share a fingerprint
    pending: np.ndarray | None = None
    for kind, _fold, idx in events:
        if kind == "inner_train":
            pending = idx
        elif kind == "inner_test":
            assert pending is not None
            train_groups = {table.group[i] for i in pending}
            assert not train_groups & {table.group[i] for i in idx}
            pending = None
    # the held-out rows of fold F are exactly F's fingerprints' rows
    for fold in result.folds:
        expected = sum(
            1 for i in term_rows if family_of_group[table.group[i]] == fold.family
        )
        assert fold.test_rows == expected
