"""V0.15 G3 tester round 3 - fix r2 (N1-N3, GC touch, child output) and the
merged registry -> SetPredictor -> planner path, on real PostgreSQL + CAS.

Derived from brief 5c/5d: a 0-byte artefact is rejected (never active, never
retryable network_error); DB refusals are non-retryable invalid_input;
gate.history is bounded; surrogates never reach the database; the active
set switching, a pin, a superseded set, a stale feature_version, a failing
warm-up, a per-query ArtefactError and an infinite-q member each leave the
planner with a valid, replayable decision (D363: calibration, never a failed
plan).
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

import math

from alembic import command
from mf_controller_harness import make_settings
from mf_jobs_harness import running_controller
from mf_ml_synthetic import synthetic_rows
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from meshforge.common.errors import ConfigError, FailureType
from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.controller.blobs import BlobStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.ml import export
from meshforge.controller.ml.artefact import ArtefactError, digest
from meshforge.controller.ml.dataset import Dataset, parquet_bytes, sort_key
from meshforge.controller.ml.predictor import ml_input
from meshforge.controller.ml.registry import MlRegistryError, ModelRegistry, PinError
from meshforge.controller.ml.replay import ML_MODEL_UNAVAILABLE, replay_decision
from meshforge.controller.ml.schema import FEATURE_VERSION
from meshforge.controller.ml.serve import LoadedModel
from meshforge.controller.ml.source import (
    STALE_FEATURES,
    ModelSource,
    RegistryMlInputSource,
    SetPredictor,
)
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fit_main import set_document
from meshforge.controller.ml.train.gate import Reference, run_gate
from meshforge.controller.ml_retrain_task import MlRetrainer
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(900)]

FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
INPUTS = {"watermark": {"x": 1}, "train": {}}
MANIFEST = {"name": "r3", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "ab" * 32


def _trained(seed: int) -> tuple[dict[str, Any], dict[str, bytes]]:
    rows = synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=10, seed=seed
    )
    table = table_from_rows(rows)
    result = train_set(table, FAST)
    gate = run_gate(table, FAST, result, Reference())
    return set_document(result, gate, FAST), {
        m.version: m.artefact for m in result.members if m.artefact
    }


def _rejected(doc: dict[str, Any], tag: str = "t") -> dict[str, Any]:
    out = copy.deepcopy(doc)
    out["accepted"] = False
    out["reason"] = "not_better: test"
    out["gate"] = {**(out.get("gate") or {}), "accepted": False, "tag": tag}
    return out


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=6)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DELETE FROM ml_model_set"))
        yield eng
    finally:
        await eng.dispose()


def _registry(engine: Any, tmp_path: Path) -> ModelRegistry:
    blobs = BlobStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    blobs.cas.prepare()
    return ModelRegistry(engine, blobs.cas)


# ----------------------------------------------------------------- N1


def _zero_byte(doc: dict[str, Any]) -> tuple[dict[str, Any], dict[str, bytes]]:
    out = copy.deepcopy(doc)
    empty = digest(b"")
    for member in out["members"]:
        if member["algorithm"] != "none":
            member["version"] = str(empty)
    return out, {str(empty): b""}


async def test_zero_byte_artefact_is_rejected_unloadable_never_active(
    engine: Any, tmp_path: Path
) -> None:
    registry = _registry(engine, tmp_path)
    doc, _ = _trained(801)
    assert doc["accepted"] is True  # the gate itself would activate it
    bad, arts = _zero_byte(doc)
    res = await registry.store(bad, arts, reference_version=None, inputs=INPUTS)
    assert res.row is not None
    assert res.row.status == "rejected"
    assert (res.row.reason or "").startswith("unloadable_artefact: compute")
    assert res.row.gate["rule"] == "unloadable_artefact"
    learned = [m for m in res.row.members if m.learned]
    assert learned and all(m.artefact_bytes == 0 for m in learned)
    assert await registry.serving_version() == (None, None)
    with pytest.raises(PinError):  # a rejected set is never pinnable
        await registry.pin(res.row.set_version)
    # and it did not poison the registry: a good set still activates
    good, garts = _trained(801)
    ok = await registry.store(good, garts, reference_version=None, inputs=INPUTS)
    assert ok.row is not None and ok.row.status == "active"


async def test_migration_0034_allows_zero_but_not_negative_bytes(
    engine: Any, tmp_path: Path
) -> None:
    registry = _registry(engine, tmp_path)
    doc, _ = _trained(802)
    bad, arts = _zero_byte(doc)
    await registry.store(bad, arts, reference_version=None, inputs=INPUTS)
    async with engine.begin() as conn:
        n = await conn.execute(
            text("SELECT count(*) FROM ml_model WHERE artefact_bytes = 0")
        )
        assert n.scalar_one() >= 1
    with pytest.raises(DBAPIError):
        async with engine.begin() as conn:
            await conn.execute(text("UPDATE ml_model SET artefact_bytes = -1"))


async def test_zero_byte_artefact_then_restart_serves_nothing(
    engine: Any, tmp_path: Path
) -> None:
    registry = _registry(engine, tmp_path)
    doc, _ = _trained(803)
    bad, arts = _zero_byte(doc)
    await registry.store(bad, arts, reference_version=None, inputs=INPUTS)
    source = ModelSource(registry, "auto")
    assert await source.refresh() is None
    assert source.unavailable is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.__setitem__("feature_version", 2**70),
        lambda d: d.__setitem__("set_version", "x" * 500),
        lambda d: d.__setitem__("dataset_digest", None),
        lambda d: d.__setitem__("set_version", "sha256:" + "z" * 64 + "\x00"),
    ],
    ids=["int_overflow", "huge_set_version", "null_digest", "nul_in_version"],
)
async def test_database_refusals_are_non_retryable_invalid_input(
    engine: Any, tmp_path: Path, mutate: Callable[[dict[str, Any]], None]
) -> None:
    registry = _registry(engine, tmp_path)
    doc, arts = _trained(804)
    bad = _rejected(doc)
    mutate(bad)
    with pytest.raises(MlRegistryError) as caught:
        await registry.store(bad, arts, reference_version=None, inputs=INPUTS)
    assert caught.value.failure_type is FailureType.INVALID_INPUT, caught.value
    assert caught.value.retryable is False
    # the registry is still usable afterwards (no poisoned transaction/lock)
    good, garts = _trained(804)
    ok = await registry.store(good, garts, reference_version=None, inputs=INPUTS)
    assert ok.row is not None and ok.row.status == "active"


# ----------------------------------------------------------------- N2 / N3


async def test_gate_history_is_bounded_to_twenty_newest(
    engine: Any, tmp_path: Path
) -> None:
    registry = _registry(engine, tmp_path)
    doc, arts = _trained(811)
    first = await registry.store(
        _rejected(doc, "g0"), arts, reference_version=None, inputs=INPUTS
    )
    assert first.row is not None and "history" not in first.row.gate
    last = first
    for i in range(1, 46):
        last = await registry.store(
            _rejected(doc, f"g{i}"), arts, reference_version=None, inputs=INPUTS
        )
        assert last.rejudged
        assert last.row is not None
        assert len(last.row.gate.get("history", [])) <= 20
    assert last.row is not None
    history = last.row.gate["history"]
    assert len(history) == 20
    assert [h["gate"]["tag"] for h in history] == [f"g{i}" for i in range(25, 45)]
    assert last.row.gate["tag"] == "g45"
    # history entries do not nest history (bounded size)
    assert all("history" not in h["gate"] for h in history)
    # identical gate again: same_set, nothing written
    again = await registry.store(
        _rejected(doc, "g45"), arts, reference_version=None, inputs=INPUTS
    )
    assert again.skipped == "same_set"


async def test_surrogates_and_controls_never_reach_the_database(
    engine: Any, tmp_path: Path
) -> None:
    registry = _registry(engine, tmp_path)
    doc, arts = _trained(821)
    bad = _rejected(doc)
    bad["reason"] = "lone \ud800 and \udfff and nul \x00 and del \x7f end"
    bad["gate"]["note"] = {"k\ud800": ["v\udc00", "oké中\U0001f600"]}
    for member in bad["members"]:
        member["reason"] = "m\ud83d"
        member["params"] = {"p\udbff": "x\x01"}
        member["metrics"] = {"m": "\x00"}
    res = await registry.store(bad, arts, reference_version=None, inputs=INPUTS)
    assert res.row is not None and res.row.status == "rejected"
    blob = json.dumps(
        [res.row.reason, res.row.gate, [m.reason for m in res.row.members]]
    )
    for ch in ("\x00", "\x7f"):
        assert ch not in blob
    assert "\\ud800" not in blob.lower() and "\\udfff" not in blob.lower()
    assert "é中\U0001f600" in res.row.gate["note"]["k�"][1]
    # round-trips through get()
    got = await registry.get(res.row.set_version)
    assert got is not None and got.reason == res.row.reason


# ------------------------------------------------------- child output (reviewer 4)


def _built() -> export.BuiltDataset:
    import hashlib

    rows = sorted(
        synthetic_rows(
            "signal", families=6, groups_per_family=5, runs_per_group=10, seed=831
        ),
        key=sort_key,
    )
    data = parquet_bytes(rows)
    dataset = Dataset(
        rows=tuple(rows),
        counts={"rows": len(rows)},
        parquet=data,
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
    )
    return export.BuiltDataset(dataset=dataset, max_runs=1000, truncated=False)


def _w(text_: str) -> Callable[[Path], None]:
    def write(out: Path) -> None:
        (out / "set.json").write_text(text_, "utf-8")

    return write


def _members_not_list(out: Path) -> None:
    (out / "set.json").write_text(
        json.dumps({"set_version": "sha256:" + "0" * 64, "members": 5}), "utf-8"
    )


def _artefact_is_directory(out: Path) -> None:
    doc, _arts = _trained(832)
    for member in doc["members"]:
        if member["algorithm"] != "none":
            member["artefact"] = "adir"
    (out / "adir").mkdir()
    (out / "set.json").write_text(json.dumps(doc), "utf-8")


def _no_set_json(out: Path) -> None:
    return None


@pytest.mark.parametrize(
    "write",
    [
        _w("[]"),
        _w("null"),
        _w(""),
        _w('{"members": null}'),
        _w('"just a string"'),
        _members_not_list,
        _artefact_is_directory,
        _no_set_json,
    ],
    ids=[
        "array",
        "null",
        "empty",
        "members_null",
        "string",
        "members_int",
        "artefact_dir",
        "no_set_json",
    ],
)
async def test_odd_child_output_is_child_output_invalid_never_crashed(
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: Callable[[Path], None],
) -> None:
    built = _built()

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    registry = _registry(engine, tmp_path)

    async def child(argv: Any, timeout_s: float) -> tuple[int, str]:
        out = Path(argv[list(argv).index("--out") + 1])
        await asyncio.to_thread(out.mkdir)
        await asyncio.to_thread(write, out)
        return 0, ""

    task = MlRetrainer(
        engine,
        registry,
        ModelSource(registry, "auto"),
        PlannerSettings(ml_min_rows=100),
        child=child,
    )
    result = await task.retrain_now("manual")
    assert result.error == "child_output_invalid", result
    assert result.row is None
    async with engine.connect() as conn:
        n = (await conn.execute(text("SELECT count(*) FROM ml_model_set"))).scalar_one()
    assert n == 0


# ======================================================== merged planner path


@dataclass
class _Live:
    node_id: str
    session_id: str
    state: NodeState = NodeState.ONLINE
    rtt_ewma_ms: float | None = 1.0
    policy: Any = None
    policy_slots: Any = None
    clock: Any = field(default_factory=lambda: SimpleNamespace(latest=None))


class _Manager:
    def __init__(self, nodes: list[str]) -> None:
        self.lives = {n: _Live(n, str(new_session_id())) for n in nodes}

    def get(self, node_id: str) -> _Live | None:
        return self.lives.get(node_id)

    def get_lingering(self, node_id: str) -> None:
        return None


async def _nodes(runtime: ControllerRuntime, n: int) -> list[str]:
    assert runtime._engine is not None
    out = []
    for _ in range(n):
        node = str(new_node_id())
        await Registry(runtime._engine).upsert_node_on_hello(
            node_id=node,
            hostname="h",
            os="linux",
            platform="x86_64",
            hardware={"capabilities": ["exec", "exec-subprocess"]},
            session_epoch=1,
            session_id=str(new_session_id()),
            state=NodeState.ONLINE,
            hardware_scores=None,
            cpu_slots=8,
            gpu_slots=0,
        )
        out.append(node)
    return sorted(out)


class _Dispatcher:
    """Plans dispatches like a dispatcher tick: ``planner.ml = await
    source.current()`` once per tick."""

    def __init__(self, runtime: ControllerRuntime, nodes: list[str]) -> None:
        self.runtime = runtime
        self.nodes = nodes
        self.n = 0
        self.planner: DispatchPlanner | None = None

    async def dispatch(self, source: Any) -> Any:
        runtime = self.runtime
        assert runtime._engine is not None
        store = ExecutionStore(runtime._engine)
        planner = DispatchPlanner(
            manager=_Manager(self.nodes),  # type: ignore[arg-type]
            settings=JobApiSettings(max_attempts_per_node=64),
            planner=PlannerSettings(default_compute_s=60.0, default_env_s=20.0),
            clock=lambda: 1_000_000.0,
            wall_clock=lambda: dt.datetime.now(dt.UTC),
            started_at=0.0,
            cache_live=lambda _n, _s: True,
        )
        self.planner = planner
        planner.ml = await source.current()
        self.n += 1
        job, _ = await store.create_queued_job(
            job_id=str(new_job_id()),
            name="r3",
            manifest=MANIFEST,
            bundle_digest=DIGEST,
            placement={"mode": "auto", "node_id": None},
            request_hash=f"{self.n:064x}",
            idempotency_key=None,
            scheduling=JobScheduling(fingerprint=f"{self.n:064x}", input_bytes=0),
        )
        results = await store.dispatch_planned(
            planner.decide,
            limit=10,
            max_per_node=64,
            now=dt.datetime.now(dt.UTC),
            aging_s=600.0,
        )
        (result,) = [r for r in results if r.job.id == job.id]
        assert result.attempt is not None
        async with runtime._engine.connect() as conn:
            return (
                await conn.execute(
                    text(
                        "SELECT policy, chosen_node_id, snapshot_digest, reason_key, "
                        "candidates, replay FROM plan_decision WHERE attempt_id = :a"
                    ),
                    {"a": result.attempt.id},
                )
            ).one()


async def _setup(runtime: ControllerRuntime) -> tuple[ModelRegistry, _Dispatcher]:
    assert runtime._engine is not None and runtime.blobs is not None
    async with runtime._engine.begin() as conn:
        await conn.execute(text("DELETE FROM ml_model_set"))
    registry = ModelRegistry(runtime._engine, runtime.blobs.cas)
    return registry, _Dispatcher(runtime, await _nodes(runtime, 2))


def _versions(row: Any) -> set[str | None]:
    return {c.get("ml_model_version") for c in row.candidates}


def _loader(registry: ModelRegistry) -> Callable[[str], Any]:
    async def build(version: str) -> Any:
        got = await registry.get(version)
        if got is None:
            return None
        return ml_input(SetPredictor(await registry.load(got)), budget_ms=None)

    def load(version: str) -> Any:
        return asyncio.run_coroutine_threadsafe(build(version), LOOP[0]).result()

    return load


LOOP: list[asyncio.AbstractEventLoop] = []


async def _replays(registry: ModelRegistry, row: Any) -> Any:
    """replay_decision with registry-backed load, run in a thread (the
    loader is synchronous, as the contract says)."""
    LOOP[:] = [asyncio.get_running_loop()]
    return await asyncio.to_thread(
        replay_decision, row.replay, row.policy, load=_loader(registry)
    )


async def test_active_set_switch_mid_run_and_replay_of_old_decision(
    pg_settings: DatabaseSettings,
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        registry, disp = await _setup(runtime)
        source = runtime.ml_source
        assert isinstance(source, RegistryMlInputSource)
        doc_a, arts_a = _trained(901)
        a = await registry.store(doc_a, arts_a, reference_version=None, inputs=INPUTS)
        assert a.row is not None and a.row.status == "active"
        row_a = await disp.dispatch(source)
        assert _versions(row_a) == {doc_a["set_version"]}
        doc_b, arts_b = _trained(902)
        assert doc_b["set_version"] != doc_a["set_version"]
        b = await registry.store(
            doc_b, arts_b, reference_version=doc_a["set_version"], inputs=INPUTS
        )
        assert b.row is not None and b.row.status == "active"
        assert b.superseded == doc_a["set_version"]
        row_b = await disp.dispatch(source)  # no restart, same source object
        assert _versions(row_b) == {doc_b["set_version"]}
        assert row_b.replay["ml"]["version"] == doc_b["set_version"]
        # the OLD decision still replays with its OWN (now superseded) set
        again = await _replays(registry, row_a)
        assert again.status == "ok" and again.decision is not None
        assert candidates_json(again.decision) == row_a.candidates
        new = await _replays(registry, row_b)
        assert new.decision is not None
        assert candidates_json(new.decision) == row_b.candidates
        # a loader that hands back the WRONG version is not a replay
        wrong = replay_decision(
            row_a.replay,
            row_a.policy,
            load=lambda _v: source._input,  # set B's input
        )
        assert wrong.status == ML_MODEL_UNAVAILABLE and wrong.decision is None


async def test_pin_a_superseded_set_then_unpin(pg_settings: DatabaseSettings) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        registry, disp = await _setup(runtime)
        source = runtime.ml_source
        assert isinstance(source, RegistryMlInputSource)
        doc_a, arts_a = _trained(911)
        doc_b, arts_b = _trained(912)
        await registry.store(doc_a, arts_a, reference_version=None, inputs=INPUTS)
        await registry.store(
            doc_b, arts_b, reference_version=doc_a["set_version"], inputs=INPUTS
        )
        assert _versions(await disp.dispatch(source)) == {doc_b["set_version"]}
        pinned = await registry.pin(doc_a["set_version"])
        assert pinned.status == "pinned"
        assert _versions(await disp.dispatch(source)) == {doc_a["set_version"]}
        await registry.unpin(doc_a["set_version"])
        assert _versions(await disp.dispatch(source)) == {doc_b["set_version"]}


async def test_stale_feature_version_set_is_skipped_decision_is_calibration(
    pg_settings: DatabaseSettings,
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        registry, disp = await _setup(runtime)
        source = runtime.ml_source
        assert isinstance(source, RegistryMlInputSource)
        doc, arts = _trained(921)
        doc["feature_version"] = FEATURE_VERSION - 1
        res = await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
        assert res.row is not None and res.row.status == "active"
        assert await source.current() is None
        assert source.models.unavailable == STALE_FEATURES
        for _ in range(3):  # idempotent, no crash, no retry storm
            row = await disp.dispatch(source)
            assert row.chosen_node_id is not None
            assert "ml" not in row.replay and _versions(row) == {None}
        # a fresh set of the current feature version takes over
        good, garts = _trained(922)
        await registry.store(
            good, garts, reference_version=doc["set_version"], inputs=INPUTS
        )
        assert _versions(await disp.dispatch(source)) == {good["set_version"]}
        assert source.models.unavailable is None


async def test_warm_up_failure_plans_without_ml_and_recovers(
    pg_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        registry, disp = await _setup(runtime)
        source = runtime.ml_source
        assert isinstance(source, RegistryMlInputSource)
        doc, arts = _trained(931)
        await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
        real = LoadedModel.predict

        def broken(_self: Any, _x: Any) -> Any:
            raise ArtefactError("native", details={"reason": "bad_model"})

        monkeypatch.setattr(LoadedModel, "predict", broken)
        row = await disp.dispatch(source)
        assert row.chosen_node_id is not None  # still a valid decision
        assert "ml" not in row.replay and _versions(row) == {None}
        # the same (cached) failed set is not re-warmed every tick
        row2 = await disp.dispatch(source)
        assert row2.chosen_node_id is not None and "ml" not in row2.replay
        monkeypatch.setattr(LoadedModel, "predict", real)
        # the warm-up failure is cached per loaded-set object (documented): a
        # new set switches it back on
        doc2, arts2 = _trained(932)
        await registry.store(
            doc2, arts2, reference_version=doc["set_version"], inputs=INPUTS
        )
        assert _versions(await disp.dispatch(source)) == {doc2["set_version"]}


async def test_per_query_artefact_error_falls_back_to_calibration_and_replays(
    pg_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        registry, disp = await _setup(runtime)
        source = runtime.ml_source
        assert isinstance(source, RegistryMlInputSource)
        doc, arts = _trained(941)
        await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
        healthy = await disp.dispatch(source)
        assert "fallback" not in healthy.replay["ml"]  # warmed up and answering
        real = LoadedModel.predict

        def broken(_self: Any, _x: Any) -> Any:
            raise ArtefactError("native", details={"reason": "bad_model"})

        monkeypatch.setattr(LoadedModel, "predict", broken)
        row = await disp.dispatch(source)  # the set is cached: warm-up skipped
        assert row.chosen_node_id is not None
        assert disp.planner is not None and disp.planner.ml_predictor_errors == 1
        assert row.replay["ml"]["version"] == doc["set_version"]
        assert row.replay["ml"]["fallback"]
        assert all(c.get("ml_fallback") == "predictor_error" for c in row.candidates)
        for cand in row.candidates:
            terms = (cand.get("estimate") or {}).get("terms") or {}
            for term in terms.values():
                block = (term.get("detail") or {}).get("ml")
                if block is not None:
                    assert block["used"] is False
                    assert block["reason"] in {
                        "predictor_error",
                        "exact_by_construction",
                        "no_model",
                    }
        # replay of the fallback decision reproduces it with and without the model
        monkeypatch.setattr(LoadedModel, "predict", real)
        r1 = await _replays(registry, row)
        assert r1.decision is not None
        assert candidates_json(r1.decision) == row.candidates
        r2 = replay_decision(row.replay, row.policy, load=lambda _v: None)
        assert r2.decision is not None
        assert candidates_json(r2.decision) == row.candidates
        # transient: once the model answers again the next decision uses it
        after = await disp.dispatch(source)
        assert "fallback" not in after.replay["ml"]
        assert _versions(after) == {doc["set_version"]}
        # the fallback decision's numbers equal a decision with NO ml at all
        bare = await disp.dispatch(_NoModel())
        by_node = {
            c["node_id"]: (c["estimate"] or {}).get("total_s") for c in bare.candidates
        }
        for cand in row.candidates:
            got = (cand["estimate"] or {}).get("total_s")
            assert got == by_node[cand["node_id"]]


class _NoModel:
    async def current(self) -> None:
        return None


class _Stub:
    def __init__(self, loaded: Any) -> None:
        self.loaded = loaded

    async def refresh(self) -> Any:
        return self.loaded


async def test_infinite_q_member_is_no_model_not_a_whole_decision_fallback(
    pg_settings: DatabaseSettings,
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        registry, disp = await _setup(runtime)
        doc, arts = _trained(951)
        res = await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
        assert res.row is not None
        loaded = await registry.load(res.row)
        model = loaded.models["compute"]
        endless = LoadedModel(replace(model.artefact, q=math.inf))
        source = RegistryMlInputSource(
            _Stub(replace(loaded, models={"compute": endless}))  # type: ignore[arg-type]
        )
        row = await disp.dispatch(source)
        assert row.chosen_node_id is not None
        assert "fallback" not in row.replay.get("ml", {})
        assert disp.planner is not None and disp.planner.ml_predictor_errors == 0
        assert all("ml_fallback" not in c for c in row.candidates)
        saw = False
        for cand in row.candidates:
            terms = (cand.get("estimate") or {}).get("terms") or {}
            if "compute" in terms:
                block = terms["compute"]["detail"]["ml"]
                assert block["used"] is False and block["reason"] == "no_model"
                saw = True
        assert saw


async def test_config_pin_and_rejected_pin_refused_at_startup(
    pg_settings: DatabaseSettings,
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        registry, _ = await _setup(runtime)
        doc_a, arts_a = _trained(961)
        doc_b, arts_b = _trained(962)
        await registry.store(doc_a, arts_a, reference_version=None, inputs=INPUTS)
        await registry.store(
            doc_b, arts_b, reference_version=doc_a["set_version"], inputs=INPUTS
        )
        doc_r, arts_r = _trained(963)
        rej = await registry.store(
            _rejected(doc_r),
            arts_r,
            reference_version=doc_b["set_version"],
            inputs=INPUTS,
        )
        assert rej.row is not None and rej.row.status == "rejected"
    # config-pinned to the superseded A while B is active: A serves
    async with running_controller(
        make_settings(pg_settings, planner={"ml": doc_a["set_version"]})
    ) as runtime:
        assert runtime.ml_source is not None
        disp = _Dispatcher(runtime, await _nodes(runtime, 2))
        ml = await runtime.ml_source.current()
        assert ml is not None and ml.version == doc_a["set_version"]
        row = await disp.dispatch(runtime.ml_source)
        assert _versions(row) == {doc_a["set_version"]}
    # pinned to a rejected / unknown set: refused, never a silent fallback
    for bad in (doc_r["set_version"], "sha256:" + "9" * 64):
        with pytest.raises(ConfigError):
            async with running_controller(
                make_settings(pg_settings, planner={"ml": bad})
            ):
                pass
    # restart keeps the active set
    async with running_controller(make_settings(pg_settings)) as runtime:
        assert runtime.ml_source is not None
        ml = await runtime.ml_source.current()
        assert ml is not None and ml.version == doc_b["set_version"]
