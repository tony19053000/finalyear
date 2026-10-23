"""V0.15 G3 tester round 3 - SetPredictor adversarial cases the merge author
did not cover: interleaved terms keep the caller's order, NaN/inf rows,
a huge batch, concurrent use from threads, and a swapped set object is
rebuilt (never a stale predictor)."""

from __future__ import annotations

import asyncio
import math
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

import numpy as np
from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.schema import FEATURE_VERSION, NUMERIC_FEATURES
from meshforge.controller.ml.serve import LoadedSet, load_model
from meshforge.controller.ml.source import RegistryMlInputSource, SetPredictor
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.planner.predictor import PredictorFailed, TermQuery

pytestmark = pytest.mark.unit

FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
W = len(NUMERIC_FEATURES)


def _model(seed: int):  # type: ignore[no-untyped-def]
    rows = synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=10, seed=seed
    )
    result = train_set(table_from_rows(rows), FAST)
    (member,) = [m for m in result.members if m.artefact]
    return load_model(member.artefact, expected_version=member.version)


@pytest.fixture(scope="module")
def two() -> LoadedSet:
    return LoadedSet(
        set_version="sha256:" + "c" * 64,
        feature_version=FEATURE_VERSION,
        models={"compute": _model(1001), "env": _model(1002)},
        without_model={},
    )


def _row(seed: int) -> tuple[float, ...]:
    rng = np.random.default_rng(seed)
    return tuple(float(v) for v in rng.uniform(0, 30, W))


def test_interleaved_terms_keep_caller_order(two: LoadedSet) -> None:
    p = SetPredictor(two)
    terms = ["compute", "env", "env", "compute", "compute", "env"]
    queries = [TermQuery(t, f"n{i}", _row(i)) for i, t in enumerate(terms)]
    out = p.predict(queries)
    assert len(out) == len(queries)
    for q, a in zip(queries, out, strict=True):
        direct = two.models[q.term].predict(np.asarray([q.features]))
        assert a.y_hat == pytest.approx(float(direct.y_hat[0]), abs=1e-12)
        assert a.model_version == two.models[q.term].version
    assert two.models["compute"].version != two.models["env"].version


def test_a_term_without_a_member_in_the_batch_fails_the_whole_batch(
    two: LoadedSet,
) -> None:
    with pytest.raises(PredictorFailed):
        SetPredictor(two).predict(
            [
                TermQuery("compute", "n", _row(1)),
                TermQuery("lane_compute", "n", _row(2)),
            ]
        )


@pytest.mark.filterwarnings("ignore::RuntimeWarning")
@pytest.mark.parametrize("bad", [math.inf, -math.inf, 1e300, -1e300])
def test_extreme_values_never_crash_the_predictor(two: LoadedSet, bad: float) -> None:
    row = list(_row(3))
    row[0] = bad
    try:
        (a,) = SetPredictor(two).predict([TermQuery("compute", "n", tuple(row))])
    except PredictorFailed:
        return  # typed failure is acceptable (D363)
    assert isinstance(a.ood, bool)  # the planner decides on ood/finite-ness


def test_large_batch_and_thread_safety(two: LoadedSet) -> None:
    p = SetPredictor(two)
    queries = [TermQuery("compute", f"n{i}", _row(i)) for i in range(5000)]
    serial = [a.y_hat for a in p.predict(queries)]

    def run(_: int) -> list[float]:
        return [a.y_hat for a in p.predict(queries[:500])]

    with ThreadPoolExecutor(8) as pool:
        for got in pool.map(run, range(16)):
            assert got == serial[:500]


class _Models:
    def __init__(self, loaded: LoadedSet) -> None:
        self.loaded = loaded

    async def refresh(self) -> LoadedSet:
        return self.loaded


def test_a_swapped_set_object_is_rebuilt_not_reused(two: LoadedSet) -> None:
    other = LoadedSet(
        set_version="sha256:" + "d" * 64,
        feature_version=FEATURE_VERSION,
        models={"compute": two.models["env"]},
        without_model={},
    )
    models = _Models(two)
    source = RegistryMlInputSource(models)  # type: ignore[arg-type]

    async def run() -> None:
        first = await source.current()
        assert first is not None and first.version == two.set_version
        models.loaded = other
        second = await source.current()
        assert second is not None and second.version == other.set_version
        assert second is not first
        models.loaded = two
        third = await source.current()
        assert third is not None and third.version == two.set_version

    asyncio.run(run())
