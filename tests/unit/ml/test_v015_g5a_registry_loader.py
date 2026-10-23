"""V0.15 G5a (D364, D368, D371): the registry loader - a stored set by
version, through the serving path.

* any status loads (a rejected / superseded set too); the answer is **the
  asked version** - mutation guard: a loader that returns the active set for
  any version fails here;
* unknown version, another feature version, an artefact that cannot be
  loaded -> None; a database error raises;
* LRU of 4 sets; a miss is never cached;
* :meth:`RegistryLoader.blocking` works from a worker thread, refuses the
  loop's own thread;
* a stored ML decision replayed with the registry loader reproduces the
  decision field for field.
"""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
from mf_eval_world import NOW, fabric
from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.registry import MlRegistryError
from meshforge.controller.ml.registry_loader import CACHE_SETS, RegistryLoader
from meshforge.controller.ml.replay import ML_MODEL_UNAVAILABLE, replay_decision
from meshforge.controller.ml.schema import FEATURE_VERSION
from meshforge.controller.ml.serve import LoadedSet, load_model
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import TrainResult, train_set
from meshforge.controller.planner.plan import plan
from meshforge.controller.planner.replay import to_replay

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Row:
    set_version: str
    status: str
    feature_version: int
    result: TrainResult


@dataclass
class FakeRegistry:
    rows: dict[str, _Row] = field(default_factory=dict)
    broken: set[str] = field(default_factory=set)
    fail: bool = False
    reads: int = 0

    async def get(self, version: str) -> _Row | None:
        if self.fail:
            raise MlRegistryError("down", details={"cause": "OSError"})
        return self.rows.get(version)

    async def load(self, row: _Row) -> LoadedSet:
        self.reads += 1
        if row.set_version in self.broken:
            raise ArtefactError("gone", details={"reason": "artefact_missing"})
        models = {
            m.term: load_model(m.artefact, expected_version=m.version)
            for m in row.result.learned
            if m.artefact is not None
        }
        return LoadedSet(row.set_version, row.feature_version, models)


def _train(algorithm: str, seed: int = 1) -> TrainResult:
    rows = synthetic_rows(
        "signal", families=4, groups_per_family=3, runs_per_group=6, seed=seed
    )
    return train_set(
        table_from_rows(rows),
        CompareSettings(terms=("compute",), algorithms=(algorithm,), min_rows=50),
    )


@pytest.fixture(scope="module")
def sets() -> dict[str, TrainResult]:
    out = {}
    for i, algorithm in enumerate(("ridge",) * 5):
        result = _train(algorithm, seed=i + 1)
        assert result.learned, algorithm
        out[result.set_version] = result
    assert len(out) == 5
    return out


def _registry(sets: Mapping[str, TrainResult], **status: str) -> FakeRegistry:
    statuses = ["active", "rejected", "superseded", "superseded", "superseded"]
    return FakeRegistry(
        {
            v: _Row(v, statuses[i], FEATURE_VERSION, r)
            for i, (v, r) in enumerate(sets.items())
        }
    )


def test_any_status_loads_and_the_answer_is_the_asked_version(
    sets: dict[str, TrainResult],
) -> None:
    registry = _registry(sets)
    loader = RegistryLoader(registry)  # type: ignore[arg-type]

    async def run() -> None:
        for version, row in registry.rows.items():
            got = await loader.load(version)
            assert got is not None, row.status
            assert got.version == version  # never the active set for another
            assert type(got.predictor).__name__ == "SetPredictor"  # no budget

    asyncio.run(run())


def test_unknown_stale_and_broken_sets_are_none_and_db_errors_raise(
    sets: dict[str, TrainResult],
) -> None:
    registry = _registry(sets)
    versions = list(registry.rows)
    stale = registry.rows[versions[1]]
    registry.rows[versions[1]] = dataclasses.replace(stale, feature_version=1)
    registry.broken.add(versions[2])
    loader = RegistryLoader(registry)  # type: ignore[arg-type]

    async def run() -> None:
        assert await loader.load("sha256:" + "00" * 32) is None
        assert await loader.load(versions[1]) is None
        assert await loader.load(versions[2]) is None
        assert loader.cached == ()  # misses are never cached
        registry.broken.clear()
        assert await loader.load(versions[2]) is not None  # restored: loads
        registry.fail = True
        with pytest.raises(MlRegistryError):
            await loader.load(versions[3])

    asyncio.run(run())


def test_the_last_four_sets_are_kept(sets: dict[str, TrainResult]) -> None:
    registry = _registry(sets)
    loader = RegistryLoader(registry)  # type: ignore[arg-type]
    versions = list(registry.rows)

    async def run() -> None:
        for version in versions[:CACHE_SETS]:
            await loader.load(version)
        assert registry.reads == CACHE_SETS
        await loader.load(versions[0])  # a hit, now the most recent
        assert registry.reads == CACHE_SETS
        await loader.load(versions[4])  # evicts versions[1] (least recent)
        assert loader.cached == (versions[2], versions[3], versions[0], versions[4])
        await loader.load(versions[1])
        assert registry.reads == CACHE_SETS + 2

    asyncio.run(run())
    with pytest.raises(ValueError, match="at least one"):
        RegistryLoader(registry, size=0)  # type: ignore[arg-type]


def test_blocking_from_a_worker_thread_and_never_on_the_loop(
    sets: dict[str, TrainResult],
) -> None:
    registry = _registry(sets)
    loader = RegistryLoader(registry)  # type: ignore[arg-type]
    version = next(iter(registry.rows))

    async def run() -> None:
        load = loader.blocking(asyncio.get_running_loop())
        got = await asyncio.to_thread(load, version)
        assert got is not None and got.version == version
        with pytest.raises(RuntimeError, match="worker thread"):
            load(version)

    asyncio.run(run())


def test_a_stored_ml_decision_replays_field_for_field(
    sets: dict[str, TrainResult],
) -> None:
    registry = _registry(sets)
    loader = RegistryLoader(registry)  # type: ignore[arg-type]
    version = next(iter(registry.rows))
    request, snapshot = fabric(
        {"node_a": 100.0, "node_b": 10.0, "node_c": 50.0, "node_d": 80.0}
    )

    async def run() -> tuple[Any, Any, Any]:
        ml = await loader.load(version)
        assert ml is not None
        original = plan(request, snapshot, "cost_model", NOW, ml=ml)
        assert original.ml is not None and original.ml.version == version
        replay = to_replay(request, snapshot, NOW, projected=False, ml=original.ml)
        fresh = RegistryLoader(registry)  # type: ignore[arg-type]
        load = fresh.blocking(asyncio.get_running_loop())
        again = await asyncio.to_thread(
            replay_decision, replay, "cost_model", load=load
        )
        gone = await asyncio.to_thread(
            replay_decision,
            {**replay, "ml": {**replay["ml"], "version": "sha256:" + "00" * 32}},
            "cost_model",
            load=load,
        )
        return original, again, gone

    original, again, gone = asyncio.run(run())
    assert again.status == "ok" and again.ml_model_version == version
    assert again.decision == original  # field for field
    assert gone.status == ML_MODEL_UNAVAILABLE and gone.decision is None
