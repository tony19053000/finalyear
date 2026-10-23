"""V0.15 G3 + G4 merge: the registry's model set reaches the planner.

A real controller (PostgreSQL, its own CAS):

* with a trained **active** set, ``server.ml_source`` (the one ML attribute)
  answers an :class:`MlInput` named by that set version, and it is what the
  dispatcher reads per tick; a dispatch planned with it stores
  ``ml_model_version`` = the set version on every candidate row and
  ``replay.ml.version``;
* with **no** set (and with ``ml = "off"``), ``ml_source.current()`` is None
  and a dispatch stores no ML key at all (V0.14 rows: calibration / raw).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
from mf_controller_harness import make_settings
from mf_jobs_harness import running_controller

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows
from sqlalchemy import text

from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.ml.registry import ModelRegistry
from meshforge.controller.ml.source import RegistryMlInputSource
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fit_main import set_document
from meshforge.controller.ml.train.gate import Reference, run_gate
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]

FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
INPUTS = {"watermark": {"x": 1}, "train": {}}
MANIFEST = {"name": "merge", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "ef" * 32


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


async def _clear(runtime: ControllerRuntime) -> None:
    assert runtime._engine is not None
    async with runtime._engine.begin() as conn:
        await conn.execute(text("DELETE FROM ml_model_set"))


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


async def _dispatch(runtime: ControllerRuntime, source: RegistryMlInputSource) -> Any:
    """One dispatch planned the way a dispatcher tick does (``planner.ml =
    await ml_source.current()``); the stored ``plan_decision`` row."""
    assert runtime._engine is not None
    store = ExecutionStore(runtime._engine)
    planner = DispatchPlanner(
        manager=_Manager(await _nodes(runtime, 2)),  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=60.0, default_env_s=20.0),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )
    planner.ml = await source.current()
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="merge",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="m" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(fingerprint="9" * 64, input_bytes=0),
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
                    "SELECT candidates, replay FROM plan_decision WHERE attempt_id = :a"
                ),
                {"a": result.attempt.id},
            )
        ).one()


async def test_an_active_set_is_the_planners_model(
    pg_settings: DatabaseSettings,
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        await _clear(runtime)
        source = runtime.ml_source
        assert isinstance(source, RegistryMlInputSource)
        assert runtime.dispatcher is not None
        assert runtime.dispatcher._ml is source  # the one attribute, per tick
        assert source.max_interval_log == PlannerSettings().ml_max_interval_log
        assert source.budget_ms == PlannerSettings().ml_predict_budget_ms
        # no set: no model, a V0.14 decision
        assert await source.current() is None
        bare = await _dispatch(runtime, source)
        assert all("ml_model_version" not in c for c in bare.candidates)
        assert "ml" not in bare.replay
        # a trained active set: the planner's model, named in the decision
        assert runtime.blobs is not None and runtime._engine is not None
        registry = ModelRegistry(runtime._engine, runtime.blobs.cas)
        doc, arts = _trained(601)
        stored = await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
        assert stored.row is not None and stored.row.status == "active"
        ml = await source.current()
        assert ml is not None and ml.version == doc["set_version"]
        assert ml.predictor.has_model("compute")
        assert not ml.predictor.has_model("env")  # a `none` member
        row = await _dispatch(runtime, source)
        assert {c["ml_model_version"] for c in row.candidates} == {doc["set_version"]}
        assert row.replay["ml"]["version"] == doc["set_version"]
        assert "fallback" not in row.replay["ml"]  # the model answered
        for cand in row.candidates:
            terms = (cand.get("estimate") or {}).get("terms") or {}
            if "compute" in terms:
                block = terms["compute"]["detail"]["ml"]
                assert block["model_version"].startswith("sha256:")
            if "env" in terms:
                env = terms["env"]["detail"]["ml"]
                assert env["used"] is False
                assert env["reason"] in {"no_model", "exact_by_construction"}


async def test_ml_off_never_gives_the_planner_a_model(
    pg_settings: DatabaseSettings,
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        await _clear(runtime)
        assert runtime.blobs is not None and runtime._engine is not None
        registry = ModelRegistry(runtime._engine, runtime.blobs.cas)
        doc, arts = _trained(602)
        await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
    async with running_controller(
        make_settings(pg_settings, planner={"ml": "off"})
    ) as runtime:
        assert runtime.ml_source is not None
        assert await runtime.ml_source.current() is None
        row = await _dispatch(runtime, runtime.ml_source)
        assert all("ml_model_version" not in c for c in row.candidates)
        assert "ml" not in row.replay
