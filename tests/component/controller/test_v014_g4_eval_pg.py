"""V0.14 G4 end to end on a real PostgreSQL: a small campaign through the real
dispatcher -> labels + records + replays from the database -> the evaluator
-> the report stored -> the API returns it -> the CLI prints it.

* 3 nodes, ``cpu.sort`` x 2 groups, ``local`` + 2 x ``mode1``, 3 repetitions
  (18 forced runs); every run is submitted like the campaign submitter does
  and placed by real dispatch ticks, so every ``plan_decision`` row carries
  the replay with its shadow request (D215, D292);
* the measured totals make ``node_c`` best in group 1 and the origin best in
  group 2; the expected picks are computed **independently** from the rows
  read back (``shadow_from_replay`` -> ``plan`` per record, majority) and the
  report must agree pick for pick, regret for regret;
* ``POST /v1/planner/eval-reports`` (admin; 403 for a user) -> 202 and,
  polled (V0.15 G6b, D378), the stored row; ``GET`` list (summaries, newest
  first) and ``GET /{id}`` (the same report, and its Markdown); unknown
  campaign / report -> 404; an unknown ``holdout`` family -> the report ends
  ``failed`` / ``invalid_input``; the database down -> 503;
* the CLI's renderer prints it (verdict lines, policy table);
* the same inputs give the same report bytes (a second evaluation).
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from alembic import command
from fastapi import FastAPI
from mf_eval_poll import bg, wait_for_report
from mf_eval_pg import (
    FAMILY,
    dispatch_runs,
    finish_runs,
    make_campaign,
    planner,
    register_nodes,
)
from sqlalchemy import text

from meshforge.cli.planner_commands import eval_lines, eval_table
from meshforge.controller.api import planner_eval_routes as routes
from meshforge.controller.api.auth import current_user, require_admin
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner import plan
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner.replay import shadow_from_replay
from meshforge.controller.planner_eval.mapping import config_for, pick_of
from meshforge.controller.planner_eval.report import report_text
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]


class _Principal:
    def __init__(self, *, admin: bool) -> None:
        self.user_id = "usr_test"
        self.name = "t"
        self.is_admin = admin


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


def _service(engine: Any) -> EvalService:
    return EvalService(
        engine,
        CalibrationStore(engine),
        EvalReportStore(engine),
        CalibrationConfig(),
        max_rows=10_000,
    )


def _app(engine: Any, *, admin: bool = True) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes.planner_eval_router)
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
        routes.PLANNER_EVAL_STATE_KEY,
        routes.PlannerEvalApi(service=_service(engine)),
    )
    return app


async def _call(app: FastAPI, method: str, path: str, **kw: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, **kw)


async def _campaign(engine: Any) -> tuple[str, list[str]]:
    nodes = await register_nodes(engine, 3)
    campaign_id, runs = await make_campaign(engine, nodes, [20, 22])
    assert len(runs) == 18
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
    groups = sorted({r.group_id for r in runs})
    best = {groups[0]: nodes[2], groups[1]: nodes[0]}

    def total(run: Any) -> int:
        fast = best[run.group_id] == run.node_id
        return (
            1_000 if fast else 3_000 + 500 * nodes.index(run.node_id)
        ) + run.repetition

    await finish_runs(engine, dispatched, total)
    return campaign_id, nodes


async def _expected_votes(
    engine: Any, campaign_id: str, policy: str
) -> dict[str, Counter[str]]:
    """Independent of the evaluator: per group, the configuration each record's
    shadow replay picks under ``policy`` (read straight from the tables)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT r.group_id, r.config_id, d.replay FROM run_record r "
                    "JOIN plan_decision d ON d.attempt_id = r.attempt_id "
                    "WHERE r.campaign_id = :c AND d.outcome = 'dispatched'"
                ),
                {"c": campaign_id},
            )
        ).all()
        configs = (
            await conn.execute(
                text(
                    "SELECT DISTINCT group_id, config_id, strategy, node_id, "
                    "origin_node_id FROM bench_run WHERE campaign_id = :c"
                ),
                {"c": campaign_id},
            )
        ).all()
    from meshforge.controller.planner_eval.model import ConfigRef

    by_group: dict[str, list[ConfigRef]] = {}
    for c in configs:
        by_group.setdefault(c.group_id, []).append(
            ConfigRef(c.config_id, c.strategy, c.node_id, c.origin_node_id)
        )
    votes: dict[str, Counter[str]] = {}
    for group_id, _config_id, replay in rows:
        shadow = shadow_from_replay(replay)
        assert shadow is not None, "a forced run's replay has its shadow request"
        request, snapshot, now = shadow
        decision = plan(request, snapshot, policy, now)
        origin = by_group[group_id][0].origin_node_id
        found = config_for(pick_of(decision), origin, by_group[group_id])
        votes.setdefault(group_id, Counter())[
            "unmeasured" if found is None else found.config_id
        ] += 1
    return votes


