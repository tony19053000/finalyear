"""V0.6 G2 criteria 3 and 5 on a real controller (PostgreSQL, gRPC, REST, the
real dispatcher and planner) with a crafted cluster of hand-driven agent
sessions (``FakeAgent``: they never run anything, so the attempts they are
given stay live and hold their slots - exactly what the crafted loads need).

The cluster (roles assigned in node-id order, so ties are predictable):

=====  =====  ======================  =====================================
role   slots  pre-occupied             what makes it special
=====  =====  ======================  =====================================
P      2 CPU  1 (pinned job)           CPU score 900 (strongest)
Q      8 CPU  1                        most free slots (resource_score)
R      2 CPU  1                        holds the job's bundle (cache index)
S      2 CPU  1                        nothing
T      1 CPU  0                        the only idle node (least_loaded)
U      -      -                        no ``exec``: always rejected
=====  =====  ======================  =====================================

* every policy is selectable **per request** (``placement.policy`` = what
  ``compute run --policy`` sends) and places the job where the planner says
  (criterion 5; the pure planner's unit tests show each policy alone);
* precedence: request > manifest ``policy`` > ``[controller.planner]
  default_policy``;
* ``GET /v1/jobs/{id}/decisions`` lists **every** candidate with its rank or
  rejection reason and predicted terms with their sources, and ``compute
  explain`` (a real CLI process) prints them (criterion 3);
* a job that must wait records its decision once per reason set, not per tick.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
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
from meshforge.common.ids import new_command_id
from meshforge.common.manifest import POLICIES
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

BUNDLE = os.urandom(200_000)
GIB = 1024**3
ROLES = ("P", "Q", "R", "S", "T", "U")
SLOTS = {"P": 2, "Q": 8, "R": 2, "S": 2, "T": 1}
SCORES = {"P": 900.0, "Q": 100.0}


@dataclass
class Cluster:
    runtime: ControllerRuntime
    http: httpx.AsyncClient
    nodes: dict[str, str]
    """role -> node_id"""
    bundle: str
    pg: DatabaseSettings

    def role(self, node_id: str) -> str:
        return next(r for r, n in self.nodes.items() if n == node_id)


def _hardware(caps: list[str]) -> agent_pb2.HardwareSummary:
    return agent_pb2.HardwareSummary(
        cpu_model="test",
        cpu_cores=4,
        cpu_threads=8,
        ram_bytes=16 * GIB,
        capabilities=caps,
    )


def _scores_doc(cpu: float) -> str:
    import json

    return json.dumps(
        {"metrics": [{"name": "cpu_fp32_gflops_all", "status": "ok", "mean": cpu}]}
    )


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


def _manifest(**extra: Any) -> dict[str, Any]:
    return {"name": "demo", "entrypoint": "python main.py", **extra}


async def _submit(cluster: Cluster, *, node_id: str | None = None, **extra: Any) -> str:
    placement = dict(extra.pop("placement", {}))
    if node_id is not None:
        placement["node_id"] = node_id
    body = {
        "manifest": _manifest(**extra.pop("manifest", {})),
        "bundle_digest": cluster.bundle,
        "placement": placement,
        **extra,
    }
    created = await cluster.http.post("/v1/jobs", json=body)
    assert created.status_code == 201, created.text
    return str(created.json()["job_id"])


async def _placed_on(cluster: Cluster, job_id: str) -> str:
    async def attempt() -> str | None:
        detail = (await cluster.http.get(f"/v1/jobs/{job_id}")).json()
        attempts = detail["attempts"]
        return attempts[0]["node_id"] if attempts else None

    return str(await _until(attempt))


@pytest_asyncio.fixture
async def make_cluster(
    pg_settings: DatabaseSettings,
) -> AsyncIterator[Callable[..., Any]]:
    stack: list[Any] = []

    async def make(default_policy: str = "cost_model") -> Cluster:
        settings = g2_settings(
            pg_settings,
            jobs={"dispatch_interval_s": 0.05},
            planner={"default_policy": default_policy},
        )
        cm = running_controller(settings)
        runtime = await cm.__aenter__()
        stack.append(("controller", cm))
        channel = NodeChannels(f"127.0.0.1:{runtime.grpc_port}")
        stack.append(("channel", channel))
        http = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{runtime.http_port}",
            headers={"authorization": f"Bearer {token_of(runtime)}"},
            timeout=10,
        )
        stack.append(("http", http))
        digest = sha256_bytes(BUNDLE)
        assert (
            await http.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        ).status_code == 201
        agents = sorted((FakeAgent(channel) for _ in ROLES), key=lambda a: a.node_id)
        nodes: dict[str, str] = {}
        for role, agent in zip(ROLES, agents, strict=True):
            nodes[role] = agent.node_id
            caps = ["docker"] if role == "U" else ["exec", "exec-subprocess"]
            hello: dict[str, Any] = {"hardware": _hardware(caps)}
            if role in SLOTS:
                hello["max_cpu_jobs"] = SLOTS[role]
                hello["max_gpu_jobs"] = 0
            await agent.connect(**hello)
            agent.start_heartbeats(0.5)
            stack.append(("agent", agent))
        cluster = Cluster(runtime, http, nodes, str(digest), pg_settings)
        for role, score in SCORES.items():
            await _sql(
                pg_settings,
                "UPDATE node SET hardware_scores = CAST(:doc AS jsonb) WHERE node_id = :n",
                doc=_scores_doc(score),
                n=nodes[role],
            )
        # R holds the bundle: a real cache-index snapshot from its session.
        r_agent = next(a for a in agents if a.node_id == nodes["R"])
        await r_agent.write(
            agent_pb2.AgentMessage(
                message_id=new_command_id(),
                session_id=r_agent.session_id,
                cache_report=agent_pb2.CacheReport(
                    seq=1,
                    snapshot=True,
                    snapshot_last=True,
                    page=0,
                    added=[
                        agent_pb2.CacheItem(
                            kind=agent_pb2.CacheItem.KIND_BLOB,
                            key=str(digest),
                            size=len(BUNDLE),
                        )
                    ],
                ),
            )
        )
        await _until(
            lambda: _sql(
                pg_settings,
                "SELECT 1 FROM cache_location WHERE node_id = :n",
                n=nodes["R"],
            )
        )
        # Pre-occupy one slot on P, Q, R and S with pinned jobs.
        for role in ("P", "Q", "R", "S"):
            job = await _submit(cluster, node_id=nodes[role])
            assert await _placed_on(cluster, job) == nodes[role]
        return cluster

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


EXPECTED = {
    "least_loaded": "T",
    "strongest_suitable": "P",
    "resource_score": "Q",
    "cache_affinity": "R",
    "cost_model": "R",
    "round_robin": "P",
}


@pytest.mark.parametrize("policy", POLICIES)
async def test_each_policy_per_request_places_where_the_planner_says(
    make_cluster: Callable[..., Any], policy: str
) -> None:
    cluster: Cluster = await make_cluster()
    job = await _submit(cluster, placement={"policy": policy})
    role = cluster.role(await _placed_on(cluster, job))
    decisions = (await cluster.http.get(f"/v1/jobs/{job}/decisions")).json()
    (decision,) = decisions["decisions"]
    assert decision["policy"] == policy and decision["outcome"] == "dispatched"
    assert decision["chosen_node_id"] == cluster.nodes[role]
    if policy == "random":
        assert role in {"P", "Q", "R", "S", "T"}
    else:
        assert role == EXPECTED[policy], decisions
    if policy == "round_robin":
        # the cursor walks on: P is now full, the next after P is Q
        second = await _submit(cluster, placement={"policy": policy})
        assert cluster.role(await _placed_on(cluster, second)) == "Q"


async def test_precedence_request_over_manifest_over_global(
    make_cluster: Callable[..., Any],
) -> None:
    cluster: Cluster = await make_cluster(default_policy="least_loaded")
    by_manifest = await _submit(cluster, manifest={"policy": "cache_affinity"})
    assert cluster.role(await _placed_on(cluster, by_manifest)) == "R"
    by_request = await _submit(
        cluster,
        manifest={"policy": "cache_affinity"},
        placement={"policy": "strongest_suitable"},
    )
    assert cluster.role(await _placed_on(cluster, by_request)) == "P"
    by_default = await _submit(cluster)
    assert cluster.role(await _placed_on(cluster, by_default)) == "T"
    rows = await _sql(
        cluster.pg,
        "SELECT job_id, policy FROM plan_decision WHERE job_id IN (:a, :b, :c)",
        a=by_manifest,
        b=by_request,
        c=by_default,
    )
    assert dict(rows) == {
        by_manifest: "cache_affinity",
        by_request: "strongest_suitable",
        by_default: "least_loaded",
    }


async def test_decisions_show_every_candidate_and_explain_prints_them(
    make_cluster: Callable[..., Any], tmp_path: Path
) -> None:
    cluster: Cluster = await make_cluster()
    job = await _submit(cluster, placement={"policy": "least_loaded"})
    await _placed_on(cluster, job)
    answer = (await cluster.http.get(f"/v1/jobs/{job}/decisions")).json()
    (decision,) = answer["decisions"]
    by_node = {c["node_id"]: c for c in decision["candidates"]}
    assert set(by_node) == set(cluster.nodes.values())  # all six
    rejected = by_node[cluster.nodes["U"]]
    assert rejected["feasible"] is False and rejected["reject_code"] == "not_exec"
    assert rejected["reject_text"]
    ranked = sorted(
        (c for c in decision["candidates"] if c["feasible"]), key=lambda c: c["rank"]
    )
    assert [c["rank"] for c in ranked] == [1, 2, 3, 4, 5]
    for c in ranked:
        terms = c["estimate"]["terms"]
        assert set(terms) == {"queue", "transfer", "env", "compute", "output"}
        assert all(
            t["source"] in {"measured", "history", "scaled", "assumed"}
            for t in terms.values()
        )
        assert c["mode"] == "offload" and c["slot"] == "cpu"
    # the holder moves nothing (measured), everyone else an assumed bandwidth
    assert by_node[cluster.nodes["R"]]["estimate"]["terms"]["transfer"]["source"] == (
        "measured"
    )
    assert by_node[cluster.nodes["S"]]["estimate"]["terms"]["transfer"]["source"] == (
        "assumed"
    )
    assert decision["actual"]["node_id"] == cluster.nodes["T"]
    assert (await cluster.http.get(f"/v1/jobs/{job}/decisions?attempt=2")).json()[
        "count"
    ] == 0

    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": str(cluster.http.base_url).rstrip("/"),
            "MESHFORGE_CLI__TOKEN": token_of(cluster.runtime),
            "MESHFORGE_DATA_DIR": str(tmp_path / "no-agent-here"),
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
            "COLUMNS": "250",
        }
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meshforge.cli",
        "explain",
        job,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await asyncio.wait_for(proc.communicate(), 60)
    assert proc.returncode == 0, err.decode()
    printed = out.decode()
    for node_id in cluster.nodes.values():
        assert node_id in printed
    assert "not_exec" in printed
    assert "(assumed)" in printed and "(measured)" in printed
    assert "dispatched (policy least_loaded)" in printed
    assert "actual: attempt" in printed


async def test_a_waiting_job_records_its_decision_once_per_reason_set(
    make_cluster: Callable[..., Any],
) -> None:
    cluster: Cluster = await make_cluster()
    # fill T (the only idle slot) and every other free slot
    for _ in range(1 + 1 + 7 + 1 + 1):  # T, P, Q x7, R, S
        await _placed_on(
            cluster, await _submit(cluster, placement={"policy": "least_loaded"})
        )
    waiting = await _submit(cluster)
    await asyncio.sleep(1.0)  # ~20 dispatch ticks
    rows = await _sql(
        cluster.pg,
        "SELECT outcome, reason_key FROM plan_decision WHERE job_id = :j",
        j=waiting,
    )
    assert len(rows) == 1 and rows[0][0] == "waiting"
    (decision,) = (await cluster.http.get(f"/v1/jobs/{waiting}/decisions")).json()[
        "decisions"
    ]
    codes = {c["node_id"]: c["reject_code"] for c in decision["candidates"]}
    assert codes[cluster.nodes["U"]] == "not_exec"
    assert all(codes[cluster.nodes[r]] == "slots_full" for r in ("P", "R", "S", "T"))
    # Q's 8 slots meet the controller's own cap (max_attempts_per_node = 8)
    assert codes[cluster.nodes["Q"]] == "node_cap"
    detail = (await cluster.http.get(f"/v1/jobs/{waiting}")).json()
    assert detail["state"] == "queued"
