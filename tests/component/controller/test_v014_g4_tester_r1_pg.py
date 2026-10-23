"""V0.14 G4 tester round 1 - eval-reports API on a real PostgreSQL, with the
**real** ``guard_requests`` / ``current_user`` / ``require_admin`` (no
dependency overrides).

* auth matrix: no / bad token -> 401 on all three routes; a user may GET (200)
  but POST -> 403 and nothing is evaluated or stored; admin POST -> 201;
* request validation: 0 / 21 campaigns, empty / 65-char ids, empty / 65-char
  holdout, non-JSON, unknown fields' effect, ``limit`` and ``format`` bounds,
  report id 0 / 2**63 -> 400; unknown -> 404; nothing stored on any refusal;
* idempotency / concurrency: several simultaneous POSTs for the same campaign
  all succeed (serialised by the service lock), each stores its own row and the
  reports are identical except for the id/created_at;
* the oracle is evaluated (reference row) but a stored report's policies list
  holds nothing a job could be placed with except real policies.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from alembic import command
from fastapi import Depends, FastAPI
from mf_eval_poll import bg, wait_for_report
from mf_eval_pg import (
    dispatch_runs,
    finish_runs,
    make_campaign,
    planner,
    register_nodes,
)
from sqlalchemy import text

from meshforge.common.api_token import API_TOKEN_PREFIX
from meshforge.controller.api import planner_eval_routes as routes
from meshforge.controller.api.auth import AUTH_STATE_KEY, ApiAuth, guard_requests
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.auth.principal import Principal
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.auth import UserRole
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]

ADMIN = API_TOKEN_PREFIX + "A" * 43
USER = API_TOKEN_PREFIX + "B" * 43
BAD = API_TOKEN_PREFIX + "C" * 43


class _Users:
    def __init__(self) -> None:
        self.table = {
            ADMIN: Principal("usr_admin", "root", UserRole.ADMIN, "tok_a"),
            USER: Principal("usr_user", "bob", UserRole.USER, "tok_b"),
        }

    async def authenticate(self, token: str, *, kind: Any = None) -> Principal | None:
        return self.table.get(token)

    async def note_legacy_use(self, source: str | None) -> None:
        return None

    async def owns_output(self, user_id: str, digest_hex: str) -> bool:
        return False


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=8)
    try:
        yield eng
    finally:
        await eng.dispose()


def _app(engine: Any) -> FastAPI:
    app = FastAPI(dependencies=[Depends(guard_requests)])
    install_error_handlers(app)
    app.include_router(routes.planner_eval_router)
    setattr(app.state, AUTH_STATE_KEY, ApiAuth(verifier=None, users=_Users()))  # type: ignore[arg-type]
    service = EvalService(
        engine,
        CalibrationStore(engine),
        EvalReportStore(engine),
        CalibrationConfig(),
        max_rows=10_000,
    )
    setattr(
        app.state, routes.PLANNER_EVAL_STATE_KEY, routes.PlannerEvalApi(service=service)
    )
    return app


async def _call(
    app: FastAPI, method: str, path: str, token: str | None, **kw: Any
) -> httpx.Response:
    headers = dict(kw.pop("headers", {}))
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        return await c.request(method, path, headers=headers, **kw)


async def _count(engine: Any) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(text("SELECT count(*) FROM planner_eval_report"))
            ).scalar_one()
        )


async def _campaign(engine: Any) -> str:
    nodes = await register_nodes(engine, 3)
    campaign_id, runs = await make_campaign(engine, nodes, [20, 22])
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
    groups = sorted({r.group_id for r in runs})
    best = {groups[0]: nodes[2], groups[1]: nodes[0]}

    def total(run: Any) -> int:
        fast = best[run.group_id] == run.node_id
        return (
            1_000 if fast else 3_000 + 500 * nodes.index(run.node_id)
        ) + run.repetition

    await finish_runs(engine, dispatched, total)
    return campaign_id


async def test_auth_matrix_with_the_real_guards(engine: Any) -> None:
    campaign_id = await _campaign(engine)
    app = _app(engine)
    body = {"campaign_ids": [campaign_id]}
    for token in (None, BAD, "not-a-token"):
        for method, path, kw in (
            ("POST", "/v1/planner/eval-reports", {"json": body}),
            ("GET", "/v1/planner/eval-reports", {}),
            ("GET", "/v1/planner/eval-reports/1", {}),
        ):
            r = await _call(app, method, path, token, **kw)
            assert r.status_code == 401, (method, path, token, r.text)
    assert await _count(engine) == 0
    forbidden = await _call(app, "POST", "/v1/planner/eval-reports", USER, json=body)
    assert forbidden.status_code == 403
    assert await _count(engine) == 0  # a non-admin evaluates and stores nothing
    ok = await _call(app, "POST", "/v1/planner/eval-reports", ADMIN, json=bg(body))
    assert ok.status_code == 202, ok.text  # V0.15 G6b (D378)
    rid = ok.json()["report_id"]
    done = await wait_for_report(lambda p: _call(app, "GET", p, ADMIN), rid)
    for token in (USER, ADMIN):
        assert (
            await _call(app, "GET", "/v1/planner/eval-reports", token)
        ).status_code == 200
        got = await _call(app, "GET", f"/v1/planner/eval-reports/{rid}", token)
        assert got.status_code == 200 and got.json() == done.json()
    # wrong methods never reach the evaluator
    for method in ("PUT", "DELETE", "PATCH"):
        r = await _call(app, method, f"/v1/planner/eval-reports/{rid}", ADMIN)
        assert r.status_code in (404, 405)
    assert await _count(engine) == 1


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"campaign_ids": []},
        {"campaign_ids": ["c"] * 21},
        {"campaign_ids": [""]},
        {"campaign_ids": ["x" * 65]},
        {"campaign_ids": "camp_1"},
        {"campaign_ids": [1, 2]},
        {"campaign_ids": ["camp_nope"], "holdout": ""},
        {"campaign_ids": ["camp_nope"], "holdout": "f" * 65},
        {"campaign_ids": ["camp_nope"], "allow_leak": "perhaps"},
        {"campaign_ids": [None]},
    ],
)
async def test_malformed_requests_are_400_and_store_nothing(
    engine: Any, body: Any
) -> None:
    app = _app(engine)
    r = await _call(app, "POST", "/v1/planner/eval-reports", ADMIN, json=body)
    assert r.status_code == 400, (body, r.status_code, r.text)
    assert r.json()["error"]["code"] == "invalid_input"
    assert await _count(engine) == 0


async def test_non_json_and_unicode_bodies(engine: Any) -> None:
    app = _app(engine)
    r = await _call(
        app,
        "POST",
        "/v1/planner/eval-reports",
        ADMIN,
        content=b"{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code in (400, 422)
    r = await _call(
        app,
        "POST",
        "/v1/planner/eval-reports",
        ADMIN,
        json=bg({"campaign_ids": ["camp_é中文\U0001f600"]}),
    )
    assert r.status_code == 404  # unknown campaign, not a 500
    r = await _call(
        app,
        "POST",
        "/v1/planner/eval-reports",
        ADMIN,
        json=bg({"campaign_ids": ["a'; DROP TABLE planner_eval_report;--"]}),
    )
    assert r.status_code == 404
    assert await _count(engine) == 0


async def test_query_and_path_bounds(engine: Any) -> None:
    campaign_id = await _campaign(engine)
    app = _app(engine)
    created = await _call(
        app,
        "POST",
        "/v1/planner/eval-reports",
        ADMIN,
        json=bg({"campaign_ids": [campaign_id]}),
    )
    rid = created.json()["report_id"]
    await wait_for_report(lambda p: _call(app, "GET", p, ADMIN), rid)
    for q, code in (
        ("0", 400),
        ("-1", 400),
        ("abc", 400),
        ("1000000", 400),
        ("1", 200),
    ):
        r = await _call(app, "GET", f"/v1/planner/eval-reports?limit={q}", USER)
        assert r.status_code == code, (q, r.status_code, r.text)
    for rid_bad, code in (
        ("0", 400),
        ("-3", 400),
        ("abc", 400),
        (str(2**63), 400),
        ("99999999", 404),
    ):
        r = await _call(app, "GET", f"/v1/planner/eval-reports/{rid_bad}", USER)
        assert r.status_code == code, (rid_bad, r.status_code)
    for fmt, code in (("markdown", 200), ("json", 200), ("html", 400), ("", 400)):
        r = await _call(
            app, "GET", f"/v1/planner/eval-reports/{rid}?format={fmt}", USER
        )
        assert r.status_code == code, (fmt, r.status_code)


async def test_repeated_and_concurrent_posts_are_independent_and_identical(
    engine: Any,
) -> None:
    campaign_id = await _campaign(engine)
    app = _app(engine)
    body = bg({"campaign_ids": [campaign_id, campaign_id]})  # duplicate tolerated
    # V0.15 G6b (D378): one evaluation at a time - concurrent starts: one 202,
    # the others 409 eval_already_running naming the running report
    results = await asyncio.gather(
        *[
            _call(app, "POST", "/v1/planner/eval-reports", ADMIN, json=body)
            for _ in range(4)
        ]
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [202, 409, 409, 409], [r.text for r in results]
    for r in results:
        if r.status_code == 409:
            assert r.json()["error"]["reason"] == "eval_already_running"
    ids = []
    for _ in range(4):
        if ids:
            started = await _call(
                app, "POST", "/v1/planner/eval-reports", ADMIN, json=body
            )
            assert started.status_code == 202, started.text
            rid = started.json()["report_id"]
        else:
            rid = next(r for r in results if r.status_code == 202).json()["report_id"]
        await wait_for_report(lambda p: _call(app, "GET", p, ADMIN), rid)
        ids.append(rid)
    assert len(set(ids)) == 4 and await _count(engine) == 4
    reports = [
        (await _call(app, "GET", f"/v1/planner/eval-reports/{i}", ADMIN)).json()[
            "report"
        ]
        for i in ids
    ]
    assert all(rep == reports[0] for rep in reports)  # deterministic
    assert reports[0]["campaign_ids"] == [campaign_id]
    listed = (await _call(app, "GET", "/v1/planner/eval-reports", USER)).json()[
        "reports"
    ]
    assert [r["id"] for r in listed] == sorted(ids, reverse=True)  # newest first
    # a summary carries verdicts but no per-group rows
    assert "groups" not in listed[0] and listed[0]["verdicts"]
    # the oracle is in the stored report as the reference row only
    keys = [p["key"] for p in reports[0]["policies"]]
    assert keys.count("oracle") == 1
