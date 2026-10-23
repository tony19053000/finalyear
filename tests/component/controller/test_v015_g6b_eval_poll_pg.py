"""V0.15 G6b (D378): evaluation reports are start-then-poll, on a real
PostgreSQL (and, for the restart, a real controller runtime).

* ``POST`` -> 202 ``{report_id, status: running}``; ``GET /{id}`` answers
  ``running`` (report null, Markdown 409) until the evaluation ends, then
  ``done`` with the report; the list carries the status too;
* two concurrent ``POST`` -> one 202, one 409 ``eval_already_running``
  naming the running report; a ``POST`` after it ends is accepted again;
* failures end ``failed`` with a reason: ``invalid_input`` (unknown
  holdout), ``crashed`` (a bug in the evaluation), ``database`` (the final
  write lost too: answered from memory, never ``running``);
* a request without ``background: true`` (a client from before G6b) gets 400
  ``eval_requires_background`` and nothing is stored;
* **restart**: a controller stopped while it evaluates writes ``failed`` /
  ``controller_restart``; a row left ``running`` by a killed controller is
  swept to ``failed`` / ``controller_restart`` when the next one starts, and
  until then any other process answers it ``failed`` - never stuck
  ``running``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from alembic import command
from fastapi import FastAPI
from mf_eval_pg import (
    FAMILY,
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
from meshforge.controller.planner_eval.model import ML_UNAVAILABLE, EvalMl
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore, EvalStoreError
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]


class _Principal:
    user_id = "usr_test"
    name = "t"
    is_admin = True


class _Gate:
    """ML inputs that wait until released: the evaluation stays running."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.entered = asyncio.Event()

    async def prepare(
        self, families: Mapping[str, frozenset[str]], *, allow_leak: bool
    ) -> EvalMl:
        self.entered.set()
        await self.release.wait()
        return EvalMl(ML_UNAVAILABLE, "test")


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


def _service(engine: Any, *, ml: Any = None, reports: Any = None) -> EvalService:
    return EvalService(
        engine,
        CalibrationStore(engine),
        reports or EvalReportStore(engine),
        CalibrationConfig(),
        max_rows=10_000,
        ml=ml,
    )


def _app(service: EvalService) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes.planner_eval_router)
    user = _Principal()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    setattr(
        app.state, routes.PLANNER_EVAL_STATE_KEY, routes.PlannerEvalApi(service=service)
    )
    return app


async def _call(app: FastAPI, method: str, path: str, **kw: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.request(method, path, **kw)


def _getter(app: FastAPI) -> Any:
    return lambda path: _call(app, "GET", path)


async def _campaign(engine: Any) -> str:
    nodes = await register_nodes(engine, 3)
    campaign_id, runs = await make_campaign(engine, nodes, [20, 22])
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
    groups = sorted({r.group_id for r in runs})
    best = {groups[0]: nodes[2], groups[1]: nodes[0]}

    def total(run: Any) -> int:
        fast = best[run.group_id] == run.node_id
        return (1_000 if fast else 3_000) + run.repetition

    await finish_runs(engine, dispatched, total)
    return campaign_id


async def _stored(engine: Any, report_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT report FROM planner_eval_report WHERE id = :id"),
                {"id": report_id},
            )
        ).scalar_one()
    return dict(row)


async def _count(engine: Any) -> int:
    async with engine.connect() as conn:
        return int(
            (
                await conn.execute(text("SELECT count(*) FROM planner_eval_report"))
            ).scalar_one()
        )


# --------------------------------------------------------------------- happy


