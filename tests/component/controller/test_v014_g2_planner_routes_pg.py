"""V0.14 G2: ``/v1/planner/calibrate`` and ``/v1/planner/calibration`` over a
real PostgreSQL (the real store and refresher; only the caller is stubbed).

* ``POST /v1/planner/calibrate`` - admin only (403 for a user); with no runs
  it writes nothing (``created`` null, ``skipped`` ``too_few_runs``);
* ``GET /v1/planner/calibration`` - the active version's factors as flat rows
  with ``n`` and key level, what the planner uses now, the recent versions;
  ``?version=N`` another one; an unknown version is 404;
* the database down -> 503 ``network_error`` with ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from alembic import command
from fastapi import FastAPI
from sqlalchemy import text

from meshforge.controller.api import planner_routes as routes
from meshforge.controller.api.auth import current_user, require_admin
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.calibration_store import CalibrationSource, CalibrationStore
from meshforge.controller.planner_calibration_task import CalibrationRefresher
from meshforge.controller.settings import PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

NODE = "node_01AAAAAAAAAAAAAAAAAAAAAAAA"
FACTORS = {
    "format": 1,
    "terms": {
        "compute": {
            "global": {"factor": 1.2, "n": 30, "median": 1.3},
            "node": {NODE: {"factor": 1.5, "n": 12, "median": 1.7}},
            "node_family": {NODE: {"matmul": {"factor": 1.6, "n": 6, "median": 1.8}}},
        }
    },
    "families": {"f" * 64: "matmul"},
}


class _Principal:
    def __init__(self, *, admin: bool) -> None:
        self.user_id = "usr_test"
        self.name = "t"
        self.is_admin = admin


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=3)
    try:
        yield eng
    finally:
        await eng.dispose()


def _app(engine: Any, *, admin: bool = True, setting: str = "auto") -> FastAPI:
    store = CalibrationStore(engine)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes.planner_router)
    user = _Principal(admin=admin)
    app.dependency_overrides[current_user] = lambda: user

    def admin_only() -> Any:
        if not admin:
            from meshforge.controller.api.auth import forbidden

            raise forbidden()
        return user

    app.dependency_overrides[require_admin] = admin_only
    setattr(
        app.state,
        routes.PLANNER_STATE_KEY,
        routes.PlannerApi(
            store=store,
            source=CalibrationSource(store, setting),
            refresher=CalibrationRefresher(store, PlannerSettings()),
        ),
    )
    return app


async def _call(app: FastAPI, method: str, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path)


async def _insert(engine: Any, status: str, reason: str | None = None) -> int:
    async with engine.begin() as conn:
        row = (
            await conn.execute(
                text(
                    "INSERT INTO planner_calibration (status, factors, fit_inputs, "
                    "holdout_error_new, holdout_error_active, reason) VALUES "
                    "(:s, CAST(:f AS jsonb), CAST(:i AS jsonb), 0.1, 0.2, :r) "
                    "RETURNING version"
                ),
                {
                    "s": status,
                    "f": json.dumps(FACTORS),
                    "i": json.dumps({"pairs": 40, "held_out": 10}),
                    "r": reason,
                },
            )
        ).one()
    return int(row.version)


async def test_calibrate_is_admin_only_and_writes_nothing_without_runs(
    engine: Any,
) -> None:
    refused = await _call(_app(engine, admin=False), "POST", "/v1/planner/calibrate")
    assert refused.status_code == 403
    answer = await _call(_app(engine), "POST", "/v1/planner/calibrate")
    assert answer.status_code == 200, answer.text
    body = answer.json()
    assert body["created"] is None and body["skipped"] == "too_few_runs"
    assert body["matched_runs"] == 0 and body["superseded_version"] is None
    assert body["in_use"] == {
        "mode": "auto",
        "pinned_version": None,
        "in_use_version": None,
    }


async def test_calibration_shows_the_active_version_and_any_other(engine: Any) -> None:
    empty = await _call(_app(engine, admin=False), "GET", "/v1/planner/calibration")
    assert empty.status_code == 200
    assert (
        empty.json()["calibration"] is None and empty.json()["active_version"] is None
    )
    old = await _insert(
        engine, "rejected", reason="holdout_error 1 > 1.05 x active 0.5"
    )
    active = await _insert(engine, "active")
    app = _app(engine, admin=False)  # a signed-in user may read it
    body = (await _call(app, "GET", "/v1/planner/calibration")).json()
    assert body["active_version"] == active
    shown = body["calibration"]
    assert shown["version"] == active and shown["status"] == "active"
    assert shown["readable"] is True
    assert shown["families"] == {"f" * 64: "matmul"}
    assert shown["factors"] == [
        {
            "term": "compute",
            "key_level": "global",
            "node_id": None,
            "family": None,
            "factor": 1.2,
            "n": 30,
            "median": 1.3,
        },
        {
            "term": "compute",
            "key_level": "node",
            "node_id": NODE,
            "family": None,
            "factor": 1.5,
            "n": 12,
            "median": 1.7,
        },
        {
            "term": "compute",
            "key_level": "node_family",
            "node_id": NODE,
            "family": "matmul",
            "factor": 1.6,
            "n": 6,
            "median": 1.8,
        },
    ]
    assert body["term_phase"] == {
        "transfer": "stage",
        "env": "env",
        "compute": "run",
        "output": "collect",
    }
    assert body["not_calibrated"]["items_fetch"] == "no_phase"
    assert [r["version"] for r in body["recent"]] == [active, old]
    other = (await _call(app, "GET", f"/v1/planner/calibration?version={old}")).json()
    assert other["calibration"]["status"] == "rejected"
    assert other["calibration"]["reason"].startswith("holdout_error")
    missing = await _call(app, "GET", f"/v1/planner/calibration?version={active + 99}")
    assert missing.status_code == 404
    bad = await _call(app, "GET", "/v1/planner/calibration?version=0")
    assert bad.status_code == 400  # invalid_input


async def test_the_database_down_is_503(pg_settings: DatabaseSettings) -> None:
    broken = DatabaseSettings(
        host="127.0.0.1", port=1, name="x", user="u", connect_timeout_s=2.0
    )
    eng = create_engine(broken)
    try:
        app = _app(eng)
        for method, path in (
            ("GET", "/v1/planner/calibration"),
            ("POST", "/v1/planner/calibrate"),
        ):
            answer = await _call(app, method, path)
            assert answer.status_code == 503, answer.text
            assert answer.json()["error"]["code"] == "network_error"
            assert answer.headers.get("retry-after") == "2"
    finally:
        await eng.dispose()
