"""V0.15 G3 (D339, D359): no-pickle artefacts and the numpy serving side.

* **Round trip** - for every algorithm, the model loaded from its artefact
  predicts what the in-memory trained model predicts (to 1e-9) on held-out
  rows, and its OOD test flags exactly what the G2 :class:`OodModel` flags;
* **content-derived version** - the version is the SHA-256 of the canonical
  bytes; the same rows give byte-identical artefacts;
* the artefact holds the preprocessing (a model without it is refused), and
  a different ``feature_version`` / column list is refused;
* **fuzz** - truncated, bit-flipped, re-ordered or hand-edited bytes give a
  typed :class:`ArtefactError`, never another exception.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from typing import Any

import numpy as np
import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.artefact import ArtefactError, decode, digest, encode
from meshforge.controller.ml.schema import FEATURE_VERSION, NUMERIC_FEATURES
from meshforge.controller.ml.serve import load_model
from meshforge.controller.ml.train.algorithms import GRID, Config, FitContext, fit_one
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import Table, table_from_rows
from meshforge.controller.ml.train.fit import artefact_bytes
from meshforge.controller.ml.train.ood import fit_ood

pytestmark = pytest.mark.unit

SETTINGS = CompareSettings(terms=("compute",), min_rows=50)


@pytest.fixture(scope="module")
def table() -> Table:
    rows = synthetic_rows(
        "signal", families=5, groups_per_family=4, runs_per_group=10, seed=7
    )
    # some missing values, so imputation + indicators are exercised
    for i, row in enumerate(rows):
        if i % 9 == 0:
            row["load_cpu_percent"] = float("nan")
        if i % 13 == 0:
            row["hw_cpu_threads"] = float("nan")
    return table_from_rows(rows)


def _split(table: Table) -> tuple[np.ndarray, np.ndarray]:
    rows = table.rows_of("compute")
    held = np.asarray([table.family[i] == "fam04" for i in rows])
    return rows[~held], rows[held]


def _config(algorithm: str, index: int = -1) -> Config:
    grid = GRID[algorithm]
    params = grid[index]
    return Config(algorithm, tuple(sorted(params.items())), len(grid) - 1)


def _fit(table: Table, algorithm: str) -> tuple[Config, Any, bytes]:
    train, _ = _split(table)
    config = _config(algorithm)
    ctx = FitContext(table=table, seed=0, fold="final")
    model = fit_one(config, ctx, train)
    data = artefact_bytes(table, "compute", config, model, train, 0.3, SETTINGS)
    return config, model, data


@pytest.mark.parametrize(
    "algorithm", ["ridge", "knn", "tree", "forest", "lightgbm", "xgboost"]
)
def test_round_trip_predictions_equal_the_trained_model(
    table: Table, algorithm: str
) -> None:
    _, model, data = _fit(table, algorithm)
    train, test = _split(table)
    loaded = load_model(data, expected_version=digest(data))
    X = table.X[test]
    expected = model.predict(X, test)
    got = loaded.predict(X)
    np.testing.assert_allclose(got.y_hat, expected, rtol=0, atol=1e-9)
    np.testing.assert_allclose(got.hi - got.y_hat, 0.3, atol=1e-12)
    np.testing.assert_allclose(got.y_hat - got.lo, 0.3, atol=1e-12)
    assert got.model_version == digest(data)
    # the OOD test is the G2 one, as fitted
    ood = fit_ood(table.X[train])
    flags, reasons = ood.check(X)
    assert np.array_equal(got.ood, flags)
    assert list(got.reasons) == reasons
    # also on rows far outside the training range
    far = X.copy()
    far[:, NUMERIC_FEATURES.index("job_input_bytes")] *= 1e4
    flags, reasons = ood.check(far)
    out = loaded.predict(far)
    assert out.ood.all() and np.array_equal(out.ood, flags)
    assert list(out.reasons) == reasons


@pytest.mark.parametrize("algorithm", ["ridge", "forest", "lightgbm"])
def test_same_rows_same_bytes_and_the_version_is_the_digest(
    table: Table, algorithm: str
) -> None:
    _, _, first = _fit(table, algorithm)
    _, _, second = _fit(table, algorithm)
    assert first == second
    art = decode(
        first,
        expected_version=digest(first),
        feature_version=FEATURE_VERSION,
        features=NUMERIC_FEATURES,
    )
    assert art.version == "sha256:" + __import__("hashlib").sha256(first).hexdigest()


def _doc(data: bytes) -> dict[str, Any]:
    out: dict[str, Any] = json.loads(data)
    return out


def _decode(data: bytes) -> Any:
    return decode(
        data,
        expected_version=None,
        feature_version=FEATURE_VERSION,
        features=NUMERIC_FEATURES,
    )


def test_an_artefact_without_its_preprocessing_is_refused(table: Table) -> None:
    _, _, data = _fit(table, "ridge")
    doc = _doc(data)
    del doc["prep"]
    with pytest.raises(ArtefactError) as caught:
        _decode(encode(doc))
    assert caught.value.details["reason"] == "missing_field"


def test_another_feature_version_or_column_list_is_refused(table: Table) -> None:
    _, _, data = _fit(table, "ridge")
    doc = _doc(data)
    doc["feature_version"] = FEATURE_VERSION + 1
    with pytest.raises(ArtefactError) as caught:
        _decode(encode(doc))
    assert caught.value.details["reason"] == "feature_version"
    doc = _doc(data)
    doc["features"] = list(reversed(doc["features"]))
    with pytest.raises(ArtefactError) as caught:
        _decode(encode(doc))
    assert caught.value.details["reason"] == "features"


def test_a_wrong_digest_and_non_canonical_bytes_are_refused(table: Table) -> None:
    _, _, data = _fit(table, "tree")
    with pytest.raises(ArtefactError) as caught:
        load_model(data, expected_version="sha256:" + "0" * 64)
    assert caught.value.details["reason"] == "digest_mismatch"
    pretty = json.dumps(_doc(data), indent=1).encode()
    with pytest.raises(ArtefactError) as caught:
        load_model(pretty, expected_version=None)
    assert caught.value.details["reason"] == "not_canonical"


def test_a_tree_with_a_cycle_is_refused(table: Table) -> None:
    _, _, data = _fit(table, "tree")
    doc = _doc(data)
    left = doc["model"]["trees"][0]["left"]
    arr = np.frombuffer(__import__("base64").b64decode(left["b64"]), "<i8").copy()
    arr[0] = 0  # the root points at itself
    left["b64"] = __import__("base64").b64encode(arr.tobytes()).decode()
    with pytest.raises(ArtefactError) as caught:
        _decode(encode(doc))
    assert caught.value.details["reason"] == "bad_tree"


@pytest.mark.parametrize("algorithm", ["ridge", "knn", "tree", "lightgbm", "xgboost"])
def test_fuzzed_bytes_only_ever_raise_artefact_error(
    table: Table, algorithm: str
) -> None:
    _, _, data = _fit(table, algorithm)
    rng = random.Random(f"fuzz-{algorithm}")  # noqa: S311 - reproducible fuzz
    cases: list[bytes] = [b"", b"{}", b"[]", b"null", data[: len(data) // 2]]
    for _ in range(60):
        mutated = bytearray(data)
        for _ in range(rng.randint(1, 4)):
            pos = rng.randrange(len(mutated))
            mutated[pos] = rng.randrange(256)
        cases.append(bytes(mutated))
        cut = rng.randrange(len(data))
        cases.append(data[:cut])
    for case in cases:
        if case != data:  # the registry's digest catches every changed byte
            with pytest.raises(ArtefactError):
                load_model(case, expected_version=digest(data))
    # well-formed JSON with hostile field values and a *matching* digest:
    # validation answers before any native parser sees the model text (a
    # LightGBM / XGBoost parser given hostile text can abort the process -
    # which is why the parsers only ever see bytes whose digest matches a
    # registry row, written after the training child parsed the same bytes)
    _, test = _split(table)
    X = table.X[test]
    doc = _doc(data)
    for key in list(doc):
        for value in (None, "x", -1, [], {}, True, 1e308):
            hostile = encode({**doc, key: value})
            try:
                loaded = load_model(hostile, expected_version=digest(hostile))
            except ArtefactError:
                continue
            out = loaded.predict(X)  # loads -> still predicts, no crash
            assert out.y_hat.shape == (len(test),)


def test_decoded_prep_is_the_trained_prep(table: Table) -> None:
    _, model, data = _fit(table, "ridge")
    art = _decode(data)
    assert np.array_equal(art.prep.medians, model.prep.medians)
    assert art.prep.indicator_cols == model.prep.indicator_cols
    assert np.array_equal(art.prep.mean, model.prep.mean)
    assert np.array_equal(art.prep.std, model.prep.std)
    assert replace(art.prep) == art.prep
