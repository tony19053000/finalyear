# ruff: noqa: S311, SIM105
"""V0.15 G3 tester r1 - artefact / serving adversarial tests (D339, D359).

Derived from the criteria, not from the implementation:

* round trip equal to 1e-9 for **every** algorithm, also with NaN cells and
  extreme-but-finite values in the matrix that is predicted;
* every damaged artefact (truncated at any position, any byte flipped, wrong
  digest, wrong feature_version, oversized, hand-edited and re-digested,
  hostile nesting) -> :class:`ArtefactError` and nothing else;
* serving rows that are empty / have inf cells do not crash the process.

The native parsers (LightGBM / XGBoost) are only fed bytes whose digest
matches (accepted risk, see the G3 note), so the hostile *re-digested*
documents here are restricted to the numpy-served algorithms.
"""

from __future__ import annotations

import base64
import json
import random
import warnings
from typing import Any

import numpy as np
import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml import artefact as art_mod
from meshforge.controller.ml.artefact import ArtefactError, decode, digest, encode
from meshforge.controller.ml.schema import FEATURE_VERSION, NUMERIC_FEATURES
from meshforge.controller.ml.serve import load_model
from meshforge.controller.ml.train.algorithms import GRID, Config, FitContext, fit_one
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import Table, table_from_rows
from meshforge.controller.ml.train.fit import artefact_bytes

pytestmark = pytest.mark.unit

SETTINGS = CompareSettings(terms=("compute",), min_rows=50)
ALGORITHMS = ["ridge", "knn", "tree", "forest", "lightgbm", "xgboost"]
NUMPY_ONLY = ["ridge", "knn", "tree", "forest"]


@pytest.fixture(scope="module")
def table() -> Table:
    rows = synthetic_rows(
        "signal", families=5, groups_per_family=4, runs_per_group=10, seed=11
    )
    for i, row in enumerate(rows):
        if i % 7 == 0:
            row["load_cpu_percent"] = float("nan")
        if i % 11 == 0:
            row["hw_cpu_threads"] = float("nan")
        if i % 5 == 0:
            row["hw_ram_bytes"] = float("nan")
    return table_from_rows(rows)


def _config(algorithm: str, index: int = -1) -> Config:
    grid = GRID[algorithm]
    return Config(algorithm, tuple(sorted(grid[index].items())), len(grid) - 1)


_CACHE: dict[str, tuple[Any, bytes]] = {}


def _fit(table: Table, algorithm: str) -> tuple[Any, bytes]:
    if algorithm not in _CACHE:
        rows = table.rows_of("compute")
        train = rows[: int(len(rows) * 0.8)]
        config = _config(algorithm)
        ctx = FitContext(table=table, seed=0, fold="final")
        model = fit_one(config, ctx, train)
        _CACHE[algorithm] = (
            model,
            artefact_bytes(table, "compute", config, model, train, 0.25, SETTINGS),
        )
    return _CACHE[algorithm]


def _load(data: bytes) -> Any:
    return load_model(data, expected_version=digest(data))


# ------------------------------------------------------------- round trip


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_round_trip_with_nan_cells_and_extremes(table: Table, algorithm: str) -> None:
    model, data = _fit(table, algorithm)
    loaded = _load(data)
    rows = table.rows_of("compute")
    test = rows[int(len(rows) * 0.8) :]
    X = table.X[test].copy()
    rng = np.random.default_rng(0)
    # NaN in ~25 % of the cells of the real numeric columns, whole NaN rows,
    # and a few extreme finite values
    mask = rng.random(X.shape) < 0.25
    X[mask] = np.nan
    X[0, :] = np.nan
    X[1, :] = 1e12
    X[2, :] = -1e12
    X[3, :] = 0.0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        expected = model.predict(X, test)
        got = loaded.predict(X)
    assert np.isfinite(expected).all()
    np.testing.assert_allclose(got.y_hat, expected, rtol=0, atol=1e-9)
    assert got.y_hat.shape == (len(test),)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_predicting_zero_rows_and_one_row(table: Table, algorithm: str) -> None:
    _, data = _fit(table, algorithm)
    loaded = _load(data)
    empty = loaded.predict(np.empty((0, len(NUMERIC_FEATURES))))
    assert empty.y_hat.shape == (0,) and empty.ood.shape == (0,)
    one = loaded.predict(table.X[:1])
    assert one.y_hat.shape == (1,)
    # row-independent: the one-row answer equals the same row in a batch
    many = loaded.predict(table.X[:25])
    np.testing.assert_allclose(many.y_hat[:1], one.y_hat, atol=1e-12)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_wrong_width_rows_are_a_value_error_not_a_crash(
    table: Table, algorithm: str
) -> None:
    _, data = _fit(table, algorithm)
    loaded = _load(data)
    with pytest.raises(ValueError):
        loaded.predict(np.zeros((3, len(NUMERIC_FEATURES) - 1)))
    with pytest.raises(ValueError):
        loaded.predict(np.zeros(len(NUMERIC_FEATURES)))


