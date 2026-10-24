"""V0.15 G6b tester: start-then-poll on a real PostgreSQL, adversarial.

From brief 5h D378: Markdown 409 before done, the store's running-only
replace rule under concurrency, the start-up sweep touching only running rows,
validation of ids / bodies, stop while the evaluation computes.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

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
from mf_eval_poll import EVAL_PATH, bg, wait_for_report
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from sqlalchemy import text

from meshforge.controller.api import planner_eval_routes as routes
from meshforge.controller.api.auth import current_user, require_admin
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval import service as svc_module
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]


class _P:
    user_id = "usr_test"
    name = "t"
    is_admin = True


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


def _app(service: EvalService) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes.planner_eval_router)
    user = _P()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    setattr(
        app.state, routes.PLANNER_EVAL_STATE_KEY, routes.PlannerEvalApi(service=service)
    )
    return app


async def _call(app: FastAPI, method: str, path: str, **kw: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        return await c.request(method, path, **kw)


async def _campaign(engine: Any) -> str:
    nodes = await register_nodes(engine, 3)
    campaign_id, runs = await make_campaign(engine, nodes, [20, 22])
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
    groups = sorted({r.group_id for r in runs})
    best = {groups[0]: nodes[2], groups[1]: nodes[0]}

    def total(run: Any) -> int:
        return (1_000 if best[run.group_id] == run.node_id else 3_000) + run.repetition

    await finish_runs(engine, dispatched, total)
    return campaign_id


async def _state(engine: Any, rid: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT report FROM planner_eval_report WHERE id = :i"),
                {"i": rid},
            )
        ).scalar_one()
    return dict(row)


# ------------------------------------------------------------- store rules


async def test_finish_and_fail_race_exactly_one_wins(engine: Any) -> None:
    store = EvalReportStore(engine)
    report = {"campaign_ids": ["c"], "holdout": None, "leaked": False, "x": 1}
    for _ in range(10):
        row = await store.start(["c"], holdout=None, leaked=False)
        a, b = await asyncio.gather(
            store.finish(row.id, report), store.fail(row.id, "crashed", "z")
        )
        assert (a is None) != (b is None), (a, b)
        final = await store.get(row.id)
        assert final is not None
        assert final.status == ("done" if a is not None else "failed")


async def test_late_finish_after_sweep_is_dropped_and_a_done_row_is_never_swept(
    engine: Any,
) -> None:
    store = EvalReportStore(engine)
    report = {"campaign_ids": ["c"], "holdout": None, "leaked": False, "x": 1}
    done = await store.start(["c"], holdout=None, leaked=False)
    await store.finish(done.id, report)
    running = [await store.start(["c"], holdout=None, leaked=False) for _ in range(3)]
    assert await store.sweep_running() == 3
    assert await store.sweep_running() == 0  # idempotent
    got = await store.get(done.id)
    assert got is not None and got.status == "done" and got.report["x"] == 1
    # the evaluation that was running finishes late: refused, row stays failed
    assert await store.finish(running[0].id, report) is None
    after = await store.get(running[0].id)
    assert after is not None
    assert (after.status, after.reason) == ("failed", "controller_restart")
    assert await store.fail(running[0].id, "crashed") is None
    again = await store.get(running[0].id)
    assert again is not None and again.reason == "controller_restart"


async def test_a_huge_detail_is_truncated_in_the_database(engine: Any) -> None:
    store = EvalReportStore(engine)
    row = await store.start(["c"], holdout=None, leaked=False)
    await store.fail(row.id, "crashed", ("é" + chr(0x202E)) * 100_000)
    got = await store.get(row.id)
    assert got is not None and got.detail is not None and len(got.detail) <= 500


# ------------------------------------------------------------------- API


async def test_markdown_of_a_failed_report_is_409_and_ids_are_validated(
    engine: Any,
) -> None:
    store = EvalReportStore(engine)
    row = await store.start(["c"], holdout=None, leaked=False)
    await store.fail(row.id, "invalid_input", "no group")
    app = _app(_service(engine))
    md = await _call(app, "GET", f"{EVAL_PATH}/{row.id}", params={"format": "markdown"})
    assert md.status_code == 409
    err = md.json()["error"]
    assert err["reason"] == "eval_report_not_done" and err["status"] == "failed"
    assert "invalid_input" in err["message"]
    as_json = await _call(app, "GET", f"{EVAL_PATH}/{row.id}")
    assert as_json.json()["report"] is None and as_json.json()["detail"] == "no group"

    missing = await _call(app, "GET", f"{EVAL_PATH}/99999")
    assert missing.status_code == 404
    assert missing.json()["error"]["reason"] == "unknown_eval_report"
    md_missing = await _call(
        app, "GET", f"{EVAL_PATH}/99999", params={"format": "markdown"}
    )
    assert md_missing.status_code == 404
    for bad in ("0", "-1", str(2**63), "abc", "1.5"):
        r = await _call(app, "GET", f"{EVAL_PATH}/{bad}")
        assert r.status_code in (400, 404, 422), (bad, r.status_code)
        assert r.status_code != 500, bad
    bad_format = await _call(
        app, "GET", f"{EVAL_PATH}/{row.id}", params={"format": "pdf"}
    )
    assert bad_format.status_code in (400, 422)


async def test_post_validation_never_stores_a_row(engine: Any) -> None:
    app = _app(_service(engine))
    cid = "camp_01AAAAAAAAAAAAAAAAAAAAAAAA"
    bodies: list[Any] = [
        {"campaign_ids": [], "background": True},
        {"campaign_ids": [""], "background": True},
        {"campaign_ids": [cid], "holdout": "", "background": True},
        {"campaign_ids": [cid], "background": "yes-please"},
        {"background": True},
        [],
        None,
    ]
    for body in bodies:
        r = await _call(app, "POST", EVAL_PATH, json=body)
        assert r.status_code in (400, 422), (body, r.status_code, r.text)
    unknown = await _call(
        app, "POST", EVAL_PATH, json=bg({"campaign_ids": ["camp_nope"]})
    )
    assert unknown.status_code == 404
    async with engine.connect() as conn:
        n = (
            await conn.execute(text("SELECT count(*) FROM planner_eval_report"))
        ).scalar()
    assert n == 0


async def test_polling_hammer_never_sees_failed_for_a_run_that_succeeds(
    engine: Any,
) -> None:
    """Many pollers hitting GET/list while the evaluation runs and finishes
    (the race window of the G6b fix): every answer is running or done."""
    campaign_id = await _campaign(engine)
    service = _service(engine)
    app = _app(service)
    started = await _call(
        app, "POST", EVAL_PATH, json=bg({"campaign_ids": [campaign_id]})
    )
    rid = started.json()["report_id"]
    seen: set[str] = set()
    done = asyncio.Event()

    async def poller() -> None:
        while not done.is_set():
            r = await _call(app, "GET", f"{EVAL_PATH}/{rid}")
            seen.add(r.json()["status"])
            ls = await _call(app, "GET", EVAL_PATH)
            seen.add("list:" + ls.json()["reports"][0]["status"])
            await asyncio.sleep(0)

    pollers = [asyncio.create_task(poller()) for _ in range(6)]
    await service.wait()
    for _ in range(20):
        await asyncio.sleep(0.02)
    done.set()
    await asyncio.gather(*pollers)
    assert "failed" not in seen and "list:failed" not in seen, seen
    final = await wait_for_report(lambda p: _call(app, "GET", p), rid)
    assert final.json()["status"] == "done"


async def test_stop_while_the_evaluation_computes_is_prompt_and_marks_the_row(
    engine: Any, pg_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The replay runs in a worker thread; a controller stop must not hang for
    the length of the replay and must still leave the row failed."""
    campaign_id = await _campaign(engine)
    entered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_evaluate(*a: Any, **k: Any) -> Any:
        loop.call_soon_threadsafe(entered.set)
        time.sleep(12)
        raise RuntimeError("never stored")

    monkeypatch.setattr(svc_module, "evaluate", slow_evaluate)
    settings = g2_settings(pg_settings)
    t_stop = 0.0
    async with (
        running_controller(settings) as rt,
        api_client(rt, token=token_of(rt)) as client,
    ):
        started = await client.post(EVAL_PATH, json=bg({"campaign_ids": [campaign_id]}))
        assert started.status_code == 202, started.text
        rid = started.json()["report_id"]
        await asyncio.wait_for(entered.wait(), 60)
        t_stop = time.monotonic()
    stop_took = time.monotonic() - t_stop
    state = (await _state(engine, rid))["run_state"]
    assert (state["status"], state["reason"]) == ("failed", "controller_restart")
    assert stop_took < 11.0, f"controller stop waited {stop_took:.1f}s for the replay"
