"""V0.14 G6 tester round 1 - decision latency on a real PostgreSQL, from D324
and roadmap 0.14.8 criterion 3 (not from the implementation).

* migration 0033 on a *populated* database: old decisions survive up and down
  with a NULL time, a time survives the up/up cycle only while the column
  exists, the check refuses -infinity but REAL holds NaN / +infinity (which the
  evaluator must then exclude);
* campaign filtering: the latency block of campaign A never includes campaign
  B's decisions, and A + B includes both;
* NULL / NaN / +infinity stored rows are excluded and counted, never zero;
* criterion 3 on real rows at the budget: p99 exactly 100.0 ms -> FAIL, just
  below -> PASS, fewer than 30 four-node decisions -> too_few_decisions;
* the API (real guards) serves the block; the CLI's rendering of the stored
  report carries the same numbers; an old report (no latency block, no
  criterion 3) is served as JSON, Markdown and in the list without error.
"""

from __future__ import annotations

import asyncio
import copy
import math
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from alembic import command
from fastapi import Depends, FastAPI
from mf_eval_pg import (
    dispatch_runs,
    finish_runs,
    make_campaign,
    medians,
    planner,
    register_nodes,
)
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from meshforge.cli.planner_commands import eval_lines, latency_lines
from meshforge.common.api_token import API_TOKEN_PREFIX
from meshforge.common.ids import new_job_id
from meshforge.controller.api import planner_eval_routes as routes
from meshforge.controller.api.auth import AUTH_STATE_KEY, ApiAuth, guard_requests
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.auth.principal import Principal
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.latency import LatencyConfig, nearest_rank
from meshforge.controller.planner_eval.loader import load_latency
from meshforge.controller.planner_eval.model import EvalConfig
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.auth import UserRole
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.timeout(600)]

ADMIN = API_TOKEN_PREFIX + "A" * 43
USER = API_TOKEN_PREFIX + "B" * 43


def _run[T](coro: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coro)


async def _q(settings: DatabaseSettings, sql: str, **params: Any) -> list[Any]:
    engine = create_engine(settings)
    try:
        async with engine.begin() as conn:
            res = await conn.execute(text(sql), params)
            return list(res.all()) if res.returns_rows else []
    finally:
        await engine.dispose()


def _sql(settings: DatabaseSettings, sql: str, **params: Any) -> list[Any]:
    return _run(_q(settings, sql, **params))


def _has_column(settings: DatabaseSettings) -> bool:
    return bool(
        _sql(
            settings,
            "SELECT 1 FROM information_schema.columns WHERE "
            "table_name = 'plan_decision' AND column_name = 'decision_ms'",
        )
    )


# ------------------------------------------------------------------ migration


def _decision(settings: DatabaseSettings, job: str, n: int, ms: Any = "-") -> None:
    cols = (
        "job_id, attempt_no, outcome, policy, snapshot_digest, reason_key, candidates"
    )
    vals = ":j, :n, 'waiting', 'cost_model', :d, 'r', '[{\"node_id\": \"a\"}]'::jsonb"
    if ms != "-":
        cols += ", decision_ms"
        vals += ", CAST(:ms AS REAL)"
    sql = f"INSERT INTO plan_decision ({cols}) VALUES ({vals})"  # noqa: S608
    params: dict[str, Any] = {"j": job, "n": n, "d": f"digest-{n}"}
    if ms != "-":
        params["ms"] = ms
    _sql(settings, sql, **params)


