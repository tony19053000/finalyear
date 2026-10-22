"""V0.15 G4: a scriptable :class:`~meshforge.controller.planner.predictor.
TermPredictor` for planner tests (no ML library needed).

:class:`StubPredictor` answers every query from a rule keyed by ``(term,
node_id)`` (then ``(term, None)`` for any node): ``y_hat``, the interval
half-width, the OOD flag. It records every query (its features included) and
counts ``predict`` calls, so tests can check batching, precedence and the
features the planner fed it. ``raises`` makes ``predict`` fail.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field

from meshforge.controller.planner.predictor import (
    MlInput,
    TermPrediction,
    TermQuery,
)

__all__ = ["Rule", "StubPredictor", "stub_ml"]


@dataclass(frozen=True, slots=True)
class Rule:
    y_hat: float = 0.0
    half_width: float = 0.1
    ood: bool = False
    ood_reason: str | None = None


@dataclass(slots=True)
class StubPredictor:
    rules: dict[tuple[str, str | None], Rule] = field(default_factory=dict)
    version: str = "stub-v1"
    raises: BaseException | None = None
    answer_count_delta: int = 0
    """Answer this many rows more (or fewer) than asked (a malformed model)."""
    calls: int = 0
    queries: list[TermQuery] = field(default_factory=list)

    @property
    def predictor_version(self) -> str:
        return self.version

    def has_model(self, term: str) -> bool:
        return any(key[0] == term for key in self.rules)

    def _rule(self, term: str, node_id: str) -> Rule:
        return self.rules.get((term, node_id)) or self.rules[(term, None)]

    def predict(self, rows: Sequence[TermQuery]) -> Sequence[TermPrediction]:
        self.calls += 1
        self.queries.extend(rows)
        if self.raises is not None:
            raise self.raises
        out = []
        for row in rows:
            rule = self._rule(row.term, row.node_id)
            out.append(
                TermPrediction(
                    y_hat=rule.y_hat,
                    lo=rule.y_hat - rule.half_width,
                    hi=rule.y_hat + rule.half_width,
                    ood=rule.ood,
                    reason=rule.ood_reason if rule.ood else None,
                    model_version=self.version,
                )
            )
        if self.answer_count_delta > 0:
            out.extend(out[: self.answer_count_delta])
        elif self.answer_count_delta < 0:
            out = out[: self.answer_count_delta]
        return out


def stub_ml(
    rules: dict[tuple[str, str | None], Rule],
    *,
    max_interval_log: float = math.log(2.0),
    **kwargs: object,
) -> tuple[MlInput, StubPredictor]:
    """An :class:`MlInput` over a :class:`StubPredictor` with the real
    feature builder (``controller/ml/features.term_features``)."""
    from meshforge.controller.ml.features import term_features

    stub = StubPredictor(rules=dict(rules), **kwargs)  # type: ignore[arg-type]
    return (
        MlInput(
            predictor=stub, features=term_features, max_interval_log=max_interval_log
        ),
        stub,
    )