@pytest.mark.parametrize("algorithm", NUMPY_ONLY)
def test_inf_cells_do_not_crash_the_numpy_models(table: Table, algorithm: str) -> None:
    _, data = _fit(table, algorithm)
    loaded = _load(data)
    X = table.X[:10].copy()
    X[0, 5] = np.inf
    X[1, 5] = -np.inf
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        out = loaded.predict(X)
    assert out.y_hat.shape == (10,)
    # an infinite cell is outside any training range
    assert bool(out.ood[0]) and bool(out.ood[1])


# ------------------------------------------------------------- damage


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_every_truncation_point_is_an_artefact_error(
    table: Table, algorithm: str
) -> None:
    _, data = _fit(table, algorithm)
    cuts = sorted({0, 1, 2, 10, len(data) // 3, len(data) // 2, len(data) - 1})
    cuts += random.Random(1).sample(range(len(data)), 25)
    for cut in cuts:
        with pytest.raises(ArtefactError):
            load_model(data[:cut], expected_version=digest(data))
        # even when the caller does not know the digest (None), a truncated
        # canonical document is not valid JSON
        if cut < len(data):
            with pytest.raises(ArtefactError):
                load_model(data[:cut], expected_version=None)


@pytest.mark.parametrize("algorithm", ALGORITHMS)
def test_single_byte_flips_are_refused_with_the_expected_digest(
    table: Table, algorithm: str
) -> None:
    _, data = _fit(table, algorithm)
    rng = random.Random(2)
    for _ in range(60):
        pos = rng.randrange(len(data))
        bad = bytearray(data)
        bad[pos] ^= 1 << rng.randrange(8)
        with pytest.raises(ArtefactError) as caught:
            load_model(bytes(bad), expected_version=digest(data))
        assert caught.value.details["reason"] == "digest_mismatch"


@pytest.mark.parametrize("algorithm", NUMPY_ONLY)
def test_single_byte_flips_without_a_digest_never_escape_as_other_errors(
    table: Table, algorithm: str
) -> None:
    """expected_version=None skips the digest (only for just-encoded bytes);
    a flipped byte must still be an ArtefactError or load a model that
    predicts without raising - never another exception type."""
    _, data = _fit(table, algorithm)
    rng = random.Random(3)
    X = table.X[:5]
    for _ in range(300):
        bad = bytearray(data)
        for _ in range(rng.randrange(1, 4)):
            bad[rng.randrange(len(data))] = rng.randrange(256)
        try:
            loaded = load_model(bytes(bad), expected_version=None)
        except ArtefactError:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            loaded.predict(X)


def test_a_wrong_digest_string_shape_is_refused(table: Table) -> None:
    _, data = _fit(table, "ridge")
    for wrong in ("", "sha256:", "sha256:" + "0" * 64, "md5:abc", digest(data).upper()):
        with pytest.raises(ArtefactError):
            load_model(data, expected_version=wrong)


def test_oversized_is_refused_before_hashing(
    table: Table, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, data = _fit(table, "ridge")
    monkeypatch.setattr(art_mod, "MAX_ARTEFACT_BYTES", len(data) - 1)
    with pytest.raises(ArtefactError) as caught:
        load_model(data, expected_version=digest(data))
    assert caught.value.details["reason"] == "too_large"


def _doc(data: bytes) -> dict[str, Any]:
    out = json.loads(data)
    assert isinstance(out, dict)
    return out


def _redigest(doc: Any) -> bytes:
    return encode(doc)


@pytest.mark.parametrize("bad", [FEATURE_VERSION + 1, 0, -1, True, "1", None, 1.5])
def test_wrong_feature_version_with_a_matching_digest(table: Table, bad: Any) -> None:
    _, data = _fit(table, "ridge")
    doc = _doc(data)
    doc["feature_version"] = bad
    new = _redigest(doc)
    with pytest.raises(ArtefactError):
        load_model(new, expected_version=digest(new))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(features=list(reversed(d["features"]))),
        lambda d: d.update(features=d["features"][:-1]),
        lambda d: d.update(features=d["features"] + ["extra"]),
        lambda d: d.update(artefact_version=2),
        lambda d: d.update(artefact_version=True),
        lambda d: d.update(algorithm="pickle"),
        lambda d: d.update(algorithm=None),
        lambda d: d.update(model=[]),
        lambda d: d.update(prep=None),
        lambda d: d.update(ood="x"),
        lambda d: d.pop("conformal"),
        lambda d: d.update(conformal={"q": -1.0, "alpha": 0.1}),
        lambda d: d.update(conformal={"q": "0.3", "alpha": 0.1}),
        lambda d: d.update(conformal={"q": 0.3, "alpha": 0}),
        lambda d: d.update(conformal={"q": 0.3, "alpha": 1.5}),
        lambda d: d.update(conformal={"q": 0.3, "alpha": True}),
        lambda d: d.update(term=5),
        lambda d: d.update(standardise="yes"),
        lambda d: d.update(libraries=[]),
        lambda d: d.update(params=None),
        lambda d: d.update(dataset_digest=None),
    ],
)
def test_edited_but_redigested_documents_are_artefact_errors(
    table: Table, mutate: Any
) -> None:
    _, data = _fit(table, "ridge")
    doc = _doc(data)
    mutate(doc)
    new = _redigest(doc)
    with pytest.raises(ArtefactError):
        load_model(new, expected_version=digest(new))


def _array_paths(node: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    found: list[tuple[Any, ...]] = []
    if isinstance(node, dict):
        if set(node) == {"dtype", "shape", "b64"}:
            return [path]
        for k, v in node.items():
            found += _array_paths(v, (*path, k))
    elif isinstance(node, list):
        for i, v in enumerate(node):
            found += _array_paths(v, (*path, i))
    return found


def _at(doc: Any, path: tuple[Any, ...]) -> Any:
    for key in path:
        doc = doc[key]
    return doc


@pytest.mark.parametrize("algorithm", NUMPY_ONLY)
def test_every_array_block_hostile_variant_is_an_artefact_error(
    table: Table, algorithm: str
) -> None:
    _, data = _fit(table, algorithm)
    base = _doc(data)
    paths = _array_paths(base)
    assert paths
    variants = [
        lambda b: {**b, "dtype": "<f4"},
        lambda b: {**b, "dtype": "O"},
        lambda b: {**b, "dtype": None},
        lambda b: {**b, "shape": [2**62, 2**62]},
        lambda b: {**b, "shape": [-1]},
        lambda b: {**b, "shape": "x"},
        lambda b: {**b, "shape": [True]},
        lambda b: {**b, "b64": ""},
        lambda b: {**b, "b64": 5},
        lambda b: {**b, "b64": "!!!not base64!!!"},
        lambda b: {**b, "b64": b["b64"][:-4]},
        lambda b: {**b, "b64": b["b64"] + "AAAA"},
        lambda b: {**b, "b64": "é" + b["b64"]},
        lambda b: {**b, "b64": None},
        lambda b: {"dtype": b["dtype"], "shape": b["shape"]},
        lambda b: [],
        lambda b: None,
    ]
    rng = random.Random(4)
    for path in rng.sample(paths, min(len(paths), 14)):
        for variant in variants:
            if path == ("ood", "reference") and variant is variants[-1]:
                continue  # a null OOD reference is the documented "kNN part off"
            doc = json.loads(data)
            parent = _at(doc, path[:-1])
            parent[path[-1]] = variant(parent[path[-1]])
            new = _redigest(doc)
            with pytest.raises(ArtefactError):
                load_model(new, expected_version=digest(new))


@pytest.mark.parametrize("algorithm", NUMPY_ONLY)
def test_nan_inf_planted_in_float_arrays_never_crashes(
    table: Table, algorithm: str
) -> None:
    """Raw bytes can hold NaN / inf even though canonical JSON cannot: a
    re-digested artefact with them must be refused or serve without an
    exception."""
    _, data = _fit(table, algorithm)
    base = _doc(data)
    X = table.X[:6]
    for path in _array_paths(base):
        block = _at(base, path)
        if block["dtype"] != "<f8" or not block["shape"] or 0 in block["shape"]:
            continue
        raw = bytearray(base64.b64decode(block["b64"]))
        for special in (np.nan, np.inf, -np.inf):
            patched = bytearray(raw)
            patched[0:8] = np.asarray([special], dtype="<f8").tobytes()
            doc = json.loads(data)
            _at(doc, path)["b64"] = base64.b64encode(bytes(patched)).decode("ascii")
            new = _redigest(doc)
            try:
                loaded = load_model(new, expected_version=digest(new))
            except ArtefactError:
                continue
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                loaded.predict(X)


def test_tree_with_out_of_range_features_or_children_is_refused(table: Table) -> None:
    _, data = _fit(table, "tree")
    base = _doc(data)
    tree_path = next(p for p in _array_paths(base) if p[-1] == "feature")
    block = _at(base, tree_path)
    feat = np.frombuffer(base64.b64decode(block["b64"]), dtype="<i8").copy()
    inner = np.flatnonzero(feat >= 0)
    assert inner.size
    for bad in (len(NUMERIC_FEATURES) + 40, 10**6, -5):
        patched = feat.copy()
        patched[inner[0]] = bad
        doc = json.loads(data)
        _at(doc, tree_path)["b64"] = base64.b64encode(patched.tobytes()).decode()
        new = _redigest(doc)
        with pytest.raises(ArtefactError):
            load_model(new, expected_version=digest(new))


def test_forest_with_one_hostile_tree_among_good_ones_is_refused(table: Table) -> None:
    _, data = _fit(table, "forest")
    doc = json.loads(data)
    trees = doc["model"]["trees"]
    assert len(trees) > 2
    # a self-loop on the last tree's root
    left = np.frombuffer(base64.b64decode(trees[-1]["left"]["b64"]), dtype="<i8").copy()
    left[0] = 0
    trees[-1]["left"]["b64"] = base64.b64encode(left.tobytes()).decode()
    new = _redigest(doc)
    with pytest.raises(ArtefactError):
        load_model(new, expected_version=digest(new))


# ------------------------------------------------------------- hostile JSON


def test_deeply_nested_json_is_an_artefact_error_not_a_recursion_error() -> None:
    for depth in (50, 500, 900, 990, 5000, 100_000):
        data = b"[" * depth + b"]" * depth
        with pytest.raises(ArtefactError):
            load_model(data, expected_version=digest(data))
        data = b'{"a":' * depth + b"1" + b"}" * depth
        with pytest.raises(ArtefactError):
            load_model(data, expected_version=digest(data))


def test_deep_nesting_inside_a_real_looking_document(table: Table) -> None:
    _, data = _fit(table, "ridge")
    for depth in (400, 900, 980):
        doc = json.loads(data)
        nest: Any = 1
        for _ in range(depth):
            nest = [nest]
        doc["params"] = {"deep": nest}
        try:
            new = _redigest(doc)
        except RecursionError:
            continue  # the test's own encoder cannot make it
        # `params` is free-form: either a clean load or a typed refusal
        try:
            load_model(new, expected_version=digest(new))
        except ArtefactError:
            pass


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"null",
        b"[]",
        b"{}",
        b'"x"',
        b"1",
        b"\xff\xfe\x00",
        b'{"a":NaN}',
        b'{"a":Infinity}',
        b'{"a":1e999}',
        b'{"a":' + b"9" * 5000 + b"}",
        b'{"a":1,"a":2}',
        '{"é":1}'.encode(),
        b'\xef\xbb\xbf{"artefact_version":1}',
        b'{"artefact_version":1}\n',
        b" {}",
    ],
)
def test_junk_documents_are_artefact_errors(data: bytes) -> None:
    for expected in (digest(data), None):
        with pytest.raises(ArtefactError):
            load_model(data, expected_version=expected)