async def test_202_then_running_then_done(engine: Any) -> None:
    campaign_id = await _campaign(engine)
    gate = _Gate()
    service = _service(engine, ml=gate)
    app = _app(service)
    started = await _call(
        app, "POST", EVAL_PATH, json=bg({"campaign_ids": [campaign_id]})
    )
    assert started.status_code == 202, started.text
    assert started.json() == {
        "report_id": started.json()["report_id"],
        "status": "running",
    }
    rid = started.json()["report_id"]
    await asyncio.wait_for(gate.entered.wait(), 60)

    running = await _call(app, "GET", f"{EVAL_PATH}/{rid}")
    assert running.status_code == 200
    body = running.json()
    assert body["status"] == "running" and body["report"] is None
    assert body["reason"] is None and body["campaign_ids"] == [campaign_id]
    md = await _call(app, "GET", f"{EVAL_PATH}/{rid}", params={"format": "markdown"})
    assert md.status_code == 409
    assert md.json()["error"]["reason"] == "eval_report_not_done"
    listed = (await _call(app, "GET", EVAL_PATH)).json()["reports"]
    assert [(r["id"], r["status"], r["eval_version"]) for r in listed] == [
        (rid, "running", 0)
    ]
    assert listed[0]["verdicts"] == [] and listed[0]["evaluated_groups"] == 0

    gate.release.set()
    done = (await wait_for_report(_getter(app), rid)).json()
    assert done["status"] == "done" and done["reason"] is None
    assert done["report"]["counts"]["evaluated_groups"] == 2
    assert done["report"]["ml"]["status"] == "ml_unavailable"
    assert "run_state" not in await _stored(engine, rid)  # the report itself
    listed = (await _call(app, "GET", EVAL_PATH)).json()["reports"]
    assert listed[0]["status"] == "done" and listed[0]["eval_version"] == 2
    md = await _call(app, "GET", f"{EVAL_PATH}/{rid}", params={"format": "markdown"})
    assert md.status_code == 200 and md.text.startswith("# Planner evaluation report")


async def test_two_concurrent_posts_one_runs_one_409(engine: Any) -> None:
    campaign_id = await _campaign(engine)
    gate = _Gate()
    app = _app(_service(engine, ml=gate))
    body = bg({"campaign_ids": [campaign_id]})
    first, second = await asyncio.gather(
        _call(app, "POST", EVAL_PATH, json=body),
        _call(app, "POST", EVAL_PATH, json=body),
    )
    codes = sorted([first.status_code, second.status_code])
    assert codes == [202, 409], (first.text, second.text)
    accepted = first if first.status_code == 202 else second
    refused = second if accepted is first else first
    rid = accepted.json()["report_id"]
    error = refused.json()["error"]
    assert error["code"] == "conflict" and error["reason"] == "eval_already_running"
    await asyncio.wait_for(gate.entered.wait(), 60)
    # while it runs, a third start names it
    third = await _call(app, "POST", EVAL_PATH, json=body)
    assert third.status_code == 409 and third.json()["error"]["report_id"] == rid
    assert await _count(engine) == 1
    gate.release.set()
    assert (await wait_for_report(_getter(app), rid)).json()["status"] == "done"
    again = await _call(app, "POST", EVAL_PATH, json=body)  # free again
    assert again.status_code == 202
    assert (await wait_for_report(_getter(app), again.json()["report_id"])).json()[
        "status"
    ] == "done"


# ------------------------------------------------------------------ failures


async def test_an_old_synchronous_client_gets_a_clear_error(engine: Any) -> None:
    campaign_id = await _campaign(engine)
    app = _app(_service(engine))
    for body in (
        {"campaign_ids": [campaign_id]},
        {"campaign_ids": [campaign_id], "background": False},
        {"campaign_ids": [campaign_id], "background": None},
    ):
        old = await _call(app, "POST", EVAL_PATH, json=body)
        assert old.status_code == 400, old.text
        error = old.json()["error"]
        assert error["code"] == "invalid_input"
        assert error["reason"] == "eval_requires_background"
        assert "background" in error["message"] and "202" in error["message"]
    assert await _count(engine) == 0


async def test_failures_end_failed_with_a_reason(engine: Any) -> None:
    campaign_id = await _campaign(engine)
    app = _app(_service(engine))
    unknown = await _call(
        app,
        "POST",
        EVAL_PATH,
        json=bg({"campaign_ids": [campaign_id], "holdout": "gpu.nope"}),
    )
    assert unknown.status_code == 202
    got = (await wait_for_report(_getter(app), unknown.json()["report_id"])).json()
    assert (got["status"], got["reason"]) == ("failed", "invalid_input")
    assert "gpu.nope" in got["detail"] and got["report"] is None
    stored = await _stored(engine, got["id"])
    assert stored["run_state"]["status"] == "failed"
    listed = (await _call(app, "GET", EVAL_PATH)).json()["reports"]
    assert (listed[0]["status"], listed[0]["reason"]) == ("failed", "invalid_input")

    missing = await _call(
        app, "POST", EVAL_PATH, json=bg({"campaign_ids": ["camp_nope"]})
    )
    assert missing.status_code == 404  # checked before anything is stored
    assert await _count(engine) == 1

    class _Boom(EvalService):
        async def report(self, *a: Any, **kw: Any) -> dict[str, Any]:
            raise RuntimeError("a bug")

    boom = _Boom(
        engine,
        CalibrationStore(engine),
        EvalReportStore(engine),
        CalibrationConfig(),
        max_rows=10_000,
    )
    app2 = _app(boom)
    started = await _call(
        app2, "POST", EVAL_PATH, json=bg({"campaign_ids": [campaign_id]})
    )
    got = (await wait_for_report(_getter(app2), started.json()["report_id"])).json()
    assert (got["status"], got["reason"], got["detail"]) == (
        "failed",
        "crashed",
        "RuntimeError",
    )
    # the controller is free again after a crash
    assert not boom.busy


