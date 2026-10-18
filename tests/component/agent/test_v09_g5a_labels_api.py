"""V0.9 G5 lane A: ``GET /v1/bench/campaigns/{id}/labels`` on a real controller,
real agents and PostgreSQL.

A 2-config campaign (cpu.primes: local on A, mode1 on B; 3 repetitions) is
labelled ``pending`` before anything runs, then submitted and run for real;
once every record is written the label is decided (``best`` or ``tie`` - real
timings, so either is honest) from 3 repetitions per config, the Parquet form
is streamed (chunked), byte-identical across two downloads (D222) and equal to
the pure export of the same records; errors are typed.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq
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
from meshforge.controller.telemetry.labels import build_labels
from meshforge.controller.telemetry.labels_export import (
    LABEL_COLUMN_NAMES,
    labels_parquet_bytes,
)
from meshforge.controller.telemetry.labels_store import label_rows, planned_runs

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]


async def _labels(http: httpx.AsyncClient, cid: str, **params: str) -> httpx.Response:  # noqa: F811
    return await http.get(f"/v1/bench/campaigns/{cid}/labels", params=params)


async def test_labels_of_a_real_campaign(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,  # noqa: F811
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    b = await _agent(grpc_target, tmp_path / "b")
    try:
        created = await _create(http, _spec(a.node_id))
        cid = created["campaign_id"]
        before = await _labels(http, cid)
        assert before.status_code == 200, before.text
        doc = before.json()
        assert doc["labels_version"] == 1 and doc["rows"] == 0
        assert [g["label"] for g in doc["groups"]] == ["pending"]
        assert doc["counts"] == {"best": 0, "tie": 0, "insufficient": 0, "pending": 1}

        sub = await http.post(f"/v1/bench/campaigns/{cid}/submit", params={"count": 6})
        assert sub.status_code == 200 and sub.json()["submitted"] == 6, sub.text
        for run in await _runs(http, cid):
            done = await wait_job(http, run["job_id"], timeout_s=120)
            assert done["state"] == "succeeded", done
        assert len(await _wait_records(engine, cid, 6)) == 6

        resp = await _labels(http, cid)
        assert resp.status_code == 200, resp.text
        doc = resp.json()
        assert doc["campaign_id"] == cid and doc["rows"] == 6
        assert doc["campaign_state"] == "done"
        (group,) = doc["groups"]
        assert group["family"] == "cpu.primes" and group["condition"] == "none"
        assert group["label"] in {"best", "tie"}, group
        assert len(group["configs"]) == 2
        for config in group["configs"]:
            assert config["state"] == "measured" and config["repetitions"] == 3
            total = config["total_ms"]
            assert (
                total["n"] == 3
                and total["ci_low"] <= total["median"] <= total["ci_high"]
            )
            assert config["compute_per_iter_ns"]["n"] == 3
            assert config["planner_node_id"] in {a.node_id, b.node_id}
        if group["label"] == "best":
            assert group["best_node_id"] in {a.node_id, b.node_id}
            assert isinstance(group["planner_agrees"], bool)
            assert group["margin_pct"] >= 0
        else:
            assert len(group["tied_config_ids"]) == 2

        # Parquet: streamed, deterministic, same as the pure export (D222)
        first = await _labels(http, cid, format="parquet")
        second = await _labels(http, cid, format="parquet")
        assert first.status_code == 200, first.text
        assert first.headers["content-type"] == "application/vnd.apache.parquet"
        assert "content-length" not in first.headers
        assert first.headers["x-meshforge-label-rows"] == "2"
        assert first.content == second.content
        pure = labels_parquet_bytes(
            build_labels(
                cid,
                await planned_runs(engine, cid),
                await label_rows(engine, cid, limit=1000),
            )
        )
        assert first.content == pure
        table = pq.read_table(io.BytesIO(first.content))
        assert table.column_names == list(LABEL_COLUMN_NAMES)
        assert set(table.column("label").to_pylist()) == {group["label"]}
    finally:
        await b.stop()
        await a.stop()


async def test_label_errors_are_typed(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
) -> None:
    missing = await _labels(http, "camp_01J0000000000000000000000A")
    assert missing.status_code == 404, missing.text
    bad = await _labels(http, "not-a-campaign")
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_input"
    fmt = await _labels(http, "camp_01J0000000000000000000000A", format="csv")
    assert fmt.status_code in {400, 422}, fmt.text
