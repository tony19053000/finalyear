"""V0.9 G4 lane A tester r1: submit_next paging edge cases (0.5) on a real
controller - bounded work when everything defers, count larger than the
planned runs, count validation, repeated drain."""

# ruff: noqa: F811
from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from test_v09_g4a_bench_item0 import (  # noqa: F401  (fixtures are imported)
    POLICY,
    _agent,
    _cancel_all,
    _create,
    _runs,
    _spec,
    engine,
    http,
    settings,
)

from meshforge.controller.bench import submit as submit_module
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(300)]


async def test_everything_deferring_terminates_and_submits_nothing(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    # the only node refuses every run for a per-run reason (policy_ram)
    a = await _agent(grpc_target, tmp_path / "a", max_ram_bytes=64 * 2**20)
    try:
        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=6))
        body = (
            await http.post(
                f"/v1/bench/campaigns/{created['campaign_id']}/submit",
                params={"count": 3},
            )
        ).json()
        assert body["submitted"] == 0
        assert body["deferred"] == 6 and len(body["outcomes"]) == 6  # each tried once
        assert body["counts"]["planned"] == 6
    finally:
        await a.stop()


async def test_examined_bound_stops_a_call_and_the_next_call_continues(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(submit_module, "MAX_EXAMINED", 4)
    a = await _agent(grpc_target, tmp_path / "a", max_ram_bytes=64 * 2**20)
    try:
        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=10))
        body = (
            await http.post(
                f"/v1/bench/campaigns/{created['campaign_id']}/submit",
                params={"count": 5},
            )
        ).json()
        assert len(body["outcomes"]) == 4  # bounded, not the whole campaign
        assert body["submitted"] == 0
    finally:
        await a.stop()


async def test_count_beyond_the_planned_runs_submits_all_then_nothing_more(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=3))
        campaign_id = created["campaign_id"]
        first = (
            await http.post(
                f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 50}
            )
        ).json()
        assert first["submitted"] == 3 and first["counts"]["planned"] == 0
        again = (
            await http.post(
                f"/v1/bench/campaigns/{campaign_id}/submit", params={"count": 50}
            )
        ).json()
        assert again["submitted"] == 0 and again["outcomes"] == []
        jobs = {r["job_id"] for r in await _runs(http, campaign_id)}
        assert len(jobs) == 3 and None not in jobs  # one job per run, no duplicates
        await _cancel_all(http, campaign_id)
    finally:
        await a.stop()


@pytest.mark.parametrize("count", [0, -1, 10_000_000])
async def test_submit_count_is_validated(
    http: httpx.AsyncClient,
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    count: int,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=3))
        resp = await http.post(
            f"/v1/bench/campaigns/{created['campaign_id']}/submit",
            params={"count": count},
        )
        assert resp.status_code == 400, resp.text
        assert (await _runs(http, created["campaign_id"]))[0]["job_id"] is None
    finally:
        await a.stop()
