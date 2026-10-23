"""V0.15 G6a on a real PostgreSQL: the two new admin routes on what the real
dispatcher stored.

* ``POST /v1/planner/ml/classifier-examples`` - a campaign placed by real
  dispatch ticks and finished (labels, matched runs, replays from the
  database): one example per labelled group, the label the oracle's class,
  each class's outcome the planner's best candidate of that class; the
  document validates against the API model and the ``ml`` child reads it
  (two groups -> ``too_few_groups``, no accuracy); unknown campaign -> 404;
* ``GET /v1/planner/decisions/{id}/replay`` - every dispatched decision of
  the campaign re-plans ``reproduced`` through the route (the production
  ``DecisionReplayer`` with a real ``RegistryLoader``); unknown id -> 404;
  a decision whose replay was wiped -> ``no_replay``;
* both routes are admin-only (the ``AdminUser`` dependency).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

import httpx
from alembic import command
from fastapi import FastAPI
from mf_eval_pg import (
    dispatch_runs,
    finish_runs,
    make_campaign,
    planner,
    register_nodes,
)
from sqlalchemy import text

from meshforge.common.cas import CasStore
from meshforge.controller.api import decision_replay_routes as replay_routes
from meshforge.controller.api import ml_classify_routes as classify_routes
from meshforge.controller.api.auth import current_user, require_admin
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.ml.decision_replay import DecisionReplayer
from meshforge.controller.ml.registry import ModelRegistry
from meshforge.controller.ml.registry_loader import RegistryLoader
from meshforge.controller.ml.train.classify import classify
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.classifier_service import (
    ClassifierExamplesService,
)
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.timeout(600)]


class _Principal:
    user_id = "usr_test"
    name = "t"
    is_admin = True


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=6)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _campaign(engine: Any) -> tuple[str, list[str]]:
    nodes = await register_nodes(engine, 3)
    campaign_id, runs = await make_campaign(engine, nodes, [20, 22])
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
    groups = sorted({r.group_id for r in runs})
    best = {groups[0]: nodes[2], groups[1]: nodes[0]}

    def total(run: Any) -> int:
        fast = best[run.group_id] == run.node_id
        return (1_000 if fast else 3_000) + run.repetition

    await finish_runs(engine, dispatched, total)
    return campaign_id, nodes


def _app(engine: Any, cas: CasStore) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(classify_routes.ml_classify_router)
    app.include_router(replay_routes.decision_replay_router)
    user = _Principal()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    service = ClassifierExamplesService(
        engine, CalibrationStore(engine), CalibrationConfig(), max_rows=10_000
    )
    setattr(
        app.state,
        classify_routes.ML_CLASSIFY_STATE_KEY,
        classify_routes.MlClassifyApi(service),
    )
    loader = RegistryLoader(ModelRegistry(engine, cas))
    setattr(
        app.state,
        replay_routes.DECISION_REPLAY_STATE_KEY,
        replay_routes.DecisionReplayApi(DecisionReplayer(engine, loader)),
    )
    return app


@pytest.fixture
def cas(tmp_path: Path) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    return store


@pytest.mark.asyncio
async def test_classifier_examples_from_a_dispatched_campaign(
    engine: Any, cas: CasStore
) -> None:
    campaign, _nodes = await _campaign(engine)
    transport = httpx.ASGITransport(app=_app(engine, cas))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post(
            "/v1/planner/ml/classifier-examples", json={"campaign_ids": [campaign]}
        )
        missing = await client.post(
            "/v1/planner/ml/classifier-examples",
            json={"campaign_ids": ["camp_nope"]},
        )
    assert response.status_code == 200, response.text
    assert missing.status_code == 404, missing.text
    doc = response.json()
    classify_routes.ClassifierExamplesOut.model_validate(doc)
    assert doc["campaign_ids"] == [campaign]
    assert doc["counts"]["groups"] == 2 and doc["counts"]["examples"] == 2
    labels = sorted(e["label"] for e in doc["examples"])
    # group 0's best is a mode1 node, group 1's the origin (local)
    assert labels == ["local", "mode1"]
    for example in doc["examples"]:
        assert example["records_used"] > 0
        assert len(example["features"]) == len(doc["feature_names"])
        assert {"local", "mode1"} <= set(example["classes"])
        for outcome in example["classes"].values():
            assert outcome["status"] in ("counted", "unmeasured")
        assert example["cost_model"]["status"] in ("counted", "unmeasured")
    assert set(doc["calibrations"]) == {e["family"] for e in doc["examples"]}
    section = classify(doc)
    assert section["status"] == "too_few_groups"
    assert "models" not in section


@pytest.mark.asyncio
async def test_every_dispatched_decision_replays_reproduced(
    engine: Any, cas: CasStore
) -> None:
    await _campaign(engine)
    async with engine.connect() as conn:
        ids = [
            int(r[0])
            for r in await conn.execute(
                text(
                    "SELECT id FROM plan_decision WHERE outcome = 'dispatched' "
                    "ORDER BY id"
                )
            )
        ]
    assert ids
    transport = httpx.ASGITransport(app=_app(engine, cas))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        for decision_id in ids:
            response = await client.get(f"/v1/planner/decisions/{decision_id}/replay")
            assert response.status_code == 200, response.text
            doc = response.json()
            assert doc["status"] == "reproduced", doc
            assert doc["snapshot_digest_match"] is True
            assert doc["stored_chosen_node_id"] == doc["replayed_chosen_node_id"]
            assert doc["ml_model_version"] is None
        unknown = await client.get("/v1/planner/decisions/999999999/replay")
        assert unknown.status_code == 404, unknown.text
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE plan_decision SET replay = NULL, replay_version = NULL "
                    "WHERE id = :i"
                ),
                {"i": ids[0]},
            )
        wiped = await client.get(f"/v1/planner/decisions/{ids[0]}/replay")
        assert wiped.json()["status"] == "no_replay", wiped.text


def test_both_routes_are_admin_only() -> None:
    for router, path in (
        (classify_routes.ml_classify_router, "/v1/planner/ml/classifier-examples"),
        (
            replay_routes.decision_replay_router,
            "/v1/planner/decisions/{decision_id}/replay",
        ),
    ):
        (route,) = [r for r in router.routes if getattr(r, "path", None) == path]
        calls = {d.call for d in route.dependant.dependencies}  # type: ignore[attr-defined]
        assert require_admin in calls, path
