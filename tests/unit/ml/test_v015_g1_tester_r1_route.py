"""V0.15 G1 tester round 1: ``GET /v1/planner/ml/dataset`` auth behaviour.

The REAL ``require_admin`` runs (only ``authenticate`` is stubbed through the
``current_user`` dependency): no user -> 401, a non-admin -> 403, an admin ->
200 with the digest header equal to the summary digest and the body's SHA-256.
The database read is replaced by an in-memory build (the PG path is covered by
the component tests).
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx
import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("fastapi")

from fastapi import FastAPI
from mf_ml_fixtures import stored, whole_run

from meshforge.controller.api import auth, ml_routes
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.ml import dataset as ds
from meshforge.controller.ml import export

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

PHASES = {"stage": 12_000, "env": 25_000, "run": 400_000, "collect": 300}


_PRINCIPAL: list[Any] = [None]


class _P:
    def __init__(self, admin: bool) -> None:
        self.user_id = "usr_t"
        self.name = "t"
        self.is_admin = admin


def _app(principal: _P | None) -> FastAPI:
    _PRINCIPAL[0] = principal
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ml_routes.ml_router)

    setattr(app.state, ml_routes.ML_STATE_KEY, ml_routes.MlDatasetApi(object(), 5))  # type: ignore[arg-type]
    return app


@pytest.fixture(autouse=True)
def _fake_build(monkeypatch: pytest.MonkeyPatch) -> None:
    async def authenticate(request: Any) -> Any:
        return _PRINCIPAL[0]  # require_admin / current_user are the real ones

    monkeypatch.setattr(auth, "authenticate", authenticate)
    data = ds.build([whole_run(stored(pinned="node_001"), phases=PHASES)])

    async def fake(engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return export.BuiltDataset(dataset=data, max_runs=max_runs, truncated=False)

    monkeypatch.setattr(export, "build_dataset", fake)


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        return await c.get(path)


@pytest.mark.parametrize("q", ["", "?format=summary", "?format=parquet"])
async def test_no_user_is_401_and_leaks_nothing(q: str) -> None:
    r = await _get(_app(None), "/v1/planner/ml/dataset" + q)
    assert r.status_code == 401
    assert "x-meshforge-dataset-digest" not in r.headers
    assert b"PAR1" not in r.content and b"digest" not in r.content


@pytest.mark.parametrize("q", ["", "?format=summary"])
async def test_non_admin_is_403_and_leaks_nothing(q: str) -> None:
    r = await _get(_app(_P(False)), "/v1/planner/ml/dataset" + q)
    assert r.status_code == 403
    assert "x-meshforge-dataset-digest" not in r.headers
    assert b"PAR1" not in r.content and b"sha256" not in r.content


async def test_admin_gets_parquet_whose_hash_matches_header_and_summary() -> None:
    app = _app(_P(True))
    body = await _get(app, "/v1/planner/ml/dataset")
    summ = await _get(app, "/v1/planner/ml/dataset?format=summary")
    assert body.status_code == summ.status_code == 200
    digest = "sha256:" + hashlib.sha256(body.content).hexdigest()
    assert body.headers["x-meshforge-dataset-digest"] == digest == summ.json()["digest"]
    assert body.content[:4] == b"PAR1" and body.content[-4:] == b"PAR1"
    assert summ.json()["bytes"] == len(body.content)


@pytest.mark.parametrize("q", ["?format=", "?format=CSV", "?format=parquet&format=x"])
async def test_bad_format_is_a_client_error_not_500(q: str) -> None:
    r = await _get(_app(_P(True)), "/v1/planner/ml/dataset" + q)
    assert 400 <= r.status_code < 500


async def test_unconfigured_route_is_an_error_envelope_not_a_crash() -> None:
    app = _app(_P(True))
    delattr(app.state, ml_routes.ML_STATE_KEY)
    r = await _get(app, "/v1/planner/ml/dataset")
    assert r.status_code in (500, 503) and "error" in r.json()
