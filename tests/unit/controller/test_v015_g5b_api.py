"""V0.15 G5b (D373, D374): the typed ML fields of the decisions answer, the
predictions route's auth / bounds / filters, and the live ML numbers on
``GET /v1/planner/ml/models``.

* **Mapping table** - a V0.14 row (no ``detail.ml``) -> ``ml`` null; ML used
  -> ``corrected_s`` = the term's seconds; not used, per reason; the
  whole-decision fallback (``ml_fallback``); the Mode 3 lane rate nested as
  ``ml.rate`` (its ``corrected_s`` = ``raw_s x e^y_hat``); malformed stored
  blocks never raise.
* **Predictions route** - the REAL ``require_admin`` (only ``authenticate``
  is stubbed): 401 / 403 / 200; ``limit`` 1..2000 (default 500) reaches the
  read; ``term`` / ``set`` are validated and passed through; a database
  failure is 503 with ``Retry-After``.
"""

from __future__ import annotations

import datetime as dt
import math
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI

from meshforge.common.ml_explain import decision_ml, term_ml
from meshforge.controller.api import auth, ml_model_routes, ml_prediction_routes
from meshforge.controller.api.decision_routes import DecisionOut, TermOut, _out
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.jobs.decision_store import DecisionRecordOut
from meshforge.controller.jobs.planning import MlServingStats
from meshforge.controller.ml import predictions
from meshforge.controller.ml.predictions import PredictionPoint, PredictionsRead

pytestmark = [pytest.mark.unit]

SET = "sha256:" + "ab" * 32
T0 = dt.datetime(2026, 10, 23, 9, 0, tzinfo=dt.UTC)


def _term(seconds: float, ml: Any = None, **detail: Any) -> dict[str, Any]:
    if ml is not None:
        detail["ml"] = ml
    return {"seconds": seconds, "source": "history", "detail": detail}


def _record(terms: dict[str, Any], **row: Any) -> DecisionRecordOut:
    candidate = {
        "node_id": "node_a",
        "mode": "offload",
        "feasible": True,
        "slot": "cpu",
        "reject_code": None,
        "reject_text": None,
        "estimate": {"total_s": 1.0, "terms": terms},
        "score": 1.0,
        "rank": 1,
        **row,
    }
    return DecisionRecordOut(
        id=1,
        attempt_no=1,
        attempt_id=None,
        outcome="dispatched",
        policy="cost_model",
        chosen_node_id="node_a",
        created_at=T0,
        snapshot_digest="00" * 32,
        reason_key="00" * 32,
        candidates=[candidate],
    )


def _ml_of(decision: DecisionOut, name: str) -> Any:
    estimate = decision.candidates[0].estimate
    assert estimate is not None
    return estimate.terms[name].ml


# ------------------------------------------------------------ mapping table


def test_a_v014_row_maps_to_nulls() -> None:
    out = _out(_record({"compute": _term(10.0)}))
    assert _ml_of(out, "compute") is None
    assert out.ml_model_version is None and out.ml_fallback is None


def test_ml_used_carries_raw_and_corrected_seconds() -> None:
    block = {
        "model_version": "m1",
        "used": True,
        "y_hat": -0.2,
        "lo": -0.3,
        "hi": -0.1,
        "raw_s": 12.0,
    }
    out = _out(_record({"compute": _term(9.8, block)}, ml_model_version=SET))
    ml = _ml_of(out, "compute")
    assert ml.used is True and ml.reason is None
    assert (ml.y_hat, ml.lo, ml.hi, ml.raw_s) == (-0.2, -0.3, -0.1, 12.0)
    assert ml.corrected_s == 9.8  # the seconds the planner used
    assert ml.rate is None
    assert out.ml_model_version == SET and out.ml_fallback is None


@pytest.mark.parametrize(
    "reason",
    [
        "no_model",
        "ood",
        "interval_too_wide",
        "exact_by_construction",
        "below_min_predicted",
        "predictor_error",
        "combine_node_unknown",
    ],
)
def test_ml_not_used_per_reason(reason: str) -> None:
    block: dict[str, Any] = {"model_version": SET, "used": False, "reason": reason}
    if reason in ("ood", "interval_too_wide"):
        block |= {"y_hat": 0.1, "lo": -1.0, "hi": 1.0}
    if reason == "ood":
        block["ood_reason"] = "range:hw_cpu_gflops"
    out = _out(_record({"env": _term(3.0, block)}, ml_model_version=SET))
    ml = _ml_of(out, "env")
    assert ml.used is False and ml.reason == reason
    assert ml.corrected_s is None and ml.raw_s is None
    assert ml.ood_reason == ("range:hw_cpu_gflops" if reason == "ood" else None)
    asked = reason in ("ood", "interval_too_wide")
    assert (ml.y_hat is not None) is asked