def test_0033_on_a_populated_database_keeps_every_old_decision(
    pg_settings: DatabaseSettings,
) -> None:
    cfg = alembic_config(pg_settings)
    command.upgrade(cfg, "0032")
    job = str(new_job_id())
    _sql(
        pg_settings,
        "INSERT INTO job (id, name, state, manifest, bundle_digest, placement, "
        "request_hash, attempt_epoch, created_at, updated_at) VALUES (:j, 'x', "
        "'queued', '{}'::jsonb, :d, '{}'::jsonb, :h, 0, now(), now())",
        j=job,
        d="sha256:" + "a" * 64,
        h="h" * 64,
    )
    for n in range(1, 51):  # 50 decisions written before the column existed
        _decision(pg_settings, job, n)
    command.upgrade(cfg, "0033")
    rows = _sql(
        pg_settings,
        "SELECT attempt_no, snapshot_digest, candidates, decision_ms FROM "
        "plan_decision WHERE job_id = :j ORDER BY attempt_no",
        j=job,
    )
    assert len(rows) == 50
    assert all(r[3] is None for r in rows)  # unknown, never 0
    assert rows[7][1] == "digest-8" and rows[7][2] == [{"node_id": "a"}]

    _decision(pg_settings, job, 51, ms=12.5)
    _decision(pg_settings, job, 52, ms=0)
    _decision(pg_settings, job, 53, ms=math.nan)  # PG: NaN >= 0 is true
    _decision(pg_settings, job, 54, ms=math.inf)
    with pytest.raises(DBAPIError):
        _decision(pg_settings, job, 55, ms=-math.inf)
    with pytest.raises(DBAPIError):
        _decision(pg_settings, job, 56, ms=-1e-6)
    stored = dict(
        _sql(
            pg_settings,
            "SELECT attempt_no, decision_ms FROM plan_decision WHERE attempt_no > 50 "
            "AND job_id = :j",
            j=job,
        )
    )
    assert stored[51] == 12.5 and stored[52] == 0.0
    assert math.isnan(stored[53]) and stored[54] == math.inf

    command.downgrade(cfg, "0032")  # populated: the data of other columns stays
    assert not _has_column(pg_settings)
    count = _sql(
        pg_settings, "SELECT count(*) FROM plan_decision WHERE job_id = :j", j=job
    )
    assert count[0][0] == 54
    command.upgrade(cfg, "head")  # times are gone, rows are NULL again
    assert _has_column(pg_settings)
    assert (
        _sql(
            pg_settings,
            "SELECT count(*) FROM plan_decision WHERE decision_ms IS NOT NULL",
        )[0][0]
        == 0
    )
    command.check(cfg)


# -------------------------------------------------------- API + loader + report


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


def _service(engine: Any, budget_ms: float = 100.0) -> EvalService:
    return EvalService(
        engine,
        CalibrationStore(engine),
        EvalReportStore(engine),
        CalibrationConfig(window_runs=200, shrink_k=5.0, clamp=(0.2, 5.0)),
        max_rows=10_000,
        config=EvalConfig(latency=LatencyConfig(budget_ms=budget_ms)),
    )


def _app(service: EvalService) -> FastAPI:
    app = FastAPI(dependencies=[Depends(guard_requests)])
    install_error_handlers(app)
    app.include_router(routes.planner_eval_router)
    setattr(app.state, AUTH_STATE_KEY, ApiAuth(verifier=None, users=_Users()))  # type: ignore[arg-type]
    setattr(
        app.state, routes.PLANNER_EVAL_STATE_KEY, routes.PlannerEvalApi(service=service)
    )
    return app


async def _call(
    app: FastAPI, method: str, path: str, token: str = ADMIN, **kw: Any
) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        return await c.request(
            method, path, headers={"Authorization": f"Bearer {token}"}, **kw
        )


async def _campaign(
    engine: Any, nodes: list[str], repeats: list[int]
) -> tuple[str, list[str]]:
    campaign_id, runs = await make_campaign(engine, nodes, repeats)
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
    await finish_runs(
        engine, dispatched, medians({nodes[0]: 900, nodes[1]: 600, nodes[2]: 300})
    )
    return campaign_id, [d.job_id for d in dispatched]


async def _set_ms(engine: Any, jobs: list[str], values: list[Any]) -> None:
    assert len(jobs) == len(values)
    async with engine.begin() as conn:
        for job, value in zip(jobs, values, strict=True):
            await conn.execute(
                text(
                    "UPDATE plan_decision SET decision_ms = CAST(:v AS REAL) "
                    "WHERE job_id = :j AND outcome = 'dispatched'"
                ),
                {"v": value, "j": job},
            )


async def _set_snapshot_nodes(engine: Any, jobs: list[str], count: int) -> None:
    """Make the stored replays say the snapshot held ``count`` nodes, all live
    (G6F D332: criterion 3 counts online + connected + exec rows)."""
    live = '{"state": "online", "connected": true, "capabilities": ["exec"]}'
    nodes = "[" + ",".join([live] * count) + "]"
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE plan_decision SET replay = jsonb_set(replay, "
                "'{snapshot,nodes}', CAST(:n AS jsonb)) WHERE job_id = ANY(:j) "
                "AND replay IS NOT NULL"
            ),
            {"n": nodes, "j": jobs},
        )


def _expected(values: list[float]) -> tuple[float | None, float | None, float | None]:
    ordered = sorted(values)
    return (
        nearest_rank(ordered, 50),
        nearest_rank(ordered, 99),
        ordered[-1] if ordered else None,
    )


