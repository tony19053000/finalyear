"""Serving a stored model: an artefact -> a predictor (V0.15 G3, D359).

:func:`load_model` turns artefact bytes (:mod:`.artefact`, digest checked)
into a :class:`LoadedModel`; :meth:`LoadedModel.predict` answers, for feature
rows in :data:`~.schema.NUMERIC_FEATURES` order (NaN = missing):

* ``y_hat`` - the residual ``ln(actual / raw)`` the term's analytical raw
  prediction is corrected by (``raw x e^y_hat``);
* ``lo`` / ``hi`` = ``y_hat -+ q`` - the split-conformal interval (``q``
  infinite = no usable interval);
* ``ood`` + ``reason`` - the D338 two-part test exactly as fitted: a
  ``hardware`` / ``job_size`` feature outside the training range +- 10 % of
  its span (``range:<feature>``, ``range:<feature>:unseen``), else the mean
  distance to the ``k`` nearest training rows (standardised) above the 99th
  percentile of the training rows' own (``knn``).

**What serving needs** (D359): numpy for ridge, kNN, tree and forest - the
tree / forest are evaluated by :func:`_tree_predict` over the exported node
arrays exactly as scikit-learn does (features cast to float32, ``x <=
threshold`` goes left; a forest sums its trees in order, then divides) -
and ``lightgbm`` / ``xgboost`` only for those two algorithms, imported on
first use (a controller without the ``ml`` extra loads every other model;
those two fail with :class:`~.artefact.ArtefactError` ``library_missing``).
**Never scikit-learn, never unpickling** (import-linter contract
``ml-serving-imports``, AST test).

The kNN neighbour search (regression and OOD) computes squared distances as
``|r|^2 + |z|^2 - 2 r.z`` for all rows at once (one matrix product), takes
the ``k + 8`` nearest by that, recomputes their exact distances and keeps
the ``k`` nearest by (exact distance, row index) - deterministic ties.

:class:`LoadedSet` = the members of one ``ml_model_set`` (D358): the loaded
models by term plus the terms that have no learned model (and why).
"""

from __future__ import annotations

import importlib.util
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import numpy as np

from meshforge.controller.ml.artefact import (
    Artefact,
    ArtefactError,
    FloatArray,
    OodArrays,
    TreeArrays,
    decode,
)
from meshforge.controller.ml.prep import transform
from meshforge.controller.ml.schema import FEATURE_VERSION, NUMERIC_FEATURES

__all__ = [
    "LoadedModel",
    "LoadedSet",
    "TermOutput",
    "library_available",
    "load_model",
]

_EXTRA_CANDIDATES: Final = 8
_LIBRARY: Final = {"lightgbm": "lightgbm", "xgboost": "xgboost"}


def library_available(algorithm: str) -> bool:
    """Whether ``algorithm`` can be served here (numpy-only ones always)."""
    name = _LIBRARY.get(algorithm)
    return name is None or importlib.util.find_spec(name) is not None


@dataclass(frozen=True, slots=True)
class TermOutput:
    """One :meth:`LoadedModel.predict` answer, per row."""

    y_hat: FloatArray
    lo: FloatArray
    hi: FloatArray
    ood: np.ndarray
    reasons: tuple[str | None, ...]
    model_version: str


# ------------------------------------------------------------------ kNN


class _Neighbours:
    """The ``k`` nearest reference rows (module docstring)."""

    def __init__(self, reference: FloatArray, k: int) -> None:
        self.reference = reference
        self.k = k
        self.norms = np.einsum("ij,ij->i", reference, reference)

    def search(self, Z: FloatArray) -> tuple[FloatArray, np.ndarray]:
        """``(distances, indices)``, each (rows, k), nearest first."""
        n_ref = self.reference.shape[0]
        k = self.k
        approx = (
            self.norms[np.newaxis, :]
            + np.einsum("ij,ij->i", Z, Z)[:, np.newaxis]
            - 2.0 * (Z @ self.reference.T)
        )
        take = min(n_ref, k + _EXTRA_CANDIDATES)
        if take < n_ref:
            candidates = np.argpartition(approx, take - 1, axis=1)[:, :take]
        else:
            candidates = np.broadcast_to(np.arange(n_ref), (Z.shape[0], n_ref))
        distances = np.empty((Z.shape[0], k), dtype=np.float64)
        indices = np.empty((Z.shape[0], k), dtype=np.intp)
        for row in range(Z.shape[0]):
            cand = np.sort(candidates[row])
            diff = self.reference[cand] - Z[row]
            exact = np.sqrt(np.einsum("ij,ij->i", diff, diff))
            order = np.lexsort((cand, exact))[:k]
            distances[row] = exact[order]
            indices[row] = cand[order]
        return distances, indices


# ------------------------------------------------------------------ trees


def _tree_predict(tree: TreeArrays, X32: np.ndarray) -> FloatArray:
    """scikit-learn's ``tree_.predict`` over node arrays: ``X32`` is the
    float32 copy scikit-learn compares (the threshold stays float64)."""
    n = X32.shape[0]
    node = np.zeros(n, dtype=np.intp)
    rows = np.arange(n)
    for _ in range(tree.left.shape[0]):
        inner = tree.left[node] != -1
        if not inner.any():
            break
        feature = np.where(inner, tree.feature[node], 0)
        go_left = X32[rows, feature] <= tree.threshold[node]
        node = np.where(
            inner, np.where(go_left, tree.left[node], tree.right[node]), node
        )
    return np.asarray(tree.value[node], dtype=np.float64)


# ------------------------------------------------------------------ the model