def test_whole_decision_fallback() -> None:
    block = {"model_version": SET, "used": False, "reason": "predictor_error"}
    record = _record(
        {"compute": _term(9.0, block)},
        ml_model_version=SET,
        ml_fallback="predictor_error",
    )
    out = _out(record)
    assert out.ml_model_version == SET
    assert out.ml_fallback == "predictor_error"


def test_mode3_lane_rate_is_nested_and_never_dropped() -> None:
    rate = {
        "model_version": "m1",
        "used": True,
        "y_hat": -0.1,
        "lo": -0.2,
        "hi": 0.0,
        "raw_s": 0.2,
    }
    block = {
        "model_version": "m1",
        "used": True,
        "y_hat": -0.1,
        "lo": -0.2,
        "hi": 0.0,
        "raw_s": 40.0,
        "rate": rate,
    }
    out = _out(_record({"compute": _term(36.19, block)}))
    ml = _ml_of(out, "compute")
    assert ml.rate is not None
    assert ml.rate.used is True and ml.rate.raw_s == 0.2
    assert ml.rate.corrected_s == pytest.approx(0.2 * math.exp(-0.1))
    assert not hasattr(ml.rate, "rate")  # one level only


@pytest.mark.parametrize(
    "block",
    [
        "x",
        5,
        [],
        {},
        {"used": "yes"},
        {"used": True, "raw_s": -1, "y_hat": math.inf, "lo": "a", "hi": None},
        {"used": False, "reason": 7, "model_version": ["v"]},
        {"used": True, "y_hat": 1e308, "raw_s": 1.0, "rate": {"used": True}},
        {"used": True, "rate": "nope"},
    ],
)
def test_malformed_stored_blocks_never_raise(block: Any) -> None:
    out = _out(_record({"compute": _term(1.0, block)}))
    ml = _ml_of(out, "compute")
    if not isinstance(block, dict):
        assert ml is None
    else:
        assert ml is not None
        assert ml.used is (block.get("used") is True)


def test_decision_ml_reads_the_first_row_that_names_it() -> None:
    assert decision_ml([{"x": 1}, {"ml_model_version": SET}]) == (SET, None)
    assert decision_ml("nope") == (None, None)
    assert decision_ml([{"ml_model_version": 5, "ml_fallback": ""}]) == (None, None)


def test_term_ml_tolerates_non_terms() -> None:
    assert term_ml(None) is None
    assert term_ml({"detail": "x"}) is None
    assert TermOut.model_validate({"seconds": 1, "source": "history"}).ml is None


# ------------------------------------------------------------ predictions route


class _P:
    def __init__(self, admin: bool) -> None:
        self.user_id = "usr_t"
        self.name = "t"
        self.is_admin = admin


_PRINCIPAL: list[Any] = [None]
_CALLS: list[dict[str, Any]] = []


@pytest.fixture(autouse=True)
def _stubs(monkeypatch: pytest.MonkeyPatch) -> None:
    async def authenticate(_request: Any) -> Any:
        return _PRINCIPAL[0]

    monkeypatch.setattr(auth, "authenticate", authenticate)
    _CALLS.clear()

    async def read(engine: Any, calibration: Any, **kwargs: Any) -> PredictionsRead:
        _CALLS.append(kwargs)
        if engine == "down":
            raise predictions.PredictionsError("db down")
        point = PredictionPoint(
            decision_id=4,
            term="compute",
            node_id="node_a",
            family="cpu.primes",
            analytical_s=10.0,
            calibrated_s=12.0,
            ml_s=11.0,
            used=True,
            reason=None,
            actual_s=11.5,
            ml_model_version=SET,
        )
        return PredictionsRead(points=[point], decisions_read=1)

    monkeypatch.setattr(ml_prediction_routes, "read_predictions", read)


def _app(principal: _P | None, engine: Any = "ok") -> FastAPI:
    _PRINCIPAL[0] = principal
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ml_prediction_routes.ml_predictions_router)
    setattr(
        app.state,
        ml_prediction_routes.ML_PREDICTIONS_STATE_KEY,
        ml_prediction_routes.MlPredictionsApi(engine, object()),  # type: ignore[arg-type]
    )
    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.get(path)


