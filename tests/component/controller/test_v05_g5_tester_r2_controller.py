"""V0.5 G5 tester round 2 - controller side, real PostgreSQL + real gRPC/HTTP.

* N2: a node lost mid-prepare (goodbye / silence) ends the request node_lost;
  a restart within the lease gets the SAME request re-sent; a late real ``done``
  replaces node_lost; a stale non-final report never does; other nodes' requests
  are untouched; re-send is per Reconcile (never twice per session);
* T2: 30+ concurrent pushes to one name and to several names at once: every
  push succeeds, versions are exactly 1..N, the API stays responsive meanwhile.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

from meshforge.common.cas import MIN_CHUNK_SIZE
from meshforge.common.ids import new_command_id, new_node_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.tree import BuiltTree, build_tree
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]
CS = MIN_CHUNK_SIZE


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,  # type: ignore[arg-type]
        blobs={"max_blob_bytes": 16 * 1024 * 1024},
        gc={"interval_s": 3600},
    )
    async with running_controller(settings) as rt:
        yield rt


@pytest_asyncio.fixture
async def http(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with api_client(runtime, token=token_of(runtime)) as client:
        yield client


async def _until(check: Any, timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not check():
        assert time.monotonic() < deadline, "condition never became true"
        await asyncio.sleep(0.05)


async def _reconcile(agent: FakeAgent) -> None:
    await agent.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=agent.session_id,
            reconcile=agent_pb2.Reconcile(),
        )
    )


async def _status(
    agent: FakeAgent, request_id: str, *, done: bool, state: str = "built"
) -> None:
    await agent.write(
        agent_pb2.AgentMessage(
            message_id=new_command_id(),
            session_id=agent.session_id,
            prepare_env_status=agent_pb2.PrepareEnvStatus(
                request_id=request_id,
                done=done,
                items=[
                    agent_pb2.PrepareItem(
                        kind="env", name="0", state=state, key="venv/abc"
                    )
                ],
            ),
        )
    )


def _prepares(agent: FakeAgent) -> list[str]:
    return [m.prepare_env.request_id for m in agent.messages("prepare_env")]


async def _start_prepare(http: httpx.AsyncClient, node_id: str) -> str:
    got = await http.post(
        f"/v1/nodes/{node_id}/prepare",
        json={
            "envs": [
                {"base": "python-cpu", "requirements": "", "runtime": "subprocess"}
            ]
        },
    )
    assert got.status_code == 202, got.text
    return str(got.json()["request_id"])


async def _get(http: httpx.AsyncClient, node_id: str, rid: str) -> dict[str, Any]:
    got = await http.get(f"/v1/nodes/{node_id}/prepare/{rid}")
    assert got.status_code == 200, got.text
    return dict(got.json())


# ----------------------------------------------------------------------- N2


async def test_restart_within_the_lease_resends_the_same_request_once_per_session(
    runtime: ControllerRuntime, http: httpx.AsyncClient
) -> None:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as ch:
        a = FakeAgent(ch, node_id=new_node_id())
        await a.connect()
        a.start_heartbeats(0.1)
        await _reconcile(a)
        rid = await _start_prepare(http, a.node_id)
        await _until(lambda: _prepares(a) == [rid])
        # the agent process restarts quickly: new epoch, new session
        b = FakeAgent(ch, node_id=a.node_id, epoch=2)
        await b.connect()
        b.start_heartbeats(0.1)
        await _reconcile(b)
        await _until(lambda: _prepares(b) == [rid])
        await asyncio.sleep(0.5)
        assert _prepares(b) == [rid]  # once, not repeatedly
        assert (await _get(http, a.node_id, rid))["done"] is False
        await _status(b, rid, done=True)
        got: dict[str, Any] = {}
        for _ in range(100):
            got = await _get(http, a.node_id, rid)
            if got["done"]:
                break
            await asyncio.sleep(0.05)
        assert got["done"] and got["ok"] is True, got
        # finished: another new session does not get it again
        c = FakeAgent(ch, node_id=a.node_id, epoch=3)
        await c.connect()
        c.start_heartbeats(0.1)
        await _reconcile(c)
        await asyncio.sleep(0.5)
        assert _prepares(c) == []
        for ag in (a, b, c):
            await ag.close()


async def test_node_lost_by_goodbye_then_late_real_done_wins_stale_progress_does_not(
    runtime: ControllerRuntime, http: httpx.AsyncClient
) -> None:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as ch:
        a = FakeAgent(ch, node_id=new_node_id())
        await a.connect()
        a.start_heartbeats(0.1)
        await _reconcile(a)
        other = FakeAgent(ch, node_id=new_node_id())
        await other.connect()
        other.start_heartbeats(0.1)
        await _reconcile(other)
        rid = await _start_prepare(http, a.node_id)
        rid_other = await _start_prepare(http, other.node_id)
        await _until(lambda: _prepares(a) == [rid])
        await _status(a, rid, done=False, state="running")
        a.stop_heartbeats()
        await a.close(graceful=True)
        got: dict[str, Any] = {}
        for _ in range(100):
            got = await _get(http, a.node_id, rid)
            if got["done"]:
                break
            await asyncio.sleep(0.1)
        assert got["done"] and got["ok"] is False, got
        assert {i["failure_type"] for i in got["items"] if i["state"] == "failed"} == {
            "node_lost"
        }
        assert (await _get(http, other.node_id, rid_other))["done"] is False
        # the node comes back (same process): a stale progress report replay
        # never revives the request, the real final report is the truth
        back = FakeAgent(ch, node_id=a.node_id, epoch=5)
        await back.connect()
        back.start_heartbeats(0.1)
        await _reconcile(back)
        await asyncio.sleep(0.3)
        assert _prepares(back) == []  # a finished (lost) request is not re-sent
        await _status(back, rid, done=False, state="running")
        await asyncio.sleep(0.2)
        again = await _get(http, a.node_id, rid)
        assert again["ok"] is False and again["done"] is True
        await _status(back, rid, done=True, state="built")
        for _ in range(100):
            again = await _get(http, a.node_id, rid)
            if again["ok"]:
                break
            await asyncio.sleep(0.05)
        assert again["ok"] is True, again  # late real done replaces node_lost
        for ag in (back, other):
            await ag.close()


async def test_node_lost_by_silence_ends_the_request(
    runtime: ControllerRuntime, http: httpx.AsyncClient
) -> None:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as ch:
        a = FakeAgent(ch, node_id=new_node_id())
        await a.connect()  # no heartbeats: the lease (0.6 s) expires
        await _reconcile(a)
        rid = await _start_prepare(http, a.node_id)
        got: dict[str, Any] = {}
        for _ in range(100):
            got = await _get(http, a.node_id, rid)
            if got["done"]:
                break
            await asyncio.sleep(0.1)
        assert got["done"] and got["ok"] is False, got
        await a.close()


async def test_repeated_reconciles_in_one_session_do_not_pile_up_resends_unboundedly(
    runtime: ControllerRuntime, http: httpx.AsyncClient
) -> None:
    """A Reconcile is sent once per session by the real agent; a misbehaving one
    spamming it must not make the controller queue unbounded duplicates."""
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as ch:
        a = FakeAgent(ch, node_id=new_node_id())
        await a.connect()
        a.start_heartbeats(0.1)
        await _reconcile(a)
        rid = await _start_prepare(http, a.node_id)
        for _ in range(20):
            await _reconcile(a)
        await asyncio.sleep(1.0)
        sent = _prepares(a)
        assert set(sent) == {rid}
        assert len(sent) <= 3, f"{len(sent)} copies of one PrepareEnv in one session"
        await a.close()


# ----------------------------------------------------------------------- T2


def _tree(root: Path, tag: str) -> BuiltTree:
    root.mkdir(parents=True)
    (root / "f.txt").write_text(tag)
    return build_tree(root, chunk_size=CS)


async def test_thirty_concurrent_pushes_to_one_name_and_to_several_names(
    runtime: ControllerRuntime, http: httpx.AsyncClient, tmp_path: Path
) -> None:
    assert runtime.blobs is not None
    names = ["alpha", "beta", "gamma"]
    jobs: list[tuple[str, BuiltTree]] = []
    for n in range(36):
        built = _tree(tmp_path / f"t{n}", f"content {n}")
        runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
        for p in built.sources.values():
            runtime.blobs.cas.put_file(p)
        jobs.append(("alpha" if n < 24 else names[n % 3], built))

    async def push(name: str, built: BuiltTree) -> httpx.Response:
        return await http.post(
            "/v1/data", json={"name": name, "tree_id": str(built.tree.tree_id)}
        )

    started = time.monotonic()
    pushes = [asyncio.create_task(push(n, b)) for n, b in jobs]
    # the API must stay responsive while the pushes queue on their name locks
    probe_times: list[float] = []
    while not all(p.done() for p in pushes):
        t0 = time.monotonic()
        got = await http.get("/v1/data")
        assert got.status_code == 200
        probe_times.append(time.monotonic() - t0)
        await asyncio.sleep(0.02)
    results = [p.result() for p in pushes]
    assert [r.status_code for r in results] == [201] * 36, [
        (r.status_code, r.text[:80]) for r in results if r.status_code != 201
    ]
    for name in {n for n, _ in jobs}:
        listed = (await http.get(f"/v1/data/{name}")).json()["versions"]
        count = sum(1 for n, _ in jobs if n == name)
        assert sorted(v["version"] for v in listed) == list(range(1, count + 1)), name
    assert max(probe_times, default=0) < 5.0, max(probe_times)
    assert time.monotonic() - started < 60