def test_duplicate_keys_cannot_smuggle_a_second_value(table: Table) -> None:
    """A document with a duplicated key is not canonical (the re-encoding
    drops one), so it is refused even though json.loads accepts it."""
    _, data = _fit(table, "ridge")
    text = data.decode("ascii")
    smuggled = (text[:-1] + ',"term":"evil"}').encode("ascii")
    with pytest.raises(ArtefactError):
        load_model(smuggled, expected_version=digest(smuggled))


def test_decode_is_pure_no_pickle_bytes_are_ever_interpreted(table: Table) -> None:
    import pickle

    class Boom:
        def __reduce__(self) -> tuple[Any, ...]:
            return (eval, ("1/0",))

    payload = pickle.dumps(Boom())
    for data in (payload, base64.b64encode(payload)):
        with pytest.raises(ArtefactError):
            load_model(data, expected_version=digest(data))
    # and as an array block's content
    _, ok = _fit(table, "ridge")
    doc = json.loads(ok)
    doc["model"]["coef"]["b64"] = base64.b64encode(payload).decode()
    new = _redigest(doc)
    with pytest.raises(ArtefactError):
        load_model(new, expected_version=digest(new))


def test_decode_signature_rejects_foreign_features(table: Table) -> None:
    _, data = _fit(table, "ridge")
    with pytest.raises(ArtefactError):
        decode(
            data,
            expected_version=digest(data),
            feature_version=FEATURE_VERSION,
            features=NUMERIC_FEATURES[:-1],
        )
    with pytest.raises(ArtefactError):
        decode(
            data,
            expected_version=digest(data),
            feature_version=FEATURE_VERSION + 1,
            features=NUMERIC_FEATURES,
        )


