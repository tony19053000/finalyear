"""V0.9 G5c (0.9.8): ``GET /v1/bench/campaigns/{id}/quality`` on a real
controller, real agents and PostgreSQL.

A 2-config campaign (cpu.primes: local on A, mode1 on B; 3 repetitions) is
reported ``pending`` before anything runs, then submitted and run for real;
once every record is written the report sees 3 repetitions per config with a
CV, no infrastructure failure (criterion 1 met), criterion 3 met, the same
medians as the labels and the same rate as ``status``; two calls are
byte-identical (D222); errors are typed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_exec_harness import wait_job
from test_v09_g3_forced_runs import (  # noqa: F401
    _agent,
    _create,
    _runs,
    _spec,
    _wait_records,
    engine,
    http,
    settings,
)

from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]


async def _quality(http: httpx.AsyncClient, cid: str) -> httpx.Response:  # noqa: F811
    return await http.get(f"/v1/bench/campaigns/{cid}/quality")


async def test_quality_of_a_real_campaign(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,  # noqa: F811
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    b = await _agent(grpc_target, tmp_path / "b")
    try:
        cid = (await _create(http, _spec(a.node_id)))["campaign_id"]
        before = await _quality(http, cid)
        assert before.status_code == 200, before.text
        doc = before.json()
        assert doc["quality_version"] == 1 and doc["summary"]["pending"]
        assert doc["summary"]["crit1_infra_ok"] is None
        assert doc["summary"]["runs_by_state"]["planned"] == 6
        assert [c["n"] for c in doc["configs"]] == [0, 0]

        sub = await http.post(f"/v1/bench/campaigns/{cid}/submit", params={"count": 6})
        assert sub.status_code == 200 and sub.json()["submitted"] == 6, sub.text
        for run in await _runs(http, cid):
            done = await wait_job(http, run["job_id"], timeout_s=120)
            assert done["state"] == "succeeded", done
        assert len(await _wait_records(engine, cid, 6)) == 6

        first = await _quality(http, cid)
        second = await _quality(http, cid)
        assert first.status_code == 200, first.text
        assert first.content == second.content  # D222
        doc = first.json()
        s = doc["summary"]
        assert s["campaign_state"] == "done" and not s["pending"]
        assert s["records"] == 6 and s["infra_failures"] == 0
        assert s["infra_failure_rate"] == 0.0 and s["crit1_infra_ok"] is True
        assert s["crit3_reps_ok"] is True
        assert s["runs_by_state"]["done"] == 6
        for config in doc["configs"]:
            assert config["n"] == 3 and config["done"] == 3 and config["planned"] == 3
            assert config["total_ms"]["cv"] is not None
            assert config["compute_per_iter_ns"]["n"] == 3
            assert not config["reps_flag"]

        labels = (await http.get(f"/v1/bench/campaigns/{cid}/labels")).json()
        lab = {c["config_id"]: c for g in labels["groups"] for c in g["configs"]}
        for config in doc["configs"]:
            assert config["total_ms"] == lab[config["config_id"]]["total_ms"]
        status = (await http.get(f"/v1/bench/campaigns/{cid}/status")).json()
        assert status["infra_failure_rate"] == s["infra_failure_rate"]
    finally:
        await b.stop()
        await a.stop()


async def test_quality_errors_are_typed(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
) -> None:
    missing = await _quality(http, "camp_01J0000000000000000000000A")
    assert missing.status_code == 404, missing.text
    # The reason tells it from an unmatched route's 404 (the dashboard's
    # "not available on this controller" state); both are `not_found`.
    assert missing.json()["error"] == {
        "code": "not_found",
        "message": "no such campaign",
        "reason": "unknown_campaign",
    }
    unrouted = await http.get(
        "/v1/bench/campaigns/camp_01J0000000000000000000000A/nope"
    )
    assert unrouted.status_code == 404, unrouted.text
    assert unrouted.json()["error"]["code"] == "not_found"
    assert "reason" not in unrouted.json()["error"]
    bad = await _quality(http, "not-a-campaign")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_input"
