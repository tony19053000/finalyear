"""V0.8 G1 deliverable 3 (D172, D180): ``/v1/events/ws`` end to end - real
PostgreSQL, a real HTTPS controller, real users, a real agent.

* job state changes arrive as events after commit, p95 < 500 ms on loopback
  (the exit-list latency budget; the dashboard's criterion 2 is <= 2 s) -
  ``perf`` since D422: on a shared CI runner it measures the runner's load, so
  it runs on a quiet lab laptop (``docs/LAB.md`` "Load-sensitive timing
  tests"); delivery and ``seq`` order are still gated by the other tests
  here;
* a user receives only their own jobs' events; an admin every job's;
* resume with ``since``/``epoch`` delivers exactly the missed events; a
  foreign epoch is ``resync``;
* a revoked token closes the stream ``4401`` (D157 re-check);
* an agent connecting is a ``node.status`` event, its heartbeats ``node.metrics``;
* the dashboard cookie works with our own Origin.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from meshforge.common.ids import new_job_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"
DIGEST = "sha256:" + "ab" * 32


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={
            **USERS_ONLY,
            "login_rate_per_minute": 600.0,
            "login_rate_burst": 100,
            "stream_recheck_interval_s": 0.2,
        },
        jobs={"dispatch_interval_s": 60.0},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def users(engine: AsyncEngine, runtime: ControllerRuntime) -> dict[str, str]:
    return {
        "admin": await create_user(engine, "admin", ADMIN_PW, admin=True),
        "alice": await create_user(engine, "alice", ALICE_PW),
        "bob": await create_user(engine, "bob", BOB_PW),
    }


def _origin(runtime: ControllerRuntime) -> str:
    return f"https://127.0.0.1:{runtime.http_port}"


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=_origin(runtime), timeout=30) as client:
        yield client


def _url(runtime: ControllerRuntime, query: str = "") -> str:
    return f"wss://127.0.0.1:{runtime.http_port}/v1/events/ws{query}"


async def _recv(ws: ClientConnection, within: float = 10.0) -> dict[str, Any]:
    raw = await asyncio.wait_for(ws.recv(), within)
    frame = json.loads(raw)
    assert isinstance(frame, dict)
    return frame


async def _until(
    ws: ClientConnection, match: Any, within: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + within
    while True:
        frame = await _recv(ws, max(0.05, deadline - time.monotonic()))
        if match(frame):
            return frame


async def _job(runtime: ControllerRuntime, owner: str) -> JobRecord:
    record, _ = await runtime.jobs.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": "n"},
        request_hash="h" * 64,
        idempotency_key=None,
        owner_user_id=owner,
    )
    return record


def _queued(job_id: str) -> Any:
    return lambda f: (
        f.get("type") == "job.state"
        and f["data"]["job_id"] == job_id
        and f["data"]["state"] == "queued"
    )


@pytest.mark.perf  # D422: a p95 latency budget - not a gate on shared CI runners
async def test_job_events_arrive_after_commit_p95_under_500_ms(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    async with connect(_url(runtime), additional_headers=bearer(token)) as ws:
        hello = await _recv(ws)
        assert hello["type"] == "hello" and hello["resumed"] is False
        assert isinstance(hello["epoch"], str) and hello["seq"] >= 0
        latencies: list[float] = []
        last_seq = hello["seq"]
        for _ in range(30):
            started = time.perf_counter()
            job = await _job(runtime, users["alice"])
            frame = await _until(ws, _queued(job.id))
            latencies.append(time.perf_counter() - started)
            assert frame["seq"] > last_seq
            last_seq = frame["seq"]
    p95 = statistics.quantiles(latencies, n=20)[18]
    assert p95 < 0.5, (p95, max(latencies))


@pytest.mark.security
async def test_users_see_only_their_own_jobs_and_admins_see_all(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    alice = await login(anon, "alice", ALICE_PW)
    bob = await login(anon, "bob", BOB_PW)
    admin = await login(anon, "admin", ADMIN_PW)
    async with (
        connect(_url(runtime), additional_headers=bearer(alice)) as a,
        connect(_url(runtime), additional_headers=bearer(bob)) as b,
        connect(_url(runtime), additional_headers=bearer(admin)) as root,
    ):
        for ws in (a, b, root):
            assert (await _recv(ws))["type"] == "hello"
        alices = await _job(runtime, users["alice"])
        await _until(a, _queued(alices.id))
        await _until(root, _queued(alices.id))
        bobs = await _job(runtime, users["bob"])
        await _until(b, _queued(bobs.id))
        # Nothing of alice's ever reached bob (he saw his own job's frames only).
        await _until(root, _queued(bobs.id))
        try:
            while True:
                frame = await _recv(b, within=0.5)
                assert frame["data"].get("job_id") != alices.id
        except TimeoutError:
            pass


async def test_resume_gets_the_missed_events_and_a_foreign_epoch_resyncs(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    async with connect(_url(runtime), additional_headers=bearer(token)) as ws:
        hello = await _recv(ws)
    epoch, seen = hello["epoch"], hello["seq"]
    missed = [await _job(runtime, users["alice"]) for _ in range(3)]
    query = f"?since={seen}&epoch={epoch}"
    async with connect(_url(runtime, query), additional_headers=bearer(token)) as ws:
        hello2 = await _recv(ws)
        assert hello2["type"] == "hello" and hello2["resumed"] is True
        frames = [await _recv(ws) for _ in range(9)]  # 3 jobs x 3 arrows
    seqs = [f["seq"] for f in frames]
    assert seqs == list(range(seen + 1, seen + 10))  # no gap, no duplicate
    queued = [f["data"]["job_id"] for f in frames if f["data"]["state"] == "queued"]
    assert queued == [j.id for j in missed]
    async with connect(
        _url(runtime, f"?since={seen}&epoch=deadbeef"), additional_headers=bearer(token)
    ) as ws:
        frame = await _recv(ws)
    assert frame["type"] == "resync" and frame["reason"] == "epoch"


@pytest.mark.security
async def test_a_revoked_token_closes_the_stream_4401(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    token_id = (await anon.get("/v1/auth/whoami", headers=bearer(token))).json()[
        "token_id"
    ]
    async with connect(_url(runtime), additional_headers=bearer(token)) as ws:
        assert (await _recv(ws))["type"] == "hello"
        revoked = await anon.delete(
            f"/v1/auth/tokens/{token_id}", headers=bearer(token)
        )
        assert revoked.status_code == 200
        started = time.monotonic()

        async def drain() -> None:
            while True:
                await _recv(ws, within=5)

        with pytest.raises(ConnectionClosed) as closed:
            await drain()
        assert time.monotonic() - started < 2.0
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401


@pytest.mark.security
async def test_no_credential_or_a_foreign_origin_cookie_is_refused(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    from websockets.exceptions import InvalidStatus

    with pytest.raises(InvalidStatus) as refused:
        async with connect(_url(runtime)):
            pass
    assert refused.value.response.status_code == 403
    session = await anon.post(
        "/v1/session",
        json={"name": "alice", "password": ALICE_PW},
        headers={"origin": _origin(runtime)},
    )
    cookie = f"mf_session={session.cookies['mf_session']}"
    with pytest.raises(InvalidStatus):
        async with connect(
            _url(runtime),
            additional_headers={"cookie": cookie, "origin": "https://evil.example"},
        ):
            pass
    async with connect(
        _url(runtime),
        additional_headers={"cookie": cookie, "origin": _origin(runtime)},
    ) as ws:
        assert (await _recv(ws))["type"] == "hello"


async def test_an_agent_connecting_is_a_node_status_then_metrics_event(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    grpc_target: str,
    tmp_path: Path,
) -> None:
    token = await login(anon, "bob", BOB_PW)  # node events: every user
    async with connect(_url(runtime), additional_headers=bearer(token)) as ws:
        assert (await _recv(ws))["type"] == "hello"
        agent = await ExecAgent.start(grpc_target, tmp_path / "agent")
        try:
            status = await _until(
                ws,
                lambda f: (
                    f.get("type") == "node.status" and f["data"]["connected"] is True
                ),
                within=20,
            )
            node_id = status["data"]["node_id"]
            assert status["data"]["state"] == "online"
            # The harness agent runs no resource monitor: one report is fed
            # into the controller's tracker the way its session reader does.
            assert runtime.resources is not None and runtime.manager is not None
            live = runtime.manager.get(node_id)
            assert live is not None
            update = agent_pb2.ResourceUpdate(
                sequence=1,
                cpu_percent=37.5,
                ram_used_bytes=1 << 30,
                ram_total_bytes=8 << 30,
            )
            update.observed_at.GetCurrentTime()
            assert runtime.resources.record(
                node_id,
                live.session_epoch,
                update,
                asyncio.get_running_loop().time(),
            )
            metrics = await _until(
                ws,
                lambda f: (
                    f.get("type") == "node.metrics" and f["data"]["node_id"] == node_id
                ),
                within=20,
            )
            assert metrics["data"]["cpu_percent"] == 37.5
            assert metrics["data"]["ram_total_bytes"] == 8 << 30
            got = await anon.get(f"/v1/nodes/{node_id}/metrics", headers=bearer(token))
            assert got.status_code == 200 and got.json()["count"] >= 1
        finally:
            await agent.stop()
        gone = await _until(
            ws,
            lambda f: (
                f.get("type") == "node.status"
                and f["data"]["node_id"] == node_id
                and f["data"]["connected"] is False
            ),
            within=20,
        )
        assert gone["data"]["state"] in ("offline", None)