async def test_campaign_to_report_to_api_to_cli(engine: Any) -> None:
    campaign_id, _nodes = await _campaign(engine)

    refused = await _call(
        _app(engine, admin=False),
        "POST",
        "/v1/planner/eval-reports",
        json={"campaign_ids": [campaign_id]},
    )
    assert refused.status_code == 403

    app = _app(engine)
    created = await _call(
        app,
        "POST",
        "/v1/planner/eval-reports",
        json=bg({"campaign_ids": [campaign_id]}),
    )
    assert created.status_code == 202, created.text
    assert created.json()["status"] == "running"
    record = (
        await wait_for_report(
            lambda p: _call(app, "GET", p), created.json()["report_id"]
        )
    ).json()
    assert record["status"] == "done" and record["reason"] is None
    report = record["report"]
    assert record["campaign_ids"] == [campaign_id] and record["leaked"] is False
    counts = report["counts"]
    assert counts["groups"] == 2 and counts["evaluated_groups"] == 2
    assert counts["labels"]["best"] == 2
    assert counts["records"] == 18 and counts["records_used"] == 18
    assert sum(counts["records_skipped"].values()) == 0
    assert [c["family"] for c in report["calibrations"]] == [FAMILY]
    assert report["calibrations"][0]["source"] == "none"  # no matched runs yet

    # every pick and regret agrees with an independent replay of the rows
    groups = {g["group_id"]: g for g in report["groups"]}
    for policy in ("cost_model", "round_robin", "random", "least_loaded"):
        expected = await _expected_votes(engine, campaign_id, policy)
        key = "cost_model:uncalibrated:min_time" if policy == "cost_model" else policy
        for group_id, votes in expected.items():
            (pick,) = [p for p in groups[group_id]["picks"] if p["key"] == key]
            assert pick["votes"] == dict(sorted(votes.items())), (policy, group_id)
            top = max(votes.values())
            winner = sorted(k for k, n in votes.items() if n == top)[0]
            assert pick["config_id"] == winner
            medians = {
                c["config_id"]: c["median_ms"] for c in groups[group_id]["configs"]
            }
            best = groups[group_id]["best_median_ms"]
            want = (medians[winner] - best) / best * 100.0
            assert pick["regret_pct"] == pytest.approx(want, abs=1e-6)
    regrets = [
        p["regret_pct"]
        for g in groups.values()
        for p in g["picks"]
        if p["status"] == "counted"
    ]
    # not vacuous: some policy is wrong somewhere, some is right
    assert any(r > 0 for r in regrets) and any(r == 0 for r in regrets)
    for g in groups.values():
        (oracle,) = [p for p in g["picks"] if p["key"] == "oracle"]
        assert oracle["config_id"] == g["best_config_id"] and oracle["regret_pct"] == 0

    # stored: list and detail return it; markdown renders it
    listed = (await _call(app, "GET", "/v1/planner/eval-reports")).json()["reports"]
    assert [r["id"] for r in listed] == [record["id"]]
    assert listed[0]["evaluated_groups"] == 2
    assert [v["criterion"] for v in listed[0]["verdicts"]] == [1, 2, 3]  # + G6 latency
    detail = await _call(app, "GET", f"/v1/planner/eval-reports/{record['id']}")
    assert detail.status_code == 200 and detail.json() == record
    md = await _call(
        app,
        "GET",
        f"/v1/planner/eval-reports/{record['id']}",
        params={"format": "markdown"},
    )
    assert md.status_code == 200
    assert md.headers["content-type"].startswith("text/markdown")
    assert md.text.startswith("# Planner evaluation report")

    # the CLI's renderer
    lines = eval_lines(record)
    assert lines[0].startswith(f"report {record['id']}: campaigns {campaign_id}")
    assert any(line.startswith("criterion 1: too_few_groups") for line in lines)
    assert eval_table(report).row_count == len(report["policies"])

    # the same inputs give the same report bytes
    again = await _service(engine).report([campaign_id])
    assert report_text(again) == report_text(report)
    rows = await _sql_count(engine)
    assert rows == 1  # report() alone stores nothing


