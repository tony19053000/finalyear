"""V0.9 G3 lane B tester r1 on a real controller + agent + PostgreSQL:
concurrent submits, CAS races, route-wide admin-only, no secrets in records."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

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


async def test_concurrent_submit_calls_never_double_a_run(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=12))
        cid = created["campaign_id"]
        assert created["run_count"] == 12
        replies = await asyncio.gather(
            *(
                http.post(f"/v1/bench/campaigns/{cid}/submit", params={"count": 5})
                for _ in range(8)
            )
        )
        assert all(r.status_code == 200 for r in replies), [r.text for r in replies]
        outcomes = [o for r in replies for o in r.json()["outcomes"]]
        submitted = [o for o in outcomes if o["outcome"] == "submitted"]
        run_ids = [o["run_id"] for o in submitted]
        assert len(run_ids) == len(set(run_ids)), "a run was reported submitted twice"
        runs = await _runs(http, cid)
        linked = [r for r in runs if r["job_id"]]
        assert len(linked) == len({r["job_id"] for r in linked})  # job_id unique
        jobs = (await http.get("/v1/jobs", params={"limit": 200})).json()["jobs"]
        bench_jobs = [j for j in jobs if j["benchmark"]]
        assert len(bench_jobs) == len(linked), "a run has no job or a job has no run"
        per_run: dict[str, int] = {}
        for job in bench_jobs:
            rid = job["benchmark"]["run_id"]
            per_run[rid] = per_run.get(rid, 0) + 1
        assert set(per_run.values()) <= {1}, per_run
        assert all(r["state"] in {"planned", "submitted"} for r in runs)
        for job in bench_jobs:
            await http.post(f"/v1/jobs/{job['job_id']}/cancel")
    finally:
        await a.stop()


async def test_run_state_cas_races_have_one_winner(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=6))
        store = runtime.bench
        assert store is not None
        runs = await store.runs(created["campaign_id"], limit=10)
        run = runs[0]
        wins = await asyncio.gather(
            *(store.mark_submitted(run.id, f"job_race{i:02d}") for i in range(8))
        )
        assert sum(wins) == 1
        # skip vs submit on a fresh run: exactly one of the two wins
        other = runs[1]
        pair = await asyncio.gather(
            store.mark_skipped(other.id, "x"),
            store.mark_submitted(other.id, "job_race99"),
        )
        assert sorted(pair) == [False, True]
        # a skipped / submitted run can never go back to planned or be skipped again
        assert not await store.mark_skipped(run.id, "late")
    finally:
        await a.stop()


async def test_every_bench_route_in_the_openapi_is_admin_only(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
) -> None:
    schema = json.loads(
        (Path(__file__).parents[3] / "frontend" / "openapi.json").read_text("utf-8")
    )
    routes = [
        (method.upper(), path)
        for path, item in schema["paths"].items()
        if path.startswith("/v1/bench")
        for method in item
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert len(routes) >= 5, routes
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as anon:
        for method, path in routes:
            url = path.replace("{campaign_id}", "camp_01J0000000000000000000000A")
            bare = await anon.request(method, url)
            assert bare.status_code in {401, 403}, (method, path, bare.status_code)
            junk = await anon.request(
                method, url, headers={"authorization": "Bearer nonsense"}
            )
            assert junk.status_code in {401, 403}, (method, path, junk.status_code)


async def test_no_token_or_secret_in_spec_runs_or_records(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    engine: Any,  # noqa: F811
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        token = http.headers["authorization"].split()[-1]
        created = await _create(http, _spec(a.node_id, strategies="[local]", reps=3))
        cid = created["campaign_id"]
        await http.post(f"/v1/bench/campaigns/{cid}/submit", params={"count": 3})
        for run in await _runs(http, cid):
            if run["job_id"]:
                from mf_exec_harness import wait_job

                await wait_job(http, run["job_id"], timeout_s=120)
        records = await _wait_records(engine, cid, 3)
        assert len(records) == 3
        blob = json.dumps(records, default=str) + json.dumps(
            (await http.get(f"/v1/bench/campaigns/{cid}")).json()
        )
        blob += json.dumps(await _runs(http, cid))
        assert token not in blob
        for needle in ("Bearer", "BEGIN PRIVATE", "password"):
            assert needle not in blob
        for record in records:  # D215/checklist 29: identity on EVERY bench record
            assert record["campaign_id"] == cid
            assert record["group_id"] and record["config_id"]
            assert record["repetition"] in {1, 2, 3}
    finally:
        await a.stop()


@pytest.mark.parametrize(
    "spec",
    [
        "name: t\nseed: 1\nbudget: {max_runs: 5}\nfamilies:\n  - family: cpu.sort\n"
        "    params: {n: .nan}\n",
        "name: t\nseed: 1\nbudget: {max_runs: 5}\nfamilies:\n  - family: cpu.sort\n"
        "    params: {n: [.inf]}\n",
        "#" * (300 * 1024),
        "!!python/object/apply:os.system ['true']",
        "a: &x [*x]\n",
        "",
        "\x00",
    ],
    ids=["nan", "inf-list", "oversize", "python-tag", "self-alias", "empty", "nul"],
)
async def test_hostile_spec_is_a_400_never_a_500(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    spec: str,
) -> None:
    resp = await http.post("/v1/bench/campaigns", json={"spec": spec})
    assert resp.status_code in {400, 413, 422}, (resp.status_code, resp.text[:300])


@pytest.mark.parametrize("count", [0, -1, 501, "x"])
async def test_submit_count_bounds(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    count: object,
) -> None:
    resp = await http.post(
        "/v1/bench/campaigns/camp_01J0000000000000000000000A/submit",
        params={"count": count},
    )
    assert resp.status_code in {400, 422}, resp.text


@pytest.mark.parametrize("value", [".nan", "[.inf]", "[1, .nan]"])
async def test_nan_or_inf_param_with_a_real_node_is_a_400_never_a_500(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    value: str,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        spec = (
            "name: t\nseed: 1\nbudget: {max_runs: 5}\nfamilies:\n  - family: cpu.sort\n"
            f"    params: {{n: {value}}}\norigin: {a.node_id}\nstrategies: [local]\n"
        )
        resp = await http.post("/v1/bench/campaigns", json={"spec": spec})
        assert resp.status_code == 400, (resp.status_code, resp.text[:300])
    finally:
        await a.stop()


@pytest.mark.parametrize("hours", [".nan", ".inf", "-.inf"])
async def test_r2_non_finite_max_hours_is_a_400_never_a_500(
    http: httpx.AsyncClient,  # noqa: F811
    runtime: ControllerRuntime,
    grpc_target: str,
    tmp_path: Path,
    hours: str,
) -> None:
    a = await _agent(grpc_target, tmp_path / "a")
    try:
        spec = (
            f"name: t\nseed: 1\nbudget: {{max_runs: 5, max_hours: {hours}}}\n"
            "families:\n  - family: cpu.sort\n"
            f"origin: {a.node_id}\nstrategies: [local]\n"
        )
        resp = await http.post("/v1/bench/campaigns", json={"spec": spec})
        assert resp.status_code == 400, (resp.status_code, resp.text[:300])
    finally:
        await a.stop()