@pytest.mark.asyncio
async def test_campaign_filter_hostile_rows_budget_boundary_api_and_cli(
    engine: Any,
) -> None:
    nodes = await register_nodes(engine, 3)
    camp_a, jobs_a = await _campaign(engine, nodes, [20, 22])
    camp_b, jobs_b = await _campaign(engine, nodes, [30, 32])
    assert len(jobs_a) == len(jobs_b) == 18 and not set(jobs_a) & set(jobs_b)

    # A: 15 real times + a NULL + a NaN + an infinity;  B: 18 x 1000 ms
    a_good = [float(i) + 0.5 for i in range(1, 16)]
    await _set_ms(engine, jobs_a, [*a_good, None, math.nan, math.inf])
    await _set_ms(engine, jobs_b, [1000.0] * 18)

    # --- the loader: campaign filtering, hostile rows excluded --------------
    only_a = await load_latency(engine, [camp_a])
    only_b = await load_latency(engine, [camp_b])
    both = await load_latency(engine, [camp_a, camp_b])
    assert (len(only_a), len(only_b), len(both)) == (18, 18, 36)
    assert await load_latency(engine, ["camp_does_not_exist"]) == []
    assert all(s.node_count == 3 for s in both)

    app = _app(_service(engine))
    posted = await _call(
        app, "POST", "/v1/planner/eval-reports", json={"campaign_ids": [camp_a]}
    )
    assert posted.status_code == 201, posted.text
    block = posted.json()["report"]["latency"]
    p50, p99, top = _expected(a_good)
    assert block["decisions"] == 15 and block["without_latency"] == 3
    assert (block["p50_ms"], block["p99_ms"], block["max_ms"]) == (p50, p99, top)
    assert block["max_ms"] == 15.5  # B's 1000 ms never leaks into A
    assert block["over_budget"] == 0
    assert block["by_node_count"]["3"]["decisions"] == 15
    assert block["judged"] == {"decisions": 0, "p99_ms": None}  # 3-node snapshots
    (c3,) = [v for v in posted.json()["report"]["verdicts"] if v["criterion"] == 3]
    assert c3["status"] == "too_few_decisions"

    # --- A + B: both campaigns count ---------------------------------------
    both_resp = await _call(
        app, "POST", "/v1/planner/eval-reports", json={"campaign_ids": [camp_b, camp_a]}
    )
    both_block = both_resp.json()["report"]["latency"]
    assert both_block["decisions"] == 33 and both_block["max_ms"] == 1000.0
    assert both_block["over_budget"] == 18

    # --- criterion 3 on 4-node snapshots, at the budget ---------------------
    await _set_snapshot_nodes(engine, [*jobs_a, *jobs_b], 4)
    judged = await _call(
        app, "POST", "/v1/planner/eval-reports", json={"campaign_ids": [camp_a, camp_b]}
    )
    report = judged.json()["report"]
    (c3,) = [v for v in report["verdicts"] if v["criterion"] == 3]
    assert report["latency"]["judged"]["decisions"] == 33
    assert c3["status"] == "FAIL" and c3["value"] == 1000.0

    only_a_4 = await _call(
        app, "POST", "/v1/planner/eval-reports", json={"campaign_ids": [camp_a]}
    )
    (c3a,) = [v for v in only_a_4.json()["report"]["verdicts"] if v["criterion"] == 3]
    assert c3a["status"] == "too_few_decisions"  # 15 four-node decisions < 30
    assert c3a["counted_groups"] == 15

    await _set_ms(engine, jobs_b, [100.0] * 18)  # p99 exactly the budget
    at = (
        await _call(
            app,
            "POST",
            "/v1/planner/eval-reports",
            json={"campaign_ids": [camp_a, camp_b]},
        )
    ).json()["report"]
    (c3_at,) = [v for v in at["verdicts"] if v["criterion"] == 3]
    assert at["latency"]["p99_ms"] == 100.0 and at["latency"]["over_budget"] == 0
    assert c3_at["status"] == "FAIL", "p99 == budget must not pass (< 100 ms)"

    await _set_ms(engine, jobs_b, [99.5] * 18)
    below = (
        await _call(
            app,
            "POST",
            "/v1/planner/eval-reports",
            json={"campaign_ids": [camp_a, camp_b]},
        )
    ).json()["report"]
    (c3_below,) = [v for v in below["verdicts"] if v["criterion"] == 3]
    assert c3_below["status"] == "PASS" and c3_below["value"] == 99.5

    # a configured budget moves the boundary
    tight = _app(_service(engine, budget_ms=50.0))
    strict = (
        await _call(
            tight,
            "POST",
            "/v1/planner/eval-reports",
            json={"campaign_ids": [camp_a, camp_b]},
        )
    ).json()["report"]
    (c3_strict,) = [v for v in strict["verdicts"] if v["criterion"] == 3]
    assert strict["latency"]["budget_ms"] == 50.0 and c3_strict["status"] == "FAIL"
    assert strict["latency"]["over_budget"] == 18

    # --- API == stored == CLI ------------------------------------------------
    rid = (
        await _call(
            app,
            "POST",
            "/v1/planner/eval-reports",
            json={"campaign_ids": [camp_a, camp_b]},
        )
    ).json()["id"]
    got = (
        await _call(app, "GET", f"/v1/planner/eval-reports/{rid}", token=USER)
    ).json()
    block = got["report"]["latency"]
    lines = latency_lines(block)
    assert f"{block['decisions']} dispatched decision(s) timed" in lines[0]
    assert f"p99 {block['p99_ms']:.2f} ms" in lines[0]
    assert f"max {block['max_ms']:.2f} ms" in lines[0]
    assert "4 nodes: 33 decision(s)" in lines[1]
    assert f"{block['judged']['decisions']} decision(s)" in lines[2]
    assert "criterion 3: PASS" in "\n".join(eval_lines(got))
    md = await _call(
        app, "GET", f"/v1/planner/eval-reports/{rid}?format=markdown", token=USER
    )
    assert md.status_code == 200 and "## Decision latency (criterion 3)" in md.text
    listed = (await _call(app, "GET", "/v1/planner/eval-reports", token=USER)).json()
    assert {v["criterion"] for r in listed["reports"] for v in r["verdicts"]} == {
        1,
        2,
        3,
    }