def test_xgboost_artefact_with_a_wrong_feature_count_is_typed_at_load_or_predict(
    table: Table,
) -> None:
    """A digest-matching XGBoost document whose booster says it has 3
    features. The refusal must be an ArtefactError at load time (or the
    predict must work): a raw XGBoostError at the first predict is a crash in
    the planner's dispatcher path."""
    _, data = _fit(table, "xgboost")
    doc = json.loads(data)
    booster = json.loads(doc["model"]["json"])
    booster["learner"]["learner_model_param"]["num_feature"] = "3"
    doc["model"]["json"] = json.dumps(booster, separators=(",", ":"))
    new = _redigest(doc)
    try:
        loaded = load_model(new, expected_version=digest(new))
    except ArtefactError:
        return
    try:
        loaded.predict(table.X[:3])
    except ArtefactError:
        return
    except Exception as exc:  # noqa: BLE001 - this is the defect being probed
        pytest.fail(f"predict raised {type(exc).__name__}, not ArtefactError")


def test_lightgbm_artefact_with_a_wrong_feature_count_is_typed(table: Table) -> None:
    import re

    _, data = _fit(table, "lightgbm")
    doc = json.loads(data)
    doc["model"]["text"] = re.sub(
        r"max_feature_idx=\d+", "max_feature_idx=3", doc["model"]["text"]
    )
    new = _redigest(doc)
    try:
        loaded = load_model(new, expected_version=digest(new))
    except ArtefactError:
        return
    try:
        loaded.predict(table.X[:3])
    except ArtefactError:
        return
    except Exception as exc:  # noqa: BLE001
        pytest.fail(f"predict raised {type(exc).__name__}, not ArtefactError")
