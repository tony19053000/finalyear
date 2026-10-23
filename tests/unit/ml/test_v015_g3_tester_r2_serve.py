"""V0.15 G3 tester r2 - D2/D3 at the model level: zero rows, one row, NaN /
inf rows, wrong widths, for every learned algorithm that is installed."""

from __future__ import annotations

import json

import numpy as np
import pytest

pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.artefact import ArtefactError, digest, encode
from meshforge.controller.ml.features import NUMERIC_FEATURES
from meshforge.controller.ml.serve import load_model
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set

pytestmark = pytest.mark.unit
W = len(NUMERIC_FEATURES)


def _artefacts() -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for algo in ("ridge", "lightgbm", "xgboost", "knn", "random_forest"):
        try:
            s = CompareSettings(terms=("compute",), algorithms=(algo,), min_rows=100)
            rows = synthetic_rows(
                "signal", families=6, groups_per_family=5, runs_per_group=10, seed=7
            )
            res = train_set(table_from_rows(rows), s)
        except Exception:  # noqa: BLE001 - algorithm unknown / not installed
            continue
        for m in res.members:
            if m.artefact:
                out[algo] = m.artefact
    return out


ARTS = _artefacts()


@pytest.mark.parametrize("algo", sorted(ARTS) or ["none"])
def test_every_installed_algorithm_survives_odd_row_counts_and_values(
    algo: str,
) -> None:
    if algo == "none":
        pytest.skip("no learned algorithm trained")
    data = ARTS[algo]
    model = load_model(data, expected_version=digest(data))
    zero = model.predict(np.empty((0, W)))
    assert zero.y_hat.shape == (0,)
    assert zero.lo.shape == (0,) and zero.hi.shape == (0,)
    one = model.predict(np.full((1, W), np.nan))
    assert one.y_hat.shape == (1,)
    big = model.predict(np.random.default_rng(0).normal(size=(2000, W)) * 1e12)
    assert big.y_hat.shape == (2000,)
    with (
        pytest.warns(RuntimeWarning)
        if algo in ("ridge", "knn")
        else np.errstate(all="ignore")
    ):
        try:  # inf is not a feature value the planner produces: a typed error is fine
            inf = model.predict(np.full((3, W), np.inf))
            assert inf.y_hat.shape == (3,)
        except ArtefactError as exc:
            assert exc.details["reason"] == "bad_model"
    with pytest.raises(ValueError):
        model.predict(np.zeros((2, W + 1)))
    with pytest.raises(ValueError):
        model.predict(np.zeros(W))


@pytest.mark.parametrize("algo", sorted(ARTS) or ["none"])
def test_a_tampered_model_payload_is_a_typed_error_never_a_raw_one(algo: str) -> None:
    if algo == "none":
        pytest.skip("no learned algorithm trained")
    if algo == "lightgbm":
        pytest.skip(
            "LightGBM text parser can SIGABRT on a tampered model (accepted risk)"
        )
    doc = json.loads(ARTS[algo])
    model = doc["model"]
    for key in list(model):
        mutated = json.loads(json.dumps(doc))
        v = mutated["model"][key]
        mutated["model"][key] = (
            "garbage" if not isinstance(v, str) else v[: len(v) // 2]
        )
        data = encode(mutated)
        try:
            load_model(data, expected_version=digest(data))
        except ArtefactError:
            continue  # typed
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"{algo}.{key}: raw {type(exc).__name__} escaped load: {exc}")
