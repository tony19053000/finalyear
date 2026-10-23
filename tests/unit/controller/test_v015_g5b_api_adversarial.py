"""V0.15 G5b tester round 1 (D374): the predictions route under hostile
query strings and a hostile read. Real ``require_admin``; ``authenticate`` and
the read are stubbed (as in ``test_v015_g5b_api``)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI

from meshforge.controller.api import auth, ml_prediction_routes
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.ml.predictions import PredictionsRead

pytestmark = [pytest.mark.unit]

PATH = "/v1/planner/ml/predictions"
SET = "sha256:" + "ab" * 32
CALLS: list[dict[str, Any]] = []


class _P:
    def __init__(self, admin: bool) -> None:
        self.user_id = "usr_t"
        self.name = "t"
        self.is_admin = admin


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def authenticate(_request: Any) -> Any:
        return _P(True)

    monkeypatch.setattr(auth, "authenticate", authenticate)
    CALLS.clear()

    async def read(_engine: Any, _cal: Any, **kwargs: Any) -> PredictionsRead:
        CALLS.append(kwargs)
        return PredictionsRead(points=[], decisions_read=0)

    monkeypatch.setattr(ml_prediction_routes, "read_predictions", read)


async def _get(path: str, *, configured: bool = True) -> httpx.Response:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ml_prediction_routes.ml_predictions_router)
    if configured:
        setattr(
            app.state,
            ml_prediction_routes.ML_PREDICTIONS_STATE_KEY,
            ml_prediction_routes.MlPredictionsApi(object(), object()),  # type: ignore[arg-type]
        )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.get(path)


@pytest.mark.asyncio
async def test_empty_registry_answers_an_empty_list_not_an_error() -> None:
    r = await _get(PATH)
    assert r.status_code == 200
    assert r.json() == {
        "limit": 500,
        "decisions_read": 0,
        "term": None,
        "set": None,
        "points": [],
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["1", "499", "500", "501", "1999", "2000"])
async def test_every_in_range_limit_reaches_the_read_unchanged(limit: str) -> None:
    r = await _get(f"{PATH}?limit={limit}")
    assert r.status_code == 200
    assert CALLS[-1]["limit"] == int(limit)
    assert r.json()["limit"] == int(limit)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "limit",
    [
        "2001",
        "99999999999999999999",
        "0",
        "-5",
        "1.5",
        "",
        " ",
        "%20",
        "NaN",
        "inf",
        "0x10",
        "٣",
    ],
)
async def test_out_of_range_or_non_integer_limits_never_reach_the_read(
    limit: str,
) -> None:
    r = await _get(f"{PATH}?limit={limit}")
    assert r.status_code == 400, limit
    assert CALLS == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "q",
    [
        "term=COMPUTE",
        "term=compute%20",
        "term=compute&term=queue",
        "set=sha256:" + "AB" * 32,
        "set=sha256:" + "ab" * 31,
        "set=sha256:" + "ab" * 33,
        "set=sha256:" + "ab" * 32 + "%0a",
        "set=%27%3B+DROP+TABLE+plan_decisions%3B--",
        "set=../../etc/passwd",
    ],
)
async def test_hostile_filters_are_refused_before_the_read(q: str) -> None:
    r = await _get(f"{PATH}?{q}")
    assert r.status_code == 400, q
    assert CALLS == []


@pytest.mark.asyncio
async def test_unknown_query_parameters_are_ignored_not_forwarded() -> None:
    r = await _get(f"{PATH}?evil=1&limit=3")
    assert r.status_code == 200
    assert CALLS == [{"limit": 3, "term": None, "set_version": None}]


@pytest.mark.asyncio
async def test_an_unconfigured_controller_is_a_clean_error_not_a_500_traceback() -> (
    None
):
    r = await _get(PATH, configured=False)
    assert r.status_code in (400, 404, 500, 501, 503)
    assert "Traceback" not in r.text
    assert CALLS == []


@pytest.mark.asyncio
async def test_a_known_set_with_no_decisions_is_an_empty_answer() -> None:
    r = await _get(f"{PATH}?set={SET}")
    assert r.status_code == 200 and r.json()["points"] == []