@pytest.mark.asyncio
async def test_a_report_stored_before_g6_is_still_served(engine: Any) -> None:
    nodes = await register_nodes(engine, 3)
    camp, _jobs = await _campaign(engine, nodes, [20, 22])
    service = _service(engine)
    fresh = await service.report([camp])
    old = copy.deepcopy(fresh)
    del old["latency"]  # what a G4/G5 controller stored
    old["verdicts"] = [v for v in old["verdicts"] if v["criterion"] != 3]
    row = await service.reports.insert(old)
    app = _app(service)

    got = await _call(app, "GET", f"/v1/planner/eval-reports/{row.id}", token=USER)
    assert got.status_code == 200, got.text
    assert got.json()["report"].get("latency") is None
    assert [v["criterion"] for v in got.json()["report"]["verdicts"]] == [1, 2]
    assert latency_lines(got.json()["report"].get("latency")) == []
    assert not any(
        line.startswith("decision latency") for line in eval_lines(got.json())
    )
    md = await _call(
        app, "GET", f"/v1/planner/eval-reports/{row.id}?format=markdown", token=USER
    )
    assert md.status_code == 200 and "Decision latency" not in md.text
    listed = await _call(app, "GET", "/v1/planner/eval-reports", token=USER)
    assert listed.status_code == 200
    assert [v["criterion"] for v in listed.json()["reports"][0]["verdicts"]] == [1, 2]
    # an old row and a new one side by side in the list
    new_row = await service.reports.insert(fresh)
    listed = (await _call(app, "GET", "/v1/planner/eval-reports", token=USER)).json()
    by_id = {r["id"]: r for r in listed["reports"]}
    assert (
        len(by_id[row.id]["verdicts"]) == 2 and len(by_id[new_row.id]["verdicts"]) == 3
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stored",
    [
        math.nan,
        math.inf,
        None,
        0.0,
    ],
)
async def test_the_decisions_answer_survives_any_stored_time(
    engine: Any, stored: float | None
) -> None:
    """The table's check admits NaN and +infinity (REAL); the decisions API
    must still produce strict JSON for such a row (a 500 on ``compute explain``
    for one odd row would hide every decision of the job)."""
    import json

    from fastapi.encoders import jsonable_encoder

    from meshforge.controller.api.decision_routes import _out

    nodes = await register_nodes(engine, 3)
    _camp, jobs = await _campaign(engine, nodes, [20, 22])
    await _set_ms(engine, jobs[:1], [stored])
    (record,) = await ExecutionStore(engine).decisions(jobs[0])
    out = _out(record)
    payload = jsonable_encoder(out)
    json.dumps(payload, allow_nan=False)  # what the JSON response does
    if stored is None or stored == 0.0:
        assert payload["decision_ms"] == stored