class LoadedModel:
    """One term's model, ready to predict (module docstring)."""

    def __init__(self, artefact: Artefact) -> None:
        self.artefact = artefact
        self.version = artefact.version
        self.term = artefact.term
        self.algorithm = artefact.algorithm
        self.q = artefact.q
        self._ood = artefact.ood
        self._ood_search = (
            None
            if artefact.ood.reference is None
            else _Neighbours(artefact.ood.reference, artefact.ood.k)
        )
        self._knn = (
            _Neighbours(artefact.model["reference"], artefact.model["k"])
            if artefact.algorithm == "knn"
            else None
        )
        self._booster = self._load_booster(artefact)

    @staticmethod
    def _load_booster(artefact: Artefact) -> Any:
        if artefact.algorithm not in _LIBRARY:
            return None
        if not library_available(artefact.algorithm):
            raise ArtefactError(
                f"{artefact.algorithm} is not installed (the controller's ml extra)",
                details={"reason": "library_missing"},
            )
        try:
            if artefact.algorithm == "lightgbm":
                import lightgbm as lgb  # only for this algorithm (D359)

                return lgb.Booster(model_str=artefact.model["text"])
            import xgboost as xgb  # only for this algorithm (D359)

            booster = xgb.Booster()
            booster.load_model(bytearray(artefact.model["json"].encode("utf-8")))
            booster.set_param({"nthread": 1})
            return booster  # noqa: TRY300 - one try covers both parsers
        except Exception as exc:  # noqa: BLE001 - any parse failure is one answer
            raise ArtefactError(
                f"the {artefact.algorithm} model cannot be parsed",
                details={"reason": "bad_model", "cause": type(exc).__name__},
            ) from None

    def predict_residual(self, X: FloatArray) -> FloatArray:
        """``y_hat`` only (the round-trip test compares this)."""
        art = self.artefact
        model = art.model
        if art.algorithm in ("lightgbm", "xgboost"):
            if art.algorithm == "lightgbm":
                out = self._booster.predict(X, num_threads=1)
            else:
                import xgboost as xgb  # only for this algorithm (D359)

                out = self._booster.predict(xgb.DMatrix(X, missing=np.nan, nthread=1))
            return np.asarray(out, dtype=np.float64)
        Z = transform(art.prep, X, standardise=art.standardise)
        if art.algorithm == "ridge":
            return np.asarray(Z @ model["coef"] + model["intercept"], dtype=np.float64)
        if art.algorithm == "knn":
            if self._knn is None:  # built in __init__ for every knn artefact
                raise ArtefactError("no kNN index", details={"reason": "bad_model"})
            _, indices = self._knn.search(Z)
            return np.asarray(model["y"][indices].mean(axis=1), dtype=np.float64)
        X32 = Z.astype(np.float32)
        total = np.zeros(X.shape[0], dtype=np.float64)
        for tree in model["trees"]:
            total += _tree_predict(tree, X32)
        return total / len(model["trees"])

    def check_ood(self, X: FloatArray) -> tuple[np.ndarray, list[str | None]]:
        """The D338 test as fitted (module docstring)."""
        ood: OodArrays = self._ood
        n = X.shape[0]
        flags = np.zeros(n, dtype=bool)
        reasons: list[str | None] = [None] * n
        for pos, col in enumerate(ood.columns):
            values = X[:, col]
            present = ~np.isnan(values)
            low, high = ood.low[pos], ood.high[pos]
            if np.isnan(low):
                outside = present
                label = f"range:{NUMERIC_FEATURES[col]}:unseen"
            else:
                outside = present & ((values < low) | (values > high))
                label = f"range:{NUMERIC_FEATURES[col]}"
            for i in np.flatnonzero(outside & ~flags):
                reasons[int(i)] = label
            flags |= outside
        if self._ood_search is not None and n:
            Z = transform(ood.prep, X, standardise=True)
            distances, _ = self._ood_search.search(Z)
            far = distances.mean(axis=1) > ood.threshold
            for i in np.flatnonzero(far & ~flags):
                reasons[int(i)] = "knn"
            flags |= far
        return flags, reasons

    def predict(self, X: FloatArray) -> TermOutput:
        """Every answer for feature rows ``X`` (module docstring)."""
        X = np.asarray(X, dtype=np.float64)
        if X.ndim != 2 or X.shape[1] != len(NUMERIC_FEATURES):
            raise ValueError(
                f"feature rows must be (n, {len(NUMERIC_FEATURES)}), got {X.shape}"
            )
        y_hat = self.predict_residual(X)
        flags, reasons = self.check_ood(X)
        return TermOutput(
            y_hat=y_hat,
            lo=y_hat - self.q,
            hi=y_hat + self.q,
            ood=flags,
            reasons=tuple(reasons),
            model_version=self.version,
        )


def load_model(data: bytes, *, expected_version: str | None) -> LoadedModel:
    """``data`` decoded and validated (:func:`~.artefact.decode`, this
    code's feature version and columns) and built;
    :class:`~.artefact.ArtefactError` for anything that cannot serve."""
    return LoadedModel(
        decode(
            data,
            expected_version=expected_version,
            feature_version=FEATURE_VERSION,
            features=NUMERIC_FEATURES,
        )
    )


@dataclass(frozen=True, slots=True)
class LoadedSet:
    """One model set's members (D358), loaded."""

    set_version: str
    feature_version: int
    models: Mapping[str, LoadedModel]
    """Term -> its learned model."""
    without_model: Mapping[str, str] = field(default_factory=dict)
    """Term -> why the set has no learned model for it."""

    @property
    def terms(self) -> Sequence[str]:
        return sorted(self.models)

    def model(self, term: str) -> LoadedModel | None:
        return self.models.get(term)
