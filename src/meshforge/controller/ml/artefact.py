"""The ML model artefact format - no pickle (V0.15 G3, D339, D359; brief §4).

Loading a pickled model is remote code execution by another name
(CLAUDE.md §9): whoever can write the blob store or the database could run
code in the controller. So an artefact is **data only**:

* one **canonical JSON** document (sorted keys, no whitespace, ASCII, no
  NaN / infinity - an infinite conformal ``q`` or OOD threshold is written as
  ``null``);
* every array a ``{"dtype", "shape", "b64"}`` block: the raw little-endian
  bytes (``<f8`` float64 or ``<i8`` int64 only) in base64 - never ``.npy``
  (no ``np.load``, so no ``allow_pickle`` question at all);
* LightGBM as its own text model (``model_to_string``), XGBoost as its own
  JSON model (``save_raw("json")``) - both are parsed by their library, never
  executed as Python;
* ridge = coefficients + intercept, kNN = the standardised training matrix +
  targets, tree / forest = scikit-learn's node arrays (``children_left`` /
  ``children_right`` / ``feature`` / ``threshold`` / leaf ``value``), evaluated
  by :mod:`.serve`'s own numpy code (no scikit-learn at serve time, D359).

Every artefact also carries the preprocessing (:class:`~.prep.Prep` arrays),
the conformal half-width ``q``, the OOD fit (ranges, reference matrix,
threshold, k), the term, algorithm, selected parameters, ``feature_version``,
the feature column order, the dataset digest and the version of the one
library the algorithm needs.

**Version = content digest**: ``sha256:`` + SHA-256 of the canonical bytes
(:func:`digest`); the CAS blob store names the blob by the same digest, so
integrity is checked on every load (:func:`decode` refuses bytes that do not
hash to the expected version). Same dataset digest + same code and library
versions -> byte-identical artefact -> the same version (criterion 3).

:func:`decode` validates the whole structure (types, shapes, index ranges,
tree topology) and raises :class:`ArtefactError` for anything else - a
corrupt, truncated or foreign file is a typed error, never a crash. Pure:
stdlib + numpy.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

import numpy as np
import numpy.typing as npt

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.controller.ml.prep import Prep

__all__ = [
    "ARTEFACT_VERSION",
    "LEARNED_ALGORITHMS",
    "MAX_ARTEFACT_BYTES",
    "Artefact",
    "ArtefactError",
    "OodArrays",
    "TreeArrays",
    "array_block",
    "conformal_block",
    "decode",
    "digest",
    "encode",
    "ood_block",
    "prep_block",
]

ARTEFACT_VERSION: Final = 1
LEARNED_ALGORITHMS: Final = ("ridge", "knn", "tree", "forest", "lightgbm", "xgboost")
MAX_ARTEFACT_BYTES: Final = 512 * 1024 * 1024
"""Refused above this (a kNN matrix of 20 000 x 100 float64 is ~21 MB as
base64)."""
_DTYPES: Final = {"<f8": np.dtype("<f8"), "<i8": np.dtype("<i8")}
_LEAF: Final = -1

type FloatArray = npt.NDArray[np.float64]
type IntArray = npt.NDArray[np.int64]


class ArtefactError(MeshForgeError):
    """The artefact cannot be used: wrong digest, not canonical JSON, a
    missing / mistyped field, an unknown algorithm or ``feature_version``, a
    tree that is not a tree, or its library is not installed
    (``details["reason"]`` names which)."""

    default_failure_type = FailureType.INTEGRITY_ERROR


def _fail(reason: str, message: str) -> ArtefactError:
    return ArtefactError(message, details={"reason": reason})


# ------------------------------------------------------------------ encoding


def array_block(values: npt.ArrayLike, dtype: str = "<f8") -> dict[str, Any]:
    """One array as a JSON block (little-endian raw bytes, base64)."""
    array = np.ascontiguousarray(np.asarray(values), dtype=_DTYPES[dtype])
    return {
        "dtype": dtype,
        "shape": [int(n) for n in array.shape],
        "b64": base64.b64encode(array.tobytes(order="C")).decode("ascii"),
    }


def _finite_or_none(value: float) -> float | None:
    return float(value) if math.isfinite(value) else None


def prep_block(prep: Prep) -> dict[str, Any]:
    """:class:`~.prep.Prep` as JSON blocks."""
    return {
        "medians": array_block(prep.medians),
        "indicator_cols": [int(c) for c in prep.indicator_cols],
        "family_levels": list(prep.family_levels),
        "mean": array_block(prep.mean),
        "std": array_block(prep.std),
    }


def encode(doc: Mapping[str, Any]) -> bytes:
    """The canonical bytes of an artefact document (module docstring);
    ``ValueError`` for a NaN / infinite float (write ``None``)."""
    return json.dumps(
        doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def digest(data: bytes) -> str:
    """``sha256:<hex>`` of ``data`` - the artefact's version."""
    return "sha256:" + hashlib.sha256(data).hexdigest()


