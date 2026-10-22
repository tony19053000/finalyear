"""The ML planner's training and comparison library (V0.15 G2, D337, D338,
D347-D353; ADR-0021).

Given a G1 dataset file, it decides **per term** whether any learned model
predicts the residual ``y = ln(actual / raw)`` better than the V0.14
calibration on workload families it has never seen - with honest intervals
and an out-of-distribution test - and writes a deterministic report:

* :mod:`.data` - the Parquet file -> arrays (versions checked, rows in the
  canonical order, a content digest independent of the file's row order);
* :mod:`.prep` - the shared preprocessing as plain arrays (D349);
* :mod:`.algorithms` - ridge, kNN, tree, forest, LightGBM, XGBoost and the
  ``raw`` / ``calibration`` baselines behind one ``fit`` / ``predict``
  protocol, each with a small fixed grid (D337, D350, D352);
* :mod:`.select` - the inner grouped k-fold and the one-standard-error rule
  (D347);
* :mod:`.conformal` - split-conformal intervals (D338);
* :mod:`.ood` - the two-part out-of-distribution test (D338);
* :mod:`.metrics` - MALE, MAPE, coverage, the paired family bootstrap (D351);
* :mod:`.compare` - the nested leave-one-family-out loop per term (D347,
  D348) -> :class:`~.compare.TermResult`;
* :mod:`.report` - canonical JSON + Markdown (D352);
* ``python -m meshforge.controller.ml.train compare ...`` - the process
  ``compute planner ml compare`` runs (D353).

**This subpackage is the only place in MeshForge that may import
scikit-learn, LightGBM or XGBoost** (import-linter contract
``ml-libraries-only-in-ml-train`` + an AST test); they come with the
controller's ``ml`` extra (D340). Importing this package itself needs none
of them (:func:`missing_libraries` checks without importing); its modules do.
No pickle / joblib anywhere (D339): G3 stores models as native text / arrays.
"""

from __future__ import annotations

import importlib.util
from typing import Final

__all__ = ["EXTRA_HINT", "REQUIRED_LIBRARIES", "missing_libraries"]

REQUIRED_LIBRARIES: Final = ("numpy", "sklearn", "lightgbm", "xgboost")
"""Import names the ``ml`` extra provides (D340)."""

EXTRA_HINT: Final = (
    "install the controller's ml extra: `uv sync --extra controller --extra ml` "
    "(or `pip install 'meshforge[controller,ml]'`)"
)


def missing_libraries() -> list[str]:
    """The :data:`REQUIRED_LIBRARIES` that are not importable (found by
    ``importlib.util.find_spec``, nothing imported)."""
    return [
        name for name in REQUIRED_LIBRARIES if importlib.util.find_spec(name) is None
    ]
