"""The shared preprocessing (V0.15 G2, D349) - lives in
:mod:`meshforge.controller.ml.prep` since G3 (serving applies the same
transform without importing the training package); re-exported here for the
training modules."""

from __future__ import annotations

from meshforge.controller.ml.prep import Prep, fit_prep, native, transform

__all__ = ["Prep", "fit_prep", "native", "transform"]