async def _sql_count(engine: Any) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(text("SELECT count(*) FROM planner_eval_report"))
            ).scalar_one()
        )


async def test_errors_unknown_campaign_report_family_and_database_down(
    engine: Any, pg_settings: DatabaseSettings
) -> None:
    campaign_id, _nodes = await _campaign(engine)
    app = _app(engine)
    missing = await _call(
        app,
        "POST",
        "/v1/planner/eval-reports",
        json=bg({"campaign_ids": ["camp_nope"]}),
    )
    assert (
        missing.status_code == 404 and "camp_nope" in missing.json()["error"]["message"]
    )
    family = await _call(
        app,
        "POST",
        "/v1/planner/eval-reports",
        json=bg({"campaign_ids": [campaign_id], "holdout": "gpu.matmul"}),
    )
    assert family.status_code == 202, family.text
    failed = (
        await wait_for_report(
            lambda p: _call(app, "GET", p), family.json()["report_id"]
        )
    ).json()
    assert failed["status"] == "failed" and failed["reason"] == "invalid_input"
    assert "gpu.matmul" in failed["detail"] and failed["report"] is None
    held = await _call(
        app,
        "POST",
        "/v1/planner/eval-reports",
        json=bg(
            {"campaign_ids": [campaign_id], "holdout": FAMILY, "allow_leak": True}
        ),
    )
    assert held.status_code == 202, held.text
    body = (
        await wait_for_report(lambda p: _call(app, "GET", p), held.json()["report_id"])
    ).json()
    assert body["holdout"] == FAMILY and body["leaked"] is True
    assert {v["status"] for v in body["report"]["verdicts"]} == {"leaked"}
    empty = await _call(
        app, "POST", "/v1/planner/eval-reports", json=bg({"campaign_ids": []})
    )
    assert empty.status_code == 400
    gone = await _call(app, "GET", "/v1/planner/eval-reports/999999")
    assert gone.status_code == 404
    # D321: the reason tells "report not found" from an unmatched route's 404
    assert gone.json()["error"]["reason"] == "unknown_eval_report"
    unrouted = await _call(app, "GET", "/v1/planner/eval-reports-nope")
    assert unrouted.status_code == 404
    assert "reason" not in unrouted.json()["error"]
    assert (await _call(app, "GET", "/v1/planner/eval-reports/0")).status_code == 400

    dead = create_engine(
        DatabaseSettings(
            host="127.0.0.1", port=1, name="x", user="u", connect_timeout_s=2.0
        )
    )
    try:
        down = _app(dead)
        for method, path, kw in (
            ("GET", "/v1/planner/eval-reports", {}),
            ("GET", "/v1/planner/eval-reports/1", {}),
            (
                "POST",
                "/v1/planner/eval-reports",
                {"json": bg({"campaign_ids": [campaign_id]})},
            ),
        ):
            answer = await _call(down, method, path, **kw)
            assert answer.status_code == 503, (path, answer.text)
            assert answer.json()["error"]["code"] == "network_error"
            assert answer.headers.get("retry-after") == "2"
    finally:
        await dead.dispose()
