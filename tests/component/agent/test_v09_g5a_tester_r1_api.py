"""V0.9 G5 lane A tester r1: the labels route on a real controller + PostgreSQL:
non-admin / anonymous refusal (also for the Parquet form), the 413 cap, 404 for
an unknown campaign, pending on an untouched campaign, and a Parquet form that
equals the JSON one."""

# ruff: noqa: F811
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import tolerant_liveness
from mf_users import bearer, create_user, login
from sqlalchemy import update
from test_v09_g3_forced_runs import (  # noqa: F401
    _agent,
    _create,
    _runs,
    _spec,
    engine,
    http,
)
from test_v09_g5a_labels_db import insert_records, record

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_session_factory
from meshforge.controller.store.models.bench import BenchRun
from meshforge.controller.store.models.telemetry import RunRecord  # noqa: F401

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

CAP = 5


@pytest.fixture
def settings(pg_settings: Any) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1, "backstop_grace_s": 1.0},
        telemetry={"record_interval_s": 0.2, "export_max_rows": CAP},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def user_http(
    runtime: ControllerRuntime, engine: Any
) -> AsyncIterator[httpx.AsyncClient]:
    await create_user(engine, "plainuser", "correct horse battery")
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        token = await login(anon, "plainuser", "correct horse battery")
    async with httpx.AsyncClient(
        base_url=base, headers=bearer(token), timeout=30
    ) as client:
        yield client


async def test_non_admin_and_anonymous_are_refused_for_both_formats(
    http: httpx.AsyncClient,
    user_http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        cid = (await _create(http, _spec(a.node_id, strategies="[local]")))[
            "campaign_id"
        ]
        url = f"/v1/bench/campaigns/{cid}/labels"
        for params in ({}, {"format": "parquet"}):
            denied = await user_http.get(url, params=params)
            assert denied.status_code == 403, (params, denied.status_code, denied.text)
            assert b"PAR1" not in denied.content and "groups" not in denied.text
            async with httpx.AsyncClient(
                base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
            ) as anon:
                bare = await anon.get(url, params=params)
                assert bare.status_code in {401, 403}, bare.status_code
        # a non-admin cannot probe which campaigns exist either
        ghost = await user_http.get(
            "/v1/bench/campaigns/camp_01J0000000000000000000000A/labels"
        )
        assert ghost.status_code == 403
        ok = await http.get(url)
        assert ok.status_code == 200
    finally:
        await a.stop()


async def test_unknown_campaign_is_404_in_both_formats(
    http: httpx.AsyncClient, runtime: ControllerRuntime
) -> None:
    for params in ({}, {"format": "parquet"}):
        resp = await http.get(
            "/v1/bench/campaigns/camp_01J0000000000000000000000A/labels", params=params
        )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "not_found"


async def test_cap_413_then_parquet_equals_json_below_it(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        created = await _create(
            http, _spec(a.node_id, strategies="[local]", reps=CAP + 1)
        )
        cid = created["campaign_id"]
        url = f"/v1/bench/campaigns/{cid}/labels"
        runs = await _runs(http, cid)
        assert len(runs) == CAP + 1
        # untouched: pending, nothing to cap
        before = await http.get(url)
        assert before.status_code == 200
        assert before.json()["groups"][0]["label"] == "pending"
        # CAP + 1 ok records of the campaign -> over the cap
        async with create_session_factory(engine)() as db, db.begin():
            for i, run in enumerate(runs):
                await db.execute(
                    update(BenchRun)
                    .where(BenchRun.id == run["run_id"])
                    .values(state="done", job_id=f"job_cap{i}")
                )
        from meshforge.controller.bench.store import BenchStore

        rows = await BenchStore(engine).runs(cid, limit=100)
        await insert_records(
            engine,
            [
                record(r, 10 + i, job_id=f"job_cap{i}", at_s=i)
                for i, r in enumerate(rows)
            ],
        )
        for params in ({}, {"format": "parquet"}):
            over = await http.get(url, params=params)
            assert over.status_code == 413, (params, over.status_code, over.text[:200])
            assert b"PAR1" not in over.content
            assert over.json()["error"]["code"] == "invalid_input"
    finally:
        await a.stop()
