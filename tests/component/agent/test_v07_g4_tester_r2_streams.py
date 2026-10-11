"""V0.7 G4 tester round 2, T-1: try to keep a log WebSocket alive past its token.

Derived from D157 ("close within ~1 s of user disable / token revoke", fail
closed) and RE-S1, not from the implementation. Real PostgreSQL, a real
controller (WSS), a real agent running a long job. The database outage is a
real one: an ``ACCESS EXCLUSIVE`` lock on ``api_token`` held by another
connection, so the re-check query hangs until ``command_timeout_s``.
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

from meshforge.controller.auth.users import UserStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(300)]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"
RECHECK_S = 0.5
PROGRAM = """\
import time
for i in range(800):
    print("tick", i, flush=True)
    time.sleep(0.25)
"""


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings.model_copy(update={"command_timeout_s": 1.0}),
        auth={**USERS_ONLY, "stream_recheck_interval_s": RECHECK_S},
        jobs={"dispatch_interval_s": 0.1, "max_log_subscribers": 200},
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


async def _open(
    runtime: ControllerRuntime, job_id: str, token: str
) -> ClientConnection:
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"
    ws = await connect(url, additional_headers=bearer(token), max_queue=None)
    assert json.loads(await asyncio.wait_for(ws.recv(), 15))
    return ws


async def _close_code(ws: ClientConnection, limit_s: float) -> tuple[int | None, float]:
    """(close code, seconds) - or (None, limit_s) when still open after limit_s."""
    t0 = time.monotonic()
    while (left := limit_s - (time.monotonic() - t0)) > 0:
        try:
            await asyncio.wait_for(ws.recv(), left)
        except ConnectionClosed as exc:
            return (exc.rcvd.code if exc.rcvd else None), time.monotonic() - t0
        except TimeoutError:
            break
    return None, limit_s


async def _alive(ws: ClientConnection) -> bool:
    try:
        await asyncio.wait_for(ws.recv(), 5)
    except (ConnectionClosed, TimeoutError):
        return False
    return True


async def _users(
    runtime: ControllerRuntime, engine: AsyncEngine, names: tuple[str, ...]
) -> dict[str, list[str]]:
    """admin, alice, bob created; ``names``-many tokens each (real logins)."""
    await create_user(engine, "admin", ADMIN_PW, admin=True)
    await create_user(engine, "alice", ALICE_PW)
    await create_user(engine, "bob", BOB_PW)
    pws = {"admin": ADMIN_PW, "alice": ALICE_PW, "bob": BOB_PW}
    out: dict[str, list[str]] = {}
    async with httpx.AsyncClient(base_url=_base(runtime), timeout=30) as anon:
        for name in names:
            out.setdefault(name, []).append(await login(anon, name, pws[name]))
    return out


async def _token_id(runtime: ControllerRuntime, token: str) -> str:
    async with httpx.AsyncClient(
        base_url=_base(runtime), headers=bearer(token), timeout=30
    ) as c:
        return str((await c.get("/v1/auth/whoami")).json()["token_id"])


async def _registry_empties(runtime: ControllerRuntime, limit_s: float = 8.0) -> int:
    assert runtime.live_streams is not None
    t0 = time.monotonic()
    while len(runtime.live_streams) and time.monotonic() - t0 < limit_s:  # noqa: ASYNC110
        await asyncio.sleep(0.1)
    return len(runtime.live_streams)


async def test_many_concurrent_streams_only_the_revoked_tokens_are_cut_and_none_leak(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    toks = await _users(runtime, engine, ("alice",) * 3 + ("admin", "admin"))
    alice_tokens, bob, admin = toks["alice"], toks["admin"][1], toks["admin"][0]
    job_id = await _running_job(runtime, alice_tokens[2], agent, tmp_path)
    ids = [await _token_id(runtime, t) for t in alice_tokens]
    per = 12
    streams: dict[int, list[ClientConnection]] = {}
    for n, tok in enumerate(alice_tokens):
        streams[n] = list(
            await asyncio.gather(*[_open(runtime, job_id, tok) for _ in range(per)])
        )
    bob_ws = await _open(runtime, job_id, bob)
    admin_ws = await _open(runtime, job_id, admin)
    assert runtime.live_streams is not None
    assert len(runtime.live_streams) == 3 * per + 2
    # Revoke token 0 AND token 1 in the database, behind the controller's back
    # (the "another process" path) in one statement.
    async with engine.begin() as db:
        await db.execute(
            text("UPDATE api_token SET revoked_at = now() WHERE id = ANY(:ids)"),
            {"ids": ids[:2]},
        )
    results = await asyncio.gather(
        *[_close_code(ws, 6.0) for n in (0, 1) for ws in streams[n]]
    )
    assert {code for code, _ in results} == {4401}
    assert max(t for _, t in results) < 3.5  # D157: ~1 s; slack for a loaded box
    assert await _alive(streams[2][0])
    assert await _alive(bob_ws) and await _alive(admin_ws)
    assert runtime.live_streams.stats.streams_cut == 2 * per
    # Everything else goes away abruptly (no close handshake): no registry leak.
    for ws in [*streams[2], bob_ws, admin_ws]:
        ws.transport.abort()
    assert await _registry_empties(runtime) == 0


async def test_a_dead_token_reconnect_storm_is_refused_every_time_without_leaks(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    toks = await _users(runtime, engine, ("alice", "admin"))
    alice = toks["alice"][0]
    job_id = await _running_job(runtime, alice, agent, tmp_path)
    ws = await _open(runtime, job_id, alice)
    tid = await _token_id(runtime, alice)
    async with engine.begin() as db:
        await db.execute(
            text("UPDATE api_token SET revoked_at = now() WHERE id = :i"), {"i": tid}
        )
    assert (await _close_code(ws, 5.0))[0] == 4401
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"

    async def retry() -> str:
        try:
            async with connect(url, additional_headers=bearer(alice)):
                return "OPEN"
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    answers = await asyncio.gather(*[retry() for _ in range(40)])
    assert all("403" in a for a in answers), set(answers)
    assert await _registry_empties(runtime, 2.0) == 0


async def test_an_admin_revoking_a_users_token_over_rest_closes_that_stream_at_once(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    toks = await _users(runtime, engine, ("alice", "admin"))
    alice, admin = toks["alice"][0], toks["admin"][0]
    job_id = await _running_job(runtime, alice, agent, tmp_path)
    tid = await _token_id(runtime, alice)
    ws = await _open(runtime, job_id, alice)
    assert runtime.live_streams is not None
    runtime.live_streams.interval_s = 30.0  # only the notify path can be quick
    await asyncio.sleep(RECHECK_S + 0.3)
    async with httpx.AsyncClient(
        base_url=_base(runtime), headers=bearer(admin), timeout=30
    ) as c:
        t0 = time.monotonic()
        assert (await c.delete(f"/v1/auth/tokens/{tid}")).status_code == 200
    code, _ = await _close_code(ws, 5.0)
    assert code == 4401
    assert time.monotonic() - t0 < 2.0


async def test_user_disable_from_another_connection_closes_every_stream_of_that_user(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    """The CLI path: a separate UserStore on its own engine (as the
    ``meshforge-controller user disable`` process would), not the controller's."""
    toks = await _users(runtime, engine, ("alice", "alice", "admin", "admin"))
    job_id = await _running_job(runtime, toks["alice"][1], agent, tmp_path)
    a1 = await _open(runtime, job_id, toks["alice"][0])
    a2 = await _open(runtime, job_id, toks["alice"][1])
    b = await _open(runtime, job_id, toks["admin"][0])
    other = create_engine(runtime.settings.database)
    try:
        await UserStore(other).set_disabled("alice", disabled=True, actor="cli")
    finally:
        await other.dispose()
    codes = await asyncio.gather(_close_code(a1, 5.0), _close_code(a2, 5.0))
    assert [c for c, _ in codes] == [4401, 4401]
    assert max(t for _, t in codes) < 3.5
    assert await _alive(b)
    # Re-enabling does not resurrect the revoked tokens.
    other = create_engine(runtime.settings.database)
    try:
        await UserStore(other).set_disabled("alice", disabled=False, actor="cli")
    finally:
        await other.dispose()
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"
    with pytest.raises(Exception, match="403"):
        async with connect(url, additional_headers=bearer(toks["alice"][0])):
            pass
    b.transport.abort()


