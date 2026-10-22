"""Split-conformal intervals (V0.15 G2, D338).

From ``n`` out-of-fold absolute residuals ``r`` (:mod:`.select`: each from a
model that never saw that row's fingerprint), the half-width is

    q = the ceil((n + 1)(1 - alpha))-th smallest r      (alpha = 0.1)

and the interval of a new prediction is ``y_hat +- q`` in log space (a factor
``exp(q)`` either way on seconds). Model-agnostic and distribution-free:
under exchangeability, ``P(|y - y_hat| <= q) >= 1 - alpha``. When that rank
exceeds ``n`` (too few residuals for the level) ``q`` is infinite - an
interval that never makes the model usable.

The **width** reported and gated (``ml_max_interval_log``, D338) is the full
width ``2 q`` (default limit ln 2: the interval spans at most a factor 2).
"""

from __future__ import annotations

import math
from typing import Final

import numpy as np

from meshforge.controller.ml.train.data import FloatArray

__all__ = ["ALPHA", "conformal_q", "covered"]

ALPHA: Final = 0.1


def conformal_q(residuals: FloatArray, alpha: float = ALPHA) -> float:
    """The half-width ``q`` (module docstring); ``inf`` for no residuals or
    too few for the level."""
    if not 0.0 < alpha < 1.0:
        raise ValueError("alpha must be in (0, 1)")
    values = np.sort(np.abs(residuals[np.isfinite(residuals)]))
    n = int(values.size)
    rank = math.ceil((n + 1) * (1.0 - alpha))
    if n == 0 or rank > n:
        return math.inf
    return float(values[rank - 1])


def covered(y: FloatArray, y_hat: FloatArray, q: float) -> FloatArray:
    """1.0 where ``y`` lies in ``y_hat +- q``, else 0.0."""
    return (np.abs(y - y_hat) <= q).astype(np.float64)
