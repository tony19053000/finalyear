"""V0.14 G3 (D295) on a real controller (PostgreSQL, gRPC, REST, the real
dispatcher and planner) with hand-driven agent sessions (``FakeAgent``):

* ``placement.objective`` > the manifest's ``objective:`` > ``[controller.
  planner] default_objective``, and the one used reaches
  ``plan_decision.objective``, the row's replay and (fix round 1, tester r1
  O1) ``GET /v1/jobs/{id}/decisions`` - which ``compute explain`` prints;
* ``prefer_local`` keeps a job on the submitter's node when no other is
  clearly faster;
* an unknown objective is refused at submit (``400 invalid_input``, the API's
  answer for any refused body - an unknown policy too), in the request or the
  manifest, and writes no job.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_jobs_harness import g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.hashing import sha256_bytes
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.planner import plan
from meshforge.controller.planner.replay import from_replay
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

BUNDLE = os.urandom(50_000)
GIB = 1024**3


async def _sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def _until(check: Callable[[], Any], timeout_s: float = 15.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = await check()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


class Fabric:
    def __init__(
        self,
        http: httpx.AsyncClient,
        pg: DatabaseSettings,
        bundle: str,
        nodes: list[str],
    ) -> None:
        self.http, self.pg, self.bundle, self.nodes = http, pg, bundle, nodes

    async def submit(self, **extra: Any) -> httpx.Response:
        manifest = {"name": "demo", "entrypoint": "python main.py"}
        manifest.update(extra.pop("manifest", {}))
        body = {"manifest": manifest, "bundle_digest": self.bundle, **extra}
        return await self.http.post("/v1/jobs", json=body)

    async def decision(self, job_id: str) -> Any:
        async def row() -> Any:
            rows = await _sql(
                self.pg,
                "SELECT outcome, objective, chosen_node_id, policy, replay, "
                "reason_key FROM plan_decision WHERE job_id = :j ORDER BY id",
                j=job_id,
            )
            return rows[0] if rows else None

        return await _until(row)


@pytest_asyncio.fixture
async def make_fabric(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[Callable[..., Any]]:
    stack: list[Any] = []

    async def make(default_objective: str = "min_time") -> Fabric:
        settings = g2_settings(
            pg_settings,
            jobs={"dispatch_interval_s": 0.05},
            planner={"default_objective": default_objective},
        )
        cm = running_controller(settings)
        runtime = await cm.__aenter__()
        stack.append(("controller", cm))
        channel = NodeChannels(f"127.0.0.1:{runtime.grpc_port}")
        stack.append(("channel", channel))
        http = httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{runtime.http_port}",
            headers={"authorization": f"Bearer {token_of(runtime)}"},
            timeout=10,
        )
        stack.append(("http", http))
        digest = sha256_bytes(BUNDLE)
        assert (
            await http.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        ).status_code == 201
        agents = sorted((FakeAgent(channel) for _ in range(2)), key=lambda a: a.node_id)
        for agent in agents:
            await agent.connect(
                hardware=agent_pb2.HardwareSummary(
                    cpu_model="test",
                    cpu_cores=4,
                    cpu_threads=8,
                    ram_bytes=16 * GIB,
                    capabilities=["exec", "exec-subprocess"],
                ),
                max_cpu_jobs=8,
                max_gpu_jobs=0,
            )
            agent.start_heartbeats(0.5)
            stack.append(("agent", agent))
        return Fabric(http, pg_settings, str(digest), [a.node_id for a in agents])

    yield make
    for kind, item in reversed(stack):
        if kind == "agent":
            item.stop_heartbeats()
            await item.close()
        elif kind == "channel":
            await item.close()
        elif kind == "http":
            await item.aclose()
        else:
            await item.__aexit__(None, None, None)


def _explain_text(decision: dict[str, Any]) -> str:
    """What ``compute explain`` prints for one API decision."""
    from rich.console import Console

    from meshforge.cli.explain_commands import decision_tables

    console = Console(width=400, record=True)
    for table in decision_tables({"decisions": [decision]}):
        console.print(table)
    return console.export_text()


def _replays(row: Any) -> None:
    replay = row.replay if isinstance(row.replay, dict) else json.loads(row.replay)
    request, snapshot, now = from_replay(replay)
    assert request.objective == row.objective
    again = plan(request, snapshot, row.policy, now)
    assert again.chosen is not None and again.chosen.node_id == row.chosen_node_id
    assert again.reason_key == row.reason_key


async def test_request_over_manifest_over_default_reaches_the_decision(
    make_fabric: Callable[..., Any],
) -> None:
    fabric: Fabric = await make_fabric(default_objective="min_network")
    cases = {
        "default": ({}, {}, "min_network"),
        "manifest": ({"objective": "prefer_local"}, {}, "prefer_local"),
        "request": (
            {"objective": "prefer_local"},
            {"objective": "min_time"},
            "min_time",
        ),
    }
    for label, (manifest, placement, want) in cases.items():
        created = await fabric.submit(manifest=manifest, placement=placement)
        assert created.status_code == 201, (label, created.text)
        row = await fabric.decision(created.json()["job_id"])
        assert row.outcome == "dispatched", label
        assert row.objective == want, label
        _replays(row)
        job_id = created.json()["job_id"]
        listed = await fabric.http.get(f"/v1/jobs/{job_id}/decisions")
        assert listed.status_code == 200, listed.text
        decisions = listed.json()["decisions"]
        assert [d["objective"] for d in decisions] == [want] * len(decisions), label
        printed = " ".join(_explain_text(decisions[0]).split())  # titles wrap
        assert f"(policy {row.policy}), objective {want}" in printed, label
        detail = (await fabric.http.get(f"/v1/jobs/{created.json()['job_id']}")).json()
        if placement:
            assert detail["placement"]["objective"] == placement["objective"]
        else:
            assert "objective" not in detail["placement"]


async def test_prefer_local_keeps_the_job_on_the_submitters_node(
    make_fabric: Callable[..., Any],
) -> None:
    fabric: Fabric = await make_fabric()
    for local in fabric.nodes:  # whichever laptop submits keeps the job
        created = await fabric.submit(
            placement={"objective": "prefer_local"}, submitter_node_id=local
        )
        assert created.status_code == 201, created.text
        row = await fabric.decision(created.json()["job_id"])
        assert row.objective == "prefer_local" and row.chosen_node_id == local
        _replays(row)


@pytest.mark.parametrize(
    "body",
    [
        {"placement": {"objective": "fastest"}},
        {"manifest": {"objective": "fastest"}},
        {"placement": {"objective": 3}},
    ],
    ids=["request", "manifest", "not-a-string"],
)
async def test_an_unknown_objective_is_refused_at_submit(
    make_fabric: Callable[..., Any], body: dict[str, Any]
) -> None:
    fabric: Fabric = await make_fabric()
    refused = await fabric.submit(**body)
    # the API's convention for a refused request body (an unknown policy too)
    assert refused.status_code == 400, refused.text
    assert refused.json()["error"]["code"] == "invalid_input"
    assert "objective" in refused.json()["error"]["message"]
    assert await _sql(fabric.pg, "SELECT id FROM job") == []