# ------------------------------------------------------------------ decoding


@dataclass(frozen=True, slots=True)
class TreeArrays:
    """One regression tree as scikit-learn's node arrays (``-1`` = leaf)."""

    left: IntArray
    right: IntArray
    feature: IntArray
    threshold: FloatArray
    value: FloatArray
    """Per node: the mean target of its training rows (used at leaves)."""


@dataclass(frozen=True, slots=True)
class OodArrays:
    columns: tuple[int, ...]
    low: FloatArray
    high: FloatArray
    """NaN = the training rows never had a value (outside for any value)."""
    prep: Prep
    reference: FloatArray | None
    """The training rows standardised by :attr:`prep` (None = kNN part off)."""
    threshold: float
    k: int


@dataclass(frozen=True, slots=True)
class Artefact:
    """A decoded, validated artefact (no model built yet: :mod:`.serve`)."""

    version: str
    size: int
    term: str
    algorithm: str
    params: dict[str, Any]
    feature_version: int
    features: tuple[str, ...]
    dataset_digest: str
    libraries: dict[str, str | None]
    prep: Prep
    standardise: bool
    q: float
    """Conformal half-width (log space); ``inf`` = no usable interval."""
    alpha: float
    ood: OodArrays
    model: dict[str, Any]
    """Per algorithm (validated): ridge ``coef`` / ``intercept``; knn
    ``reference`` / ``y`` / ``k``; tree ``trees`` (one); forest ``trees``;
    lightgbm ``text`` / ``rounds``; xgboost ``json``."""


def _field(doc: Mapping[str, Any], key: str, kind: type | tuple[type, ...]) -> Any:
    if key not in doc:
        raise _fail("missing_field", f"the artefact has no {key!r}")
    value = doc[key]
    if isinstance(value, bool) and kind is not bool and bool not in _kinds(kind):
        raise _fail("bad_field", f"the artefact's {key!r} is not a {kind}")
    if not isinstance(value, kind):
        raise _fail("bad_field", f"the artefact's {key!r} is not a {kind}")
    return value


def _kinds(kind: type | tuple[type, ...]) -> tuple[type, ...]:
    return kind if isinstance(kind, tuple) else (kind,)


def _array(block: Any, name: str, *, ndim: int, dtype: str = "<f8") -> Any:
    if not isinstance(block, Mapping):
        raise _fail("bad_array", f"{name}: not an array block")
    if block.get("dtype") != dtype:
        raise _fail("bad_array", f"{name}: dtype must be {dtype}")
    shape = block.get("shape")
    if (
        not isinstance(shape, list)
        or len(shape) != ndim
        or not all(isinstance(n, int) and not isinstance(n, bool) for n in shape)
        or any(n < 0 for n in shape)
    ):
        raise _fail("bad_array", f"{name}: bad shape")
    raw = block.get("b64")
    if not isinstance(raw, str):
        raise _fail("bad_array", f"{name}: no data")
    try:
        data = base64.b64decode(raw.encode("ascii"), validate=True)
    except (binascii.Error, ValueError, UnicodeEncodeError):
        raise _fail("bad_array", f"{name}: data is not base64") from None
    item = _DTYPES[dtype].itemsize
    if len(data) != item * math.prod(shape):
        raise _fail("bad_array", f"{name}: {len(data)} bytes do not fit {shape}")
    return np.frombuffer(data, dtype=_DTYPES[dtype]).reshape(shape).copy()


