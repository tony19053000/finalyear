"""Shared preprocessing as plain arrays (V0.15 G2, D349; moved out of
``ml.train`` in G3 so serving - :mod:`..serve`, numpy only - applies the
exact same transform the model was trained with).

LightGBM and XGBoost take NaN natively (:func:`native`). Ridge, kNN and the
scikit-learn tree / forest go through one explicit step **fitted on the
training rows only** (:func:`fit_prep`):

1. **median imputation** per feature (a feature with no value at all in the
   training rows is imputed with 0.0: it is then constant, so the value does
   not matter, and its missing indicator carries the fact);
2. one **missing-indicator** column (1.0 = was NaN) per feature that had any
   NaN in the training rows;
3. optional **family one-hot** (``--with-family`` ablation only, D334): one
   column per family seen in training (a held-out family is all zeros);
4. **standardisation** (ridge / kNN / the OOD distance): mean and standard
   deviation of the training columns (a constant column keeps scale 1).

:class:`Prep` is arrays and tuples only (no pickled scikit-learn
``Pipeline``), so G3 can store it as arrays (D339).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

type FloatArray = npt.NDArray[np.float64]

__all__ = ["Prep", "fit_prep", "native", "transform"]


@dataclass(frozen=True, slots=True)
class Prep:
    medians: FloatArray
    """(features,): the imputation value per feature."""
    indicator_cols: tuple[int, ...]
    """Features that get a missing-indicator column, in order."""
    family_levels: tuple[str, ...]
    """Families with a one-hot column (empty = no family features)."""
    mean: FloatArray
    """Of every output column (imputed, indicators, families)."""
    std: FloatArray
    """Same; 1.0 where the training column was constant."""


def _family_block(
    families: Sequence[str] | None, levels: tuple[str, ...], n: int
) -> FloatArray:
    block = np.zeros((n, len(levels)), dtype=np.float64)
    if levels and families is not None:
        index = {name: j for j, name in enumerate(levels)}
        for i, family in enumerate(families):
            j = index.get(family)
            if j is not None:
                block[i, j] = 1.0
    return block


def _assemble(
    X: FloatArray,
    medians: FloatArray,
    indicator_cols: tuple[int, ...],
    families: Sequence[str] | None,
    levels: tuple[str, ...],
) -> FloatArray:
    missing = np.isnan(X)
    imputed = np.where(missing, medians[np.newaxis, :], X)
    indicators = missing[:, list(indicator_cols)].astype(np.float64)
    return np.hstack([imputed, indicators, _family_block(families, levels, X.shape[0])])


def fit_prep(
    X: FloatArray, families: Sequence[str] | None = None, *, with_family: bool = False
) -> Prep:
    """The preprocessing fitted on training rows ``X`` (module docstring)."""
    medians = np.zeros(X.shape[1], dtype=np.float64)
    for j in range(X.shape[1]):
        present = X[:, j][~np.isnan(X[:, j])]
        if present.size:
            medians[j] = float(np.median(present))
    indicator_cols = tuple(int(j) for j in np.flatnonzero(np.isnan(X).any(axis=0)))
    levels = (
        tuple(sorted(set(families))) if with_family and families is not None else ()
    )
    full = _assemble(X, medians, indicator_cols, families, levels)
    mean = full.mean(axis=0) if full.shape[0] else np.zeros(full.shape[1])
    std = full.std(axis=0) if full.shape[0] else np.ones(full.shape[1])
    std = np.where(std > 0.0, std, 1.0)
    return Prep(
        medians=medians,
        indicator_cols=indicator_cols,
        family_levels=levels,
        mean=mean,
        std=std,
    )


def transform(
    prep: Prep,
    X: FloatArray,
    families: Sequence[str] | None = None,
    *,
    standardise: bool,
) -> FloatArray:
    """``X`` imputed, with indicators (and family columns); standardised on
    request. No NaN in the output."""
    full = _assemble(X, prep.medians, prep.indicator_cols, families, prep.family_levels)
    if standardise:
        full = (full - prep.mean[np.newaxis, :]) / prep.std[np.newaxis, :]
    return full


def native(
    prep: Prep, X: FloatArray, families: Sequence[str] | None = None
) -> FloatArray:
    """``X`` with NaN kept (LightGBM / XGBoost handle it) plus the family
    columns when the prep has any."""
    if not prep.family_levels:
        return X
    return np.hstack([X, _family_block(families, prep.family_levels, X.shape[0])])