async def test_a_lost_final_write_is_answered_from_memory(engine: Any) -> None:
    campaign_id = await _campaign(engine)

    class _LossyStore(EvalReportStore):
        async def finish(self, *a: Any, **kw: Any) -> Any:
            raise EvalStoreError("down", details={"cause": "OperationalError"})

        async def fail(self, *a: Any, **kw: Any) -> Any:
            raise EvalStoreError("down", details={"cause": "OperationalError"})

    app = _app(_service(engine, reports=_LossyStore(engine)))
    started = await _call(
        app, "POST", EVAL_PATH, json=bg({"campaign_ids": [campaign_id]})
    )
    rid = started.json()["report_id"]
    got = (await wait_for_report(_getter(app), rid)).json()
    assert (got["status"], got["reason"]) == ("failed", "database")
    assert "OperationalError" in got["detail"]
    # the database still says running; a fresh process answers it failed too
    assert (await _stored(engine, rid))["run_state"]["status"] == "running"
    other = (await _call(_app(_service(engine)), "GET", f"{EVAL_PATH}/{rid}")).json()
    assert (other["status"], other["reason"]) == ("failed", "controller_restart")


# ------------------------------------------------------------------- restart


async def test_restart_while_running_never_leaves_it_running(
    engine: Any, pg_settings: DatabaseSettings
) -> None:
    campaign_id = await _campaign(engine)
    settings = g2_settings(pg_settings)

    # 1. a controller stopped while it evaluates marks the report itself
    async with running_controller(settings) as rt:
        assert rt.planner_eval is not None
        gate = _Gate()
        rt.planner_eval._ml = gate  # noqa: SLF001 - hold the evaluation open
        async with api_client(rt, token=token_of(rt)) as client:
            started = await client.post(
                EVAL_PATH, json=bg({"campaign_ids": [campaign_id]})
            )
            assert started.status_code == 202, started.text
            first = started.json()["report_id"]
            await asyncio.wait_for(gate.entered.wait(), 60)
            assert (await client.get(f"{EVAL_PATH}/{first}")).json()[
                "status"
            ] == "running"
    stored = await _stored(engine, first)
    assert stored["run_state"]["status"] == "failed"
    assert stored["run_state"]["reason"] == "controller_restart"

    # 2. a killed controller leaves the row running: nobody runs it now
    left = await EvalReportStore(engine).start(
        [campaign_id], holdout=None, leaked=False
    )
    assert (await _stored(engine, left.id))["run_state"]["status"] == "running"
    # any other process already answers it failed, before a sweep
    answered = (
        await _call(_app(_service(engine)), "GET", f"{EVAL_PATH}/{left.id}")
    ).json()
    assert (answered["status"], answered["reason"]) == ("failed", "controller_restart")

    # 3. the next controller's start-up sweep stores it failed
    async with running_controller(settings) as rt:
        assert (await _stored(engine, left.id))["run_state"] == {
            "status": "failed",
            "reason": "controller_restart",
            "detail": "the controller stopped while this evaluation ran; run it again",
        }
        async with api_client(rt, token=token_of(rt)) as client:
            for rid in (first, left.id):
                got = (await client.get(f"{EVAL_PATH}/{rid}")).json()
                assert (got["status"], got["reason"]) == (
                    "failed",
                    "controller_restart",
                ), got
            listed = (await client.get(EVAL_PATH)).json()["reports"]
            assert {r["status"] for r in listed} == {"failed"}
            # and it evaluates again normally after the restart
            again = await client.post(
                EVAL_PATH, json=bg({"campaign_ids": [campaign_id], "holdout": FAMILY})
            )
            assert again.status_code == 202, again.text
            done = await wait_for_report(client.get, again.json()["report_id"])
            assert done.json()["status"] == "done"
