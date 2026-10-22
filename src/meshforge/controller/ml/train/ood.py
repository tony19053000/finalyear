"""The two-part out-of-distribution test (V0.15 G2, D338).

Fitted on training rows only (:func:`fit_ood`); a row is OOD when either
part fires (:meth:`OodModel.check`):

a. **range** - any ``hardware`` or ``job_size`` feature outside ``[min - m x
   span, max + m x span]`` of the training values (``m`` = 10 %). A NaN in
   the checked row is ignored (unknown is not "outside"); a feature the
   training rows never had a value for is outside for any value
   (``range:<feature>:unseen``). Catches "a new laptop" and "a new job size".
b. **kNN distance** - the mean distance to the ``k`` (5) nearest training
   rows in the standardised, imputed feature space (:mod:`.prep`, every
   numeric feature) above the ``p``-th (99th) percentile of the training
   rows' own leave-one-out mean distances. Catches a combination never seen
   even when each feature is in range. Off with ``k`` or fewer training rows.

The reason is the first part that fired (``range:<feature>`` for the first
feature in schema order, else ``knn``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import numpy as np
from sklearn.neighbors import NearestNeighbors

from meshforge.controller.ml.schema import NUMERIC_FEATURES, FeatureGroup
from meshforge.controller.ml.train.data import FloatArray, feature_indices
from meshforge.controller.ml.train.prep import Prep, fit_prep, transform

__all__ = ["OOD_K", "OOD_PERCENTILE", "RANGE_MARGIN", "OodModel", "fit_ood"]

RANGE_MARGIN: Final = 0.10
OOD_K: Final = 5
OOD_PERCENTILE: Final = 99.0
RANGE_GROUPS: Final = (FeatureGroup.HARDWARE, FeatureGroup.JOB_SIZE)


@dataclass(frozen=True, slots=True)
class OodModel:
    columns: tuple[int, ...]
    """Range-checked feature indices."""
    low: FloatArray
    high: FloatArray
    """Per checked column; NaN = the training rows never had a value."""
    prep: Prep
    neighbours: Any
    """A fitted ``NearestNeighbors`` (None = the kNN part is off)."""
    threshold: float
    k: int

    def check(self, X: FloatArray) -> tuple[np.ndarray, list[str | None]]:
        """``(ood flags, reasons)`` for feature rows ``X``."""
        n = X.shape[0]
        flags = np.zeros(n, dtype=bool)
        reasons: list[str | None] = [None] * n
        for col_pos, col in enumerate(self.columns):
            values = X[:, col]
            present = ~np.isnan(values)
            low, high = self.low[col_pos], self.high[col_pos]
            if np.isnan(low):
                outside = present
                label = f"range:{NUMERIC_FEATURES[col]}:unseen"
            else:
                outside = present & ((values < low) | (values > high))
                label = f"range:{NUMERIC_FEATURES[col]}"
            for i in np.flatnonzero(outside & ~flags):
                reasons[int(i)] = label
            flags |= outside
        if self.neighbours is not None and n:
            Z = transform(self.prep, X, standardise=True)
            distances, _ = self.neighbours.kneighbors(Z, n_neighbors=self.k)
            far = distances.mean(axis=1) > self.threshold
            for i in np.flatnonzero(far & ~flags):
                reasons[int(i)] = "knn"
            flags |= far
        return flags, reasons


def fit_ood(
    X: FloatArray,
    *,
    margin: float = RANGE_MARGIN,
    k: int = OOD_K,
    percentile: float = OOD_PERCENTILE,
) -> OodModel:
    """The test fitted on training feature rows ``X`` (module docstring)."""
    columns = feature_indices(*RANGE_GROUPS)
    low = np.full(len(columns), np.nan)
    high = np.full(len(columns), np.nan)
    for pos, col in enumerate(columns):
        present = X[:, col][~np.isnan(X[:, col])]
        if present.size:
            lo, hi = float(present.min()), float(present.max())
            span = hi - lo
            low[pos], high[pos] = lo - margin * span, hi + margin * span
    prep = fit_prep(X)
    neighbours: Any = None
    threshold = np.inf
    if X.shape[0] > k:
        Z = transform(prep, X, standardise=True)
        neighbours = NearestNeighbors(n_neighbors=k, algorithm="brute", n_jobs=1).fit(Z)
        distances, indices = neighbours.kneighbors(Z, n_neighbors=k + 1)
        own = indices == np.arange(X.shape[0])[:, np.newaxis]
        # leave one out: drop the row itself (or, when it is not among the
        # k + 1 because of exact duplicates, the farthest of them)
        drop = np.where(own.any(axis=1), own.argmax(axis=1), k)
        keep = np.ones_like(own)
        keep[np.arange(X.shape[0]), drop] = False
        loo = distances[keep].reshape(X.shape[0], k).mean(axis=1)
        threshold = float(np.percentile(loo, percentile))
    return OodModel(
        columns=columns,
        low=low,
        high=high,
        prep=prep,
        neighbours=neighbours,
        threshold=threshold,
        k=k,
    )