def _float_or_inf(value: Any, name: str) -> float:
    if value is None:
        return math.inf
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _fail("bad_field", f"{name}: not a number")
    if not math.isfinite(value) or value < 0.0:
        raise _fail("bad_field", f"{name}: must be a finite number >= 0 or null")
    return float(value)


def _int(value: Any, name: str, low: int, high: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise _fail("bad_field", f"{name}: not an integer in [{low}, {high}]")
    return value


def _prep(block: Any, width: int, name: str) -> Prep:
    if not isinstance(block, Mapping):
        raise _fail("bad_field", f"{name}: not an object")
    medians = _array(block.get("medians"), f"{name}.medians", ndim=1)
    cols = block.get("indicator_cols")
    levels = block.get("family_levels")
    if (
        not isinstance(cols, list)
        or not all(isinstance(c, int) and not isinstance(c, bool) for c in cols)
        or any(not 0 <= c < width for c in cols)
        or cols != sorted(set(cols))
    ):
        raise _fail("bad_field", f"{name}.indicator_cols: bad column list")
    if not isinstance(levels, list) or not all(isinstance(v, str) for v in levels):
        raise _fail("bad_field", f"{name}.family_levels: not a list of names")
    if medians.shape != (width,):
        raise _fail("bad_array", f"{name}.medians: expected {width} values")
    out_width = width + len(cols) + len(levels)
    mean = _array(block.get("mean"), f"{name}.mean", ndim=1)
    std = _array(block.get("std"), f"{name}.std", ndim=1)
    if mean.shape != (out_width,) or std.shape != (out_width,):
        raise _fail("bad_array", f"{name}: mean / std must have {out_width} values")
    if not (np.isfinite(medians).all() and np.isfinite(mean).all()):
        raise _fail("bad_array", f"{name}: non-finite medians / means")
    if not (np.isfinite(std).all() and (std > 0.0).all()):
        raise _fail("bad_array", f"{name}: std must be finite and > 0")
    return Prep(
        medians=medians,
        indicator_cols=tuple(cols),
        family_levels=tuple(levels),
        mean=mean,
        std=std,
    )


def _tree(block: Any, width: int, name: str) -> TreeArrays:
    if not isinstance(block, Mapping):
        raise _fail("bad_tree", f"{name}: not an object")
    left = _array(block.get("left"), f"{name}.left", ndim=1, dtype="<i8")
    right = _array(block.get("right"), f"{name}.right", ndim=1, dtype="<i8")
    feature = _array(block.get("feature"), f"{name}.feature", ndim=1, dtype="<i8")
    threshold = _array(block.get("threshold"), f"{name}.threshold", ndim=1)
    value = _array(block.get("value"), f"{name}.value", ndim=1)
    n = left.shape[0]
    if n == 0 or any(a.shape != (n,) for a in (right, feature, threshold, value)):
        raise _fail("bad_tree", f"{name}: node arrays of different lengths")
    index = np.arange(n)
    leaf = left == _LEAF
    if not np.array_equal(leaf, right == _LEAF):
        raise _fail("bad_tree", f"{name}: a node with one child")
    inner = ~leaf
    # children come after their parent (scikit-learn's order): no cycle, and
    # every walk ends within n steps
    if (
        (left[inner] <= index[inner]).any()
        or (right[inner] <= index[inner]).any()
        or (left[inner] >= n).any()
        or (right[inner] >= n).any()
    ):
        raise _fail("bad_tree", f"{name}: a child index out of order or range")
    if ((feature[inner] < 0) | (feature[inner] >= width)).any():
        raise _fail("bad_tree", f"{name}: a split feature out of range")
    if not np.isfinite(value[leaf]).all() or np.isnan(threshold[inner]).any():
        raise _fail("bad_tree", f"{name}: non-finite leaf value or NaN threshold")
    return TreeArrays(left, right, feature, threshold, value)


def _model(algorithm: str, block: Any, width: int) -> dict[str, Any]:
    if not isinstance(block, Mapping):
        raise _fail("bad_field", "model: not an object")
    if algorithm == "ridge":
        coef = _array(block.get("coef"), "model.coef", ndim=1)
        if coef.shape != (width,) or not np.isfinite(coef).all():
            raise _fail("bad_array", f"model.coef: expected {width} finite values")
        intercept = block.get("intercept")
        if isinstance(intercept, bool) or not isinstance(intercept, int | float):
            raise _fail("bad_field", "model.intercept: not a number")
        return {"coef": coef, "intercept": float(intercept)}
    if algorithm == "knn":
        reference = _array(block.get("reference"), "model.reference", ndim=2)
        y = _array(block.get("y"), "model.y", ndim=1)
        rows = reference.shape[0]
        if reference.shape[1] != width or y.shape != (rows,) or rows == 0:
            raise _fail("bad_array", "model: reference / y shapes do not match")
        if not (np.isfinite(reference).all() and np.isfinite(y).all()):
            raise _fail("bad_array", "model: non-finite reference / y")
        k = _int(block.get("k"), "model.k", 1, rows)
        return {"reference": reference, "y": y, "k": k}
    if algorithm in ("tree", "forest"):
        trees = block.get("trees")
        if not isinstance(trees, list) or not trees:
            raise _fail("bad_tree", "model.trees: not a non-empty list")
        if algorithm == "tree" and len(trees) != 1:
            raise _fail("bad_tree", "model.trees: a tree artefact holds one tree")
        return {
            "trees": tuple(
                _tree(t, width, f"model.trees[{i}]") for i, t in enumerate(trees)
            )
        }
    if algorithm == "lightgbm":
        text = block.get("text")
        if not isinstance(text, str) or not text.startswith("tree\n"):
            raise _fail("bad_field", "model.text: not a LightGBM text model")
        rounds = _int(block.get("rounds"), "model.rounds", 1, 1_000_000)
        return {"text": text, "rounds": rounds}
    # xgboost
    raw = block.get("json")
    if not isinstance(raw, str) or not raw.startswith('{"learner"'):
        raise _fail("bad_field", "model.json: not an XGBoost JSON model")
    return {"json": raw}


def _ood(block: Any, features: int, name: str = "ood") -> OodArrays:
    if not isinstance(block, Mapping):
        raise _fail("bad_field", f"{name}: not an object")
    cols = block.get("columns")
    if (
        not isinstance(cols, list)
        or not all(isinstance(c, int) and not isinstance(c, bool) for c in cols)
        or any(not 0 <= c < features for c in cols)
    ):
        raise _fail("bad_field", f"{name}.columns: bad column list")
    low = _array(block.get("low"), f"{name}.low", ndim=1)
    high = _array(block.get("high"), f"{name}.high", ndim=1)
    if low.shape != (len(cols),) or high.shape != (len(cols),):
        raise _fail("bad_array", f"{name}: low / high must match the columns")
    prep = _prep(block.get("prep"), features, f"{name}.prep")
    k = _int(block.get("k"), f"{name}.k", 1, 1000)
    reference = None
    if block.get("reference") is not None:
        reference = _array(block.get("reference"), f"{name}.reference", ndim=2)
        if reference.shape[1] != prep.mean.shape[0] or reference.shape[0] <= k:
            raise _fail("bad_array", f"{name}.reference: bad shape")
        if not np.isfinite(reference).all():
            raise _fail("bad_array", f"{name}.reference: non-finite values")
    threshold = _float_or_inf(block.get("threshold"), f"{name}.threshold")
    return OodArrays(tuple(cols), low, high, prep, reference, threshold, k)


def decode(
    data: bytes,
    *,
    expected_version: str | None,
    feature_version: int,
    features: Sequence[str],
) -> Artefact:
    """The validated :class:`Artefact` in ``data`` (module docstring).
    ``expected_version`` (the CAS / registry digest; None only for bytes this
    process just encoded) must equal the bytes' digest; ``feature_version``
    and ``features`` = what this code builds features with."""
    if len(data) > MAX_ARTEFACT_BYTES:
        raise _fail("too_large", f"the artefact is over {MAX_ARTEFACT_BYTES} bytes")
    version = digest(data)
    if expected_version is not None and version != expected_version:
        raise _fail("digest_mismatch", "the artefact does not hash to its version")
    try:
        doc = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise _fail("not_json", "the artefact is not canonical JSON") from None
    if not isinstance(doc, dict):
        raise _fail("not_json", "the artefact is not a JSON object")
    try:
        canonical = encode(doc)
    except ValueError:
        raise _fail("not_canonical", "the artefact holds NaN / infinity") from None
    if canonical != data:
        raise _fail("not_canonical", "the artefact is not in canonical form")
    if _field(doc, "artefact_version", int) != ARTEFACT_VERSION:
        raise _fail("artefact_version", "unknown artefact_version")
    stored_features = _field(doc, "feature_version", int)
    if stored_features != feature_version:
        raise _fail(
            "feature_version",
            f"the artefact has feature_version {stored_features}, this code "
            f"builds {feature_version}",
        )
    names = _field(doc, "features", list)
    if names != list(features):
        raise _fail(
            "features", "the artefact's feature columns differ from this code's"
        )
    algorithm = _field(doc, "algorithm", str)
    if algorithm not in LEARNED_ALGORITHMS:
        raise _fail("algorithm", f"unknown algorithm {algorithm!r}")
    width = len(names)
    prep = _prep(_field(doc, "prep", dict), width, "prep")
    standardise = _field(doc, "standardise", bool)
    model_width = (
        width
        if algorithm in ("lightgbm", "xgboost")
        else width + len(prep.indicator_cols) + len(prep.family_levels)
    )
    conformal = _field(doc, "conformal", dict)
    alpha = conformal.get("alpha")
    if isinstance(alpha, bool) or not isinstance(alpha, float) or not 0 < alpha < 1:
        raise _fail("bad_field", "conformal.alpha: not in (0, 1)")
    libraries = _field(doc, "libraries", dict)
    if not all(
        isinstance(k, str) and (v is None or isinstance(v, str))
        for k, v in libraries.items()
    ):
        raise _fail("bad_field", "libraries: not a name -> version map")
    if prep.family_levels:
        raise _fail("family_features", "family-feature models are an ablation only")
    return Artefact(
        version=version,
        size=len(data),
        term=_field(doc, "term", str),
        algorithm=algorithm,
        params=dict(_field(doc, "params", dict)),
        feature_version=stored_features,
        features=tuple(names),
        dataset_digest=_field(doc, "dataset_digest", str),
        libraries=dict(libraries),
        prep=prep,
        standardise=standardise,
        q=_float_or_inf(conformal.get("q"), "conformal.q"),
        alpha=float(alpha),
        ood=_ood(_field(doc, "ood", dict), width),
        model=_model(algorithm, _field(doc, "model", dict), model_width),
    )


def ood_block(
    *,
    columns: Sequence[int],
    low: FloatArray,
    high: FloatArray,
    prep: Prep,
    reference: FloatArray | None,
    threshold: float,
    k: int,
) -> dict[str, Any]:
    """The OOD fit as JSON blocks."""
    return {
        "columns": [int(c) for c in columns],
        "low": array_block(low),
        "high": array_block(high),
        "prep": prep_block(prep),
        "reference": None if reference is None else array_block(reference),
        "threshold": _finite_or_none(threshold),
        "k": int(k),
    }


def conformal_block(q: float, alpha: float) -> dict[str, Any]:
    return {"q": _finite_or_none(q), "alpha": float(alpha)}
