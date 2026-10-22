"""Re-plan a stored decision with the model it was planned with (V0.15 G4,
D364, Q23).

``plan(*from_replay(r), policy, calibration=calibration_from_replay(r))``
reproduces a V0.14 decision. A decision planned with an ML model also needs
**that** model: :func:`replay_decision` reads the replay's ``ml`` key
(:func:`~meshforge.controller.planner.replay.ml_from_replay`) and asks
``load`` for the version.

* no ``ml`` key -> the V0.14 replay (no model);
* the version loads -> re-planned with it (the same model gives the same
  corrections, so the same decision - field for field);
* the decision **fell back** (its predictor failed, D363: ``ml.fallback``) ->
  re-planned in the same fallback, which never asks the model, so it needs
  none (the version is only named);
* the version is gone -> ``ml_model_unavailable`` and **no decision**: a
  replay that silently re-planned with calibration would claim to reproduce
  a decision it does not (Q23).

``load`` is the caller's (G3's registry loader; an in-memory dict in tests);
it returns None for an unknown version and must not raise for one. Pure
otherwise: no database, no clock, no logging.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from meshforge.controller.planner.calibration import Calibration
from meshforge.controller.planner.plan import plan
from meshforge.controller.planner.predictor import (
    MlInput,
    TermPrediction,
    TermQuery,
)
from meshforge.controller.planner.replay import (
    calibration_from_replay,
    from_replay,
    ml_from_replay,
)
from meshforge.controller.planner.types import Decision

__all__ = [
    "ML_MODEL_UNAVAILABLE",
    "ReplayResult",
    "replay_decision",
]

ML_MODEL_UNAVAILABLE: Final = "ml_model_unavailable"


@dataclass(frozen=True, slots=True)
class ReplayResult:
    decision: Decision | None
    """None = not re-planned (:attr:`status`)."""
    status: str
    """``ok`` | :data:`ML_MODEL_UNAVAILABLE`."""
    ml_model_version: str | None = None
    """The version the stored decision used (None = no model)."""


class _Named:
    """A predictor that only names a version (a fallback replay never asks
    the model)."""

    def __init__(self, version: str) -> None:
        self._version = version

    @property
    def predictor_version(self) -> str:
        return self._version

    def has_model(self, term: str) -> bool:
        return False

    def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
        raise AssertionError("a fallback replay never asks the model")


def _never_called(*_args: Any) -> tuple[float, ...]:
    raise AssertionError("a fallback replay builds no features")


def replay_decision(
    replay: Mapping[str, Any],
    policy: str,
    *,
    load: Callable[[str], MlInput | None],
    calibration: Calibration | None = None,
    use_stored_calibration: bool = True,
) -> ReplayResult:
    """The stored decision planned again (module docstring). ``calibration``
    is used when ``use_stored_calibration`` is False (an evaluator's
    held-out calibration); else the replay's own block. Raises
    :class:`~meshforge.controller.planner.replay.ReplayError` for an
    unreadable replay."""
    request, snapshot, now = from_replay(replay)
    used = ml_from_replay(replay)
    factors = calibration_from_replay(replay) if use_stored_calibration else calibration
    if used is None:
        return ReplayResult(
            plan(request, snapshot, policy, now, calibration=factors), "ok"
        )
    if used.error is not None:
        loaded = load(used.version)
        base = (
            loaded
            if loaded is not None and loaded.version == used.version
            else MlInput(predictor=_Named(used.version), features=_never_called)
        )
        ml = dataclasses.replace(base, fallback=used.error)
    else:
        found = load(used.version)
        if found is None or found.version != used.version:
            return ReplayResult(None, ML_MODEL_UNAVAILABLE, used.version)
        ml = found
    decision = plan(request, snapshot, policy, now, calibration=factors, ml=ml)
    return ReplayResult(decision, "ok", used.version)
