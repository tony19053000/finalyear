"""V0.15 G3 + G4 merge: the loaded model set as the planner's predictor.

* :class:`SetPredictor` - ``predictor_version`` = the set version, each
  answer's ``model_version`` = its member; a ``none`` member (and one
  without a finite conformal ``q``) is ``has_model`` False; answers equal
  :meth:`LoadedModel.predict` on the **raw** (pre-prep) feature rows the
  planner builds (the model applies its own prep);
* an :class:`ArtefactError` / a wrong row width while predicting is
  :class:`PredictorFailed` (D363: the decision falls back to calibration);
* :func:`ml_input` warms a real set up (NaN / zero / mixed raw rows) and
  answers an :class:`MlInput` under the budget;
* :class:`RegistryMlInputSource` builds the input once per loaded set and
  answers None without a set; a set of a stale feature version is skipped
  quietly by :class:`ModelSource` (one info line, no error).
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from typing import Any

import pytest
import structlog

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
import numpy as np
from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.predictor import BudgetedPredictor, ml_input
from meshforge.controller.ml.schema import FEATURE_VERSION, NUMERIC_FEATURES
from meshforge.controller.ml.serve import LoadedModel, LoadedSet, load_model
from meshforge.controller.ml.source import (
    STALE_FEATURES,
    ModelSource,
    RegistryMlInputSource,
    SetPredictor,
)
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.planner.predictor import PredictorFailed, TermQuery

pytestmark = pytest.mark.unit

FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
W = len(NUMERIC_FEATURES)


@pytest.fixture(scope="module")
def loaded() -> LoadedSet:
    rows = synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=10, seed=701
    )
    table = table_from_rows(rows)
    result = train_set(table, FAST)
    models = {
        m.term: load_model(m.artefact, expected_version=m.version)
        for m in result.members
        if m.artefact
    }
    assert set(models) == {"compute"}
    return LoadedSet(
        set_version="sha256:" + "a" * 64,
        feature_version=FEATURE_VERSION,
        models=models,
        without_model={"env": "insufficient_data"},
    )


def _rows(n: int, seed: int = 0) -> list[tuple[float, ...]]:
    rng = np.random.default_rng(seed)
    out = []
    for i in range(n):
        row = rng.uniform(0.0, 50.0, W)
        row[i % W] = math.nan  # raw rows carry NaN = missing (prep imputes)
        out.append(tuple(float(v) for v in row))
    return out


def test_answers_are_the_members_on_raw_rows(loaded: LoadedSet) -> None:
    predictor = SetPredictor(loaded)
    assert predictor.predictor_version == loaded.set_version
    assert predictor.has_model("compute")
    assert not predictor.has_model("env")  # a `none` member
    assert not predictor.has_model("combine")  # not in the set at all
    rows = _rows(7)
    answers = predictor.predict(
        [TermQuery("compute", f"n{i}", r) for i, r in enumerate(rows)]
    )
    direct = loaded.models["compute"].predict(np.asarray(rows, dtype=np.float64))
    assert len(answers) == 7
    for k, answer in enumerate(answers):
        assert answer.y_hat == float(direct.y_hat[k])
        assert answer.lo == float(direct.lo[k]) and answer.hi == float(direct.hi[k])
        assert answer.ood == bool(direct.ood[k])
        assert answer.reason == direct.reasons[k]
        assert answer.model_version == loaded.models["compute"].version
    assert predictor.predict([]) == []


def test_a_member_without_a_finite_interval_is_no_model(loaded: LoadedSet) -> None:
    model = loaded.models["compute"]
    endless = LoadedModel(replace(model.artefact, q=math.inf))
    predictor = SetPredictor(replace(loaded, models={"compute": endless}))
    assert not predictor.has_model("compute")
    with pytest.raises(PredictorFailed, match="no_model:compute"):
        predictor.predict([TermQuery("compute", "n", _rows(1)[0])])


def test_artefact_errors_and_bad_rows_are_predictor_failed(
    loaded: LoadedSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    predictor = SetPredictor(loaded)
    with pytest.raises(PredictorFailed, match="malformed_features"):
        predictor.predict([TermQuery("compute", "n", (1.0,) * (W - 1))])

    def broken(_self: Any, _x: Any) -> Any:
        raise ArtefactError("native", details={"reason": "bad_model"})

    monkeypatch.setattr(LoadedModel, "predict", broken)
    with pytest.raises(PredictorFailed) as caught:
        predictor.predict([TermQuery("compute", "n", _rows(1)[0])])
    assert caught.value.cause == "artefact_error:bad_model"


def test_ml_input_warms_a_real_set_up(loaded: LoadedSet) -> None:
    ml = ml_input(SetPredictor(loaded), max_interval_log=0.5, budget_ms=5000.0)
    assert ml is not None and ml.version == loaded.set_version
    assert isinstance(ml.predictor, BudgetedPredictor)
    assert ml.predictor.budget_ms == 5000.0 and ml.max_interval_log == 0.5


class _Models:
    """A :class:`ModelSource` stand-in: ``refresh`` answers a set."""

    def __init__(self, loaded: LoadedSet | None) -> None:
        self.loaded = loaded
        self.calls = 0

    async def refresh(self) -> LoadedSet | None:
        self.calls += 1
        return self.loaded


def test_registry_source_builds_once_per_set(
    loaded: LoadedSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []
    real = ml_input

    def counting(predictor: Any, **kwargs: Any) -> Any:
        built.append(predictor.predictor_version)
        assert kwargs == {"max_interval_log": 0.4, "budget_ms": 33.0}
        return real(predictor, **kwargs)

    monkeypatch.setattr("meshforge.controller.ml.source.ml_input", counting)
    models = _Models(loaded)
    source = RegistryMlInputSource(models, max_interval_log=0.4, budget_ms=33.0)  # type: ignore[arg-type]

    async def run() -> None:
        first = await source.current()
        assert first is not None and first.version == loaded.set_version
        assert await source.current() is first  # same set: not rebuilt
        models.loaded = None
        assert await source.current() is None
        models.loaded = loaded
        assert await source.current() is not None

    asyncio.run(run())
    assert built == [loaded.set_version, loaded.set_version]


def test_a_failing_warm_up_means_no_model(
    loaded: LoadedSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(_self: Any, _x: Any) -> Any:
        raise ArtefactError("native", details={"reason": "bad_model"})

    monkeypatch.setattr(LoadedModel, "predict", broken)
    source = RegistryMlInputSource(_Models(loaded))  # type: ignore[arg-type]
    with structlog.testing.capture_logs() as logs:
        assert asyncio.run(source.current()) is None
        assert asyncio.run(source.current()) is None  # cached: no new warm-up
    assert [e["event"] for e in logs].count("controller.ml.model_warm_up_failed") == 1


class _Row:
    def __init__(self, version: str, feature_version: int) -> None:
        self.set_version = version
        self.feature_version = feature_version
        self.status = "active"
        self.reason = None


class _StaleRegistry:
    def __init__(self) -> None:
        self.loads = 0

    async def serving_version(self) -> tuple[str, str]:
        return "active", "sha256:" + "b" * 64

    async def get(self, version: str) -> _Row:
        return _Row(version, FEATURE_VERSION - 1)

    async def load(self, row: _Row) -> LoadedSet:
        self.loads += 1
        raise AssertionError("a stale set is never loaded")


def test_a_stale_feature_version_is_skipped_quietly() -> None:
    registry = _StaleRegistry()
    source = ModelSource(registry, "auto")  # type: ignore[arg-type]

    async def run() -> None:
        for _ in range(5):
            assert await source.refresh() is None

    with structlog.testing.capture_logs() as logs:
        asyncio.run(run())
    assert registry.loads == 0
    assert source.unavailable == STALE_FEATURES and source.kind == "active"
    events = [(e["event"], e["log_level"]) for e in logs]
    assert events == [("controller.planner.ml_set_stale_features", "info")]
