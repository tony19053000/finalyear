"""V0.15 G4: the in-memory predictor over G2's shipped candidates (D336,
D347), and the planner using a **real** trained model end to end.

* a synthetic-truth dataset (``signal``: a learnable residual) trains a
  learned model for ``compute``; ``node_noise`` (calibration is the truth)
  gives no model -> ``has_model`` False -> the planner says ``no_model``;
* predictions: ``y_hat +- q`` with the all-row conformal ``q``, OOD flags
  and reasons from the D338 test; one model call per term per batch;
* the version is a content digest: same table -> same version;
* ``plan`` with it (through ``ml_input``, the real feature builder) decides,
  records ``detail.ml`` with a known reason on every learnable term, and an
  out-of-range fabric is OOD (calibration kept).
"""

from __future__ import annotations

import math

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows
from mf_planner_fabrics import synthetic_fabric

from meshforge.controller.ml.predictor import ml_input
from meshforge.controller.ml.schema import NUMERIC_FEATURES
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.memory import InMemoryPredictor, train_in_memory
from meshforge.controller.planner import plan
from meshforge.controller.planner.predictor import MlReason, TermQuery
from meshforge.controller.planner.replay import project

pytestmark = pytest.mark.unit

SETTINGS = CompareSettings(terms=("compute",), algorithms=("ridge", "lightgbm"))


def _signal() -> InMemoryPredictor:
    rows = synthetic_rows("signal", families=5, groups_per_family=5, runs_per_group=10)
    return train_in_memory(table_from_rows(rows), SETTINGS)


@pytest.fixture(scope="module")
def signal() -> InMemoryPredictor:
    return _signal()


def _row(load: float, gflops: float, size: float = 1e7) -> tuple[float, ...]:
    values = dict.fromkeys(NUMERIC_FEATURES, math.nan)
    values.update(
        hw_cpu_gflops=gflops,
        hw_cpu_threads=8.0,
        hw_ram_bytes=16.0 * 2**30,
        hw_gpu_count=0.0,
        load_cpu_percent=load,
        load_live_attempts=1.0,
        job_input_bytes=size,
        job_cpus=1.0,
        job_lanes=1.0,
        an_raw_s=30.0,
        an_src_assumed=1.0,
        an_src_measured=0.0,
        an_src_history=0.0,
        an_src_scaled=0.0,
    )
    return tuple(values[n] for n in NUMERIC_FEATURES)


def test_a_learnable_residual_trains_a_model(signal: InMemoryPredictor) -> None:
    assert signal.has_model("compute")
    assert not signal.has_model("env")
    model = signal.models["compute"]
    assert model.config.startswith(("ridge", "lightgbm"))
    assert math.isfinite(model.q) and model.q > 0.0
    assert signal.predictor_version.startswith("mem-")


def test_predictions_follow_the_truth_with_the_conformal_interval(
    signal: InMemoryPredictor,
) -> None:
    rows = [
        TermQuery("compute", "n1", _row(load=10.0, gflops=140.0)),
        TermQuery("compute", "n2", _row(load=90.0, gflops=140.0)),
    ]
    low, high = signal.predict(rows)
    assert signal.calls == 1
    assert high.y_hat > low.y_hat + 0.3  # truth: 0.9 x load / 100
    q = signal.models["compute"].q
    for p in (low, high):
        assert p.hi - p.lo == pytest.approx(2 * q)
        assert not p.ood and p.reason is None
        assert p.model_version == signal.predictor_version


def test_out_of_range_rows_are_ood(signal: InMemoryPredictor) -> None:
    (far,) = signal.predict([TermQuery("compute", "n", _row(50.0, 140.0, size=1e13))])
    assert far.ood and far.reason == "range:job_input_bytes"


def test_the_version_is_a_content_digest(signal: InMemoryPredictor) -> None:
    assert _signal().predictor_version == signal.predictor_version
    other = synthetic_rows(
        "signal", families=5, groups_per_family=5, runs_per_group=10, seed=2
    )
    assert (
        train_in_memory(table_from_rows(other), SETTINGS).predictor_version
        != signal.predictor_version
    )


def test_a_baseline_pick_ships_no_model() -> None:
    """Only the baselines to choose from -> the pick is ``raw`` or
    ``calibration`` -> no learned model (the planner keeps calibration)."""
    rows = synthetic_rows("signal", families=5, groups_per_family=5, runs_per_group=10)
    baselines = CompareSettings(terms=("compute",), algorithms=())
    predictor = train_in_memory(table_from_rows(rows), baselines)
    assert not predictor.has_model("compute")


def test_under_the_floor_no_model() -> None:
    rows = synthetic_rows("signal", families=2, groups_per_family=3, runs_per_group=5)
    assert train_in_memory(table_from_rows(rows), SETTINGS).models == {}


def test_bad_rows_are_errors(signal: InMemoryPredictor) -> None:
    with pytest.raises(ValueError, match="length"):
        signal.predict([TermQuery("compute", "n", (1.0,))])
    with pytest.raises(KeyError):
        signal.predict([TermQuery("env", "n", _row(1.0, 100.0))])


def test_the_planner_uses_a_real_model_end_to_end(signal: InMemoryPredictor) -> None:
    request, snapshot, now = synthetic_fabric(4, split=False, seed=4)
    view = project(request, snapshot)
    decision = plan(
        request, view, "cost_model", now, ml=ml_input(signal, budget_ms=None)
    )
    assert decision.chosen is not None
    assert decision.ml is not None and decision.ml.error is None
    reasons = {r.value for r in MlReason}
    for row in decision.candidates:
        if row.estimate is None:
            continue
        block = row.estimate.compute.detail["ml"]
        assert block["model_version"] == signal.predictor_version
        assert block["used"] or block["reason"] in reasons
        assert row.estimate.env.detail["ml"]["reason"] in {
            "no_model",
            "exact_by_construction",
        }