PATH = "/v1/planner/ml/predictions"


@pytest.mark.asyncio
async def test_no_user_401_non_admin_403_and_nothing_read() -> None:
    assert (await _get(_app(None), PATH)).status_code == 401
    refused = await _get(_app(_P(False)), PATH)
    assert refused.status_code == 403
    assert b"node_a" not in refused.content
    assert _CALLS == []


@pytest.mark.asyncio
async def test_admin_gets_points_with_the_default_bound() -> None:
    r = await _get(_app(_P(True)), PATH)
    assert r.status_code == 200
    body = r.json()
    assert body["limit"] == 500 and body["decisions_read"] == 1
    assert body["points"][0] == {
        "decision_id": 4,
        "term": "compute",
        "node_id": "node_a",
        "family": "cpu.primes",
        "analytical_s": 10.0,
        "calibrated_s": 12.0,
        "ml_s": 11.0,
        "used": True,
        "reason": None,
        "actual_s": 11.5,
        "ml_model_version": SET,
    }
    assert _CALLS == [{"limit": 500, "term": None, "set_version": None}]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["0", "2001", "-1", "abc", "1e9"])
async def test_limit_is_bounded(limit: str) -> None:
    r = await _get(_app(_P(True)), f"{PATH}?limit={limit}")
    assert r.status_code == 400
    assert _CALLS == []


@pytest.mark.asyncio
async def test_limit_max_and_filters_reach_the_read() -> None:
    r = await _get(_app(_P(True)), f"{PATH}?limit=2000&term=chunk_rate&set={SET}")
    assert r.status_code == 200
    assert r.json()["term"] == "chunk_rate" and r.json()["set"] == SET
    assert _CALLS == [{"limit": 2000, "term": "chunk_rate", "set_version": SET}]


@pytest.mark.asyncio
@pytest.mark.parametrize("q", ["term=queue", "term=", "set=sha256:xyz", "set=abc"])
async def test_bad_filters_are_client_errors(q: str) -> None:
    r = await _get(_app(_P(True)), f"{PATH}?{q}")
    assert r.status_code == 400
    assert _CALLS == []


@pytest.mark.asyncio
async def test_database_down_is_503_retryable() -> None:
    r = await _get(_app(_P(True), engine="down"), PATH)
    assert r.status_code == 503
    assert r.headers["retry-after"] == "2"


# ------------------------------------------------------------ live ML numbers


def _source() -> Any:
    return SimpleNamespace(
        current=None, mode="auto", pinned=None, kind=None, unavailable=None
    )


def test_in_use_carries_the_dispatchers_live_numbers() -> None:
    api = ml_model_routes.MlModelsApi(
        registry=object(),  # type: ignore[arg-type]
        source=_source(),
        retrainer=object(),  # type: ignore[arg-type]
        serving_stats=lambda: MlServingStats(
            predict_last_ms=3.25, predictor_errors_total=7
        ),
    )
    out = ml_model_routes._in_use(api.source, api)
    assert out.ml_predict_last_ms == 3.25
    assert out.ml_predictor_errors_total == 7


def test_without_a_dispatcher_the_numbers_are_null() -> None:
    api = ml_model_routes.MlModelsApi(
        registry=object(),  # type: ignore[arg-type]
        source=_source(),
        retrainer=object(),  # type: ignore[arg-type]
    )
    out = ml_model_routes._in_use(api.source, api)
    assert out.ml_predict_last_ms is None and out.ml_predictor_errors_total is None


def test_planner_property_reads_the_budgeted_predictors_last_ms() -> None:
    from meshforge.controller.jobs.planning import DispatchPlanner

    prop = DispatchPlanner.ml_predict_last_ms.fget
    assert prop is not None
    assert prop(SimpleNamespace(ml=None)) is None
    with_ms = SimpleNamespace(
        ml=SimpleNamespace(predictor=SimpleNamespace(last_ms=4.5))
    )
    assert prop(with_ms) == 4.5
    for odd in (None, True, math.nan, -1.0, "3"):
        planner = SimpleNamespace(
            ml=SimpleNamespace(predictor=SimpleNamespace(last_ms=odd))
        )
        assert prop(planner) is None
    no_attr = SimpleNamespace(ml=SimpleNamespace(predictor=object()))
    assert prop(no_attr) is None
