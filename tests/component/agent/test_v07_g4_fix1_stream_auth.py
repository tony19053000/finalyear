"""D157 T-1: an open log WebSocket loses its user when its token does.

Real PostgreSQL, a real controller (HTTPS/WSS) and a real agent running a job
that prints for ~20 s. Each test opens follows, then revokes / expires /
disables one credential and checks that exactly the affected follows are
closed ``4401 unauthorized`` within the re-check bound, while the others keep
receiving frames.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
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
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
RECHECK_S = 0.5
PROGRAM = """\
import time
for i in range(80):
    print("tick", i, flush=True)
    time.sleep(0.25)
"""


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "stream_recheck_interval_s": RECHECK_S},
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


def _base(runtime: ControllerRuntime) -> str:
    return f"https://127.0.0.1:{runtime.http_port}"


async def _running_job(
    runtime: ControllerRuntime, token: str, agent: ExecAgent, tmp_path: Path
) -> str:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(PROGRAM)
    manifest: dict[str, Any] = {"name": "ticker", "entrypoint": ["python", "main.py"]}
    async with httpx.AsyncClient(
        base_url=_base(runtime), headers=bearer(token), timeout=30
    ) as client:
        job_id = await submit(client, project, agent.node_id, manifest)
        await wait_job(client, job_id, states=frozenset({"running"}), timeout_s=60)
    return job_id


@contextlib.asynccontextmanager
async def _follow(
    runtime: ControllerRuntime, job_id: str, token: str
) -> AsyncIterator[ClientConnection]:
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"
    async with connect(url, additional_headers=bearer(token)) as ws:
        assert json.loads(await asyncio.wait_for(ws.recv(), 15))
        yield ws


async def _closed_within(
    ws: ClientConnection, limit_s: float
) -> tuple[int | None, float]:
    """Read until the server closes; (close code, seconds) - or (None, limit)."""
    t0 = time.monotonic()
    with contextlib.suppress(TimeoutError):
        while time.monotonic() - t0 < limit_s:
            try:
                await asyncio.wait_for(ws.recv(), limit_s)
            except ConnectionClosed as exc:
                return (exc.rcvd.code if exc.rcvd else None), time.monotonic() - t0
    return None, limit_s


async def _still_streaming(ws: ClientConnection) -> None:
    frame = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert "data" in frame or "stream" in frame, frame


async def test_a_token_revoked_over_rest_closes_its_follow_at_once_and_only_it(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    await create_user(engine, "admin", ADMIN_PW, admin=True)
    await create_user(engine, "alice", ALICE_PW)
    async with httpx.AsyncClient(base_url=_base(runtime), timeout=30) as anon:
        first = await login(anon, "alice", ALICE_PW)
        second = await login(anon, "alice", ALICE_PW)
        admin = await login(anon, "admin", ADMIN_PW)
    job_id = await _running_job(runtime, first, agent, tmp_path)
    async with (
        _follow(runtime, job_id, first) as doomed,
        _follow(runtime, job_id, second) as same_user_other_token,
        _follow(runtime, job_id, admin) as admins,
    ):
        # Only the notify path can be quick now: the periodic re-check is
        # parked in a 30 s wait (it reads interval_s before each wait).
        assert runtime.live_streams is not None
        runtime.live_streams.interval_s = 30.0
        await asyncio.sleep(RECHECK_S + 0.2)
        async with httpx.AsyncClient(
            base_url=_base(runtime), headers=bearer(first), timeout=30
        ) as alice:
            me = (await alice.get("/v1/auth/whoami")).json()
            t0 = time.monotonic()
            r = await alice.delete(f"/v1/auth/tokens/{me['token_id']}")
            assert r.status_code == 200, r.text
        code, _ = await _closed_within(doomed, 5.0)
        elapsed = time.monotonic() - t0
        assert code == 4401
        # The notify path: at once after the 200, not at the next tick.
        assert elapsed < 2.0, elapsed
        await _still_streaming(same_user_other_token)
        await _still_streaming(admins)
    # A new follow with the revoked token is refused at the handshake.
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"
    with pytest.raises(Exception, match="403"):
        async with connect(url, additional_headers=bearer(first)):
            pass
    assert runtime.live_streams is not None
    assert runtime.live_streams.stats.streams_cut == 1


async def test_an_expired_token_closes_its_follow_within_one_recheck(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    await create_user(engine, "alice", ALICE_PW)
    async with httpx.AsyncClient(base_url=_base(runtime), timeout=30) as anon:
        token = await login(anon, "alice", ALICE_PW)
        other = await login(anon, "alice", ALICE_PW)
    job_id = await _running_job(runtime, token, agent, tmp_path)
    async with httpx.AsyncClient(
        base_url=_base(runtime), headers=bearer(token), timeout=30
    ) as alice:
        token_id = (await alice.get("/v1/auth/whoami")).json()["token_id"]
    async with (
        _follow(runtime, job_id, token) as ws,
        _follow(runtime, job_id, other) as kept,
    ):
        async with engine.begin() as db:  # the token's life ends now (DB clock)
            await db.execute(
                text("UPDATE api_token SET expires_at = now() WHERE id = :id"),
                {"id": token_id},
            )
        code, elapsed = await _closed_within(ws, 5.0)
        assert code == 4401
        assert elapsed < RECHECK_S * 2 + 1.0, elapsed
        await _still_streaming(kept)


async def test_a_follow_that_ends_normally_leaves_no_registration(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    await create_user(engine, "alice", ALICE_PW)
    async with httpx.AsyncClient(base_url=_base(runtime), timeout=30) as anon:
        token = await login(anon, "alice", ALICE_PW)
    job_id = await _running_job(runtime, token, agent, tmp_path)
    streams = runtime.live_streams
    assert streams is not None
    async with _follow(runtime, job_id, token):
        assert len(streams) == 1
    deadline = time.monotonic() + 5
    while len(streams) and time.monotonic() < deadline:  # noqa: ASYNC110
        await asyncio.sleep(0.05)
    assert len(streams) == 0  # the client went away: unregistered


async def test_a_disabled_user_with_an_unrevoked_token_is_closed_too(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    """``user disable`` revokes the tokens in the same transaction; the
    re-check also refuses a disabled user on its own (like ``authenticate``),
    so a row flipped by hand cannot keep a stream alive."""
    await create_user(engine, "alice", ALICE_PW)
    await create_user(engine, "admin", ADMIN_PW, admin=True)
    async with httpx.AsyncClient(base_url=_base(runtime), timeout=30) as anon:
        token = await login(anon, "alice", ALICE_PW)
        admin = await login(anon, "admin", ADMIN_PW)
    job_id = await _running_job(runtime, token, agent, tmp_path)
    async with (
        _follow(runtime, job_id, token) as ws,
        _follow(runtime, job_id, admin) as kept,
    ):
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE app_user SET disabled = true WHERE name = 'alice'")
            )
        code, elapsed = await _closed_within(ws, 5.0)
        assert code == 4401
        assert elapsed < RECHECK_S * 2 + 1.0, elapsed
        await _still_streaming(kept)