async def test_a_hung_database_closes_every_stream_4503_and_streams_recover_after(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    """Real outage: api_token is locked, the re-check query hangs to the 1 s
    command timeout. D157: 5 failed checks in a row -> every stream 4503."""
    toks = await _users(runtime, engine, ("alice", "admin", "admin"))
    job_id = await _running_job(runtime, toks["alice"][0], agent, tmp_path)
    ws_a = await _open(runtime, job_id, toks["alice"][0])
    ws_b = await _open(runtime, job_id, toks["admin"][0])
    ws_c = await _open(runtime, job_id, toks["admin"][1])
    assert runtime.live_streams is not None
    locker = await engine.connect()
    try:
        tx = await locker.begin()
        await locker.execute(text("LOCK TABLE api_token IN ACCESS EXCLUSIVE MODE"))
        t0 = time.monotonic()
        results = await asyncio.gather(
            *[_close_code(w, 40.0) for w in (ws_a, ws_b, ws_c)]
        )
        elapsed = time.monotonic() - t0
        assert [c for c, _ in results] == [4503, 4503, 4503], results
        # 5 failures x (1 s command timeout + 0.5 s tick) ~ 8 s; far under a minute.
        assert elapsed < 30, elapsed
        assert runtime.live_streams.stats.check_failures >= 5
        await tx.rollback()
    finally:
        await locker.close()
    # Outage over: a fresh, honest follow is accepted and stays open.
    again = await _open(runtime, job_id, toks["alice"][0])
    assert await _alive(again)
    await asyncio.sleep(RECHECK_S * 4)
    assert await _alive(again)
    again.transport.abort()


async def test_a_short_outage_does_not_kill_streams(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    """Fail closed must not be fail-flappy: 2 failed checks then recovery."""
    toks = await _users(runtime, engine, ("alice", "admin"))
    job_id = await _running_job(runtime, toks["alice"][0], agent, tmp_path)
    ws = await _open(runtime, job_id, toks["alice"][0])
    assert runtime.live_streams is not None
    real = runtime.users.live_token_ids  # type: ignore[union-attr]
    calls = {"n": 0}

    async def flaky(ids: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2 or 4 <= calls["n"] <= 5:  # never 5 in a row
            raise RuntimeError("db blip")
        return await real(ids)

    runtime.users.live_token_ids = flaky  # type: ignore[method-assign,union-attr]
    await asyncio.sleep(RECHECK_S * 9)
    assert calls["n"] >= 6
    assert await _alive(ws)
    ws.transport.abort()


async def test_a_stream_dies_if_its_token_is_revoked_during_an_outage_window(
    runtime: ControllerRuntime, engine: AsyncEngine, agent: ExecAgent, tmp_path: Path
) -> None:
    """Revoke while 2 checks fail: must end (4401 once the DB answers again,
    or 4503) - never stay open."""
    toks = await _users(runtime, engine, ("alice", "admin"))
    job_id = await _running_job(runtime, toks["alice"][0], agent, tmp_path)
    ws = await _open(runtime, job_id, toks["alice"][0])
    tid = await _token_id(runtime, toks["alice"][0])
    real = runtime.users.live_token_ids  # type: ignore[union-attr]
    calls = {"n": 0}

    async def flaky(ids: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("db blip")
        return await real(ids)

    runtime.users.live_token_ids = flaky  # type: ignore[method-assign,union-attr]
    async with engine.begin() as db:
        await db.execute(
            text("UPDATE api_token SET revoked_at = now() WHERE id = :i"), {"i": tid}
        )
    code, _ = await _close_code(ws, 8.0)
    assert code in (4401, 4503), code


async def test_controller_restart_a_token_revoked_while_it_was_down_stays_refused(
    pg_settings: DatabaseSettings, engine: AsyncEngine, tmp_path: Path
) -> None:
    from meshforge.controller.server import ControllerRuntime as Runtime

    settings = make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "stream_recheck_interval_s": RECHECK_S},
        **tolerant_liveness(),
    )
    async with Runtime.start(settings) as first:
        await create_user(engine, "alice", ALICE_PW)
        await create_user(engine, "bob", BOB_PW)
        async with httpx.AsyncClient(base_url=_base(first), timeout=30) as anon:
            alice = await login(anon, "alice", ALICE_PW)
            bob = await login(anon, "bob", BOB_PW)
        tid = await _token_id(first, alice)
    async with engine.begin() as db:  # while the controller is down
        await db.execute(
            text("UPDATE api_token SET revoked_at = now() WHERE id = :i"), {"i": tid}
        )
    async with Runtime.start(settings) as second:
        url = f"wss://127.0.0.1:{second.http_port}/v1/jobs/job_x/logs/ws"
        with pytest.raises(Exception, match="403"):
            async with connect(url, additional_headers=bearer(alice)):
                pass
        # The live token is accepted at the handshake (then 4400/4404 for a bad job).
        async with connect(url, additional_headers=bearer(bob)) as ws:
            with contextlib.suppress(ConnectionClosed):
                await asyncio.wait_for(ws.recv(), 5)
        assert second.live_streams is not None
        assert await _registry_empties(second, 3.0) == 0
