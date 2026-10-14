"""V0.7 G4 (D141, D148, D151 re-gate): REST users end to end - real PostgreSQL,
a real controller (HTTPS), a real agent running real jobs.

* every REST read but ``/healthz`` needs a user (D148);
* login -> token -> whoami / tokens; revoke, expiry, a disabled user and a
  password reset take effect on the very next request;
* login backoff: per user and per source IP (429 + Retry-After), a correct
  password is refused during a block, audit rows written;
* the legacy fabric token is refused (product default);
* one user's job is 404 to another on **every** job route - detail, list,
  cancel, logs, decisions, the WebSocket follow and the outputs download - and
  an Idempotency-Key cannot reach it; an admin sees everything;
* invites and node revoke are admin-only (403 for a user);
* neither a password nor a token reaches a log event.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import structlog
from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_jobs_harness import token_of
from mf_users import bearer, create_user, login
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from meshforge.common.ids import new_invite_id, new_job_id, new_node_id
from meshforge.controller.auth.passwords import PasswordHasher
from meshforge.controller.auth.users import UserStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"

PROGRAM = """\
import os, sys
os.makedirs("results", exist_ok=True)
open("results/r.txt", "w").write("secret result of alice\\n")
print("alice out", flush=True)
"""


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={
            **USERS_ONLY,
            "login_free_failures": 2,
            "login_backoff_base_s": 30.0,
            "login_rate_per_minute": 600.0,
            "login_rate_burst": 100,
        },
        jobs={"dispatch_interval_s": 0.1},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def users(engine: AsyncEngine, runtime: ControllerRuntime) -> dict[str, str]:
    """After ``runtime``: the controller migrates the database at start."""
    return {
        "admin": await create_user(engine, "admin", ADMIN_PW, admin=True),
        "alice": await create_user(engine, "alice", ALICE_PW),
        "bob": await create_user(engine, "bob", BOB_PW),
    }


def _base(runtime: ControllerRuntime) -> str:
    return f"https://127.0.0.1:{runtime.http_port}"


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=_base(runtime), timeout=30) as client:
        yield client


async def _as(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, name: str, password: str
) -> httpx.AsyncClient:
    token = await login(anon, name, password)
    return httpx.AsyncClient(base_url=_base(runtime), headers=bearer(token), timeout=30)


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


# ---------------------------------------------------------------- D148


@pytest.mark.security
async def test_every_read_needs_a_user_only_healthz_is_open(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    node = str(new_node_id())
    job = str(new_job_id())
    reads = [
        "/v1/nodes",
        f"/v1/nodes/{node}",
        f"/v1/nodes/{node}/resources",
        "/v1/sessions",
        "/v1/network",
        "/v1/jobs",
        f"/v1/jobs/{job}",
        f"/v1/jobs/{job}/logs",
        f"/v1/jobs/{job}/decisions",
        "/v1/data",
        "/v1/fabric/invites",
        "/v1/auth/whoami",
        "/v1/auth/tokens",
    ]
    for path in reads:
        response = await anon.get(path)
        assert response.status_code == 401, path
        assert response.json()["error"]["code"] == "unauthorized"
    assert (await anon.get("/healthz")).status_code == 200
    async with await _as(runtime, anon, "alice", ALICE_PW) as alice:
        for path in ("/v1/nodes", "/v1/sessions", "/v1/network", "/v1/jobs"):
            assert (await alice.get(path)).status_code == 200, path


@pytest.mark.security
async def test_legacy_fabric_token_is_refused_by_default(
    runtime: ControllerRuntime, anon: httpx.AsyncClient
) -> None:
    fabric = bearer(token_of(runtime))
    for method, path in (("GET", "/v1/jobs"), ("POST", "/v1/fabric/invites")):
        response = await anon.request(method, path, headers=fabric)
        assert response.status_code == 401, path


# ---------------------------------------------------------------- tokens


@pytest.mark.security
async def test_login_whoami_and_every_way_a_token_stops_working(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    me = (await anon.get("/v1/auth/whoami", headers=bearer(token))).json()
    assert me["user"] == {"id": users["alice"], "name": "alice", "role": "user"}
    assert me["legacy"] is False and me["token_expires_at"]
    listed = (await anon.get("/v1/auth/tokens", headers=bearer(token))).json()
    assert [t["current"] for t in listed["tokens"]] == [True]
    assert token not in json.dumps(listed)
    # Only admins list every user's tokens.
    assert (
        await anon.get("/v1/auth/tokens?all=true", headers=bearer(token))
    ).status_code == 403

    # 1. revoke (compute logout): the next request is refused.
    revoked = await anon.delete(
        f"/v1/auth/tokens/{me['token_id']}", headers=bearer(token)
    )
    assert revoked.status_code == 200 and revoked.json()["state"] == "revoked"
    assert (await anon.get("/v1/jobs", headers=bearer(token))).status_code == 401

    # 2. expiry (the database clock decides).
    token = await login(anon, "alice", ALICE_PW)
    assert (await anon.get("/v1/jobs", headers=bearer(token))).status_code == 200
    async with engine.begin() as db:
        await db.execute(
            text(
                "UPDATE api_token SET created_at = now() - interval '2 days', "
                "expires_at = now() - interval '1 second' WHERE user_id = :u "
                "AND revoked_at IS NULL"
            ),
            {"u": users["alice"]},
        )
    assert (await anon.get("/v1/jobs", headers=bearer(token))).status_code == 401

    # 3. a disabled user (controller CLI path, another process in production).
    token = await login(anon, "alice", ALICE_PW)
    store = UserStore(engine)
    _, revoked_count = await store.set_disabled("alice", disabled=True, actor="t")
    assert revoked_count == 1
    assert (await anon.get("/v1/jobs", headers=bearer(token))).status_code == 401
    refused = await anon.post(
        "/v1/auth/login", json={"name": "alice", "password": ALICE_PW}
    )
    assert refused.status_code == 401
    await store.set_disabled("alice", disabled=False, actor="t")

    # 4. a password reset revokes every token of the user.
    token = await login(anon, "alice", ALICE_PW)
    await store.reset_password(
        "alice",
        pw_hash=await PasswordHasher().hash(SecretStr("new-alice-pw-9")),
        actor="t",
    )
    assert (await anon.get("/v1/jobs", headers=bearer(token))).status_code == 401
    assert await login(anon, "alice", "new-alice-pw-9")

    # A user cannot revoke another user's token (404, like an unknown id).
    bob_token = await login(anon, "bob", BOB_PW)
    bob_me = (await anon.get("/v1/auth/whoami", headers=bearer(bob_token))).json()
    alice_token = await login(anon, "alice", "new-alice-pw-9")
    other = await anon.delete(
        f"/v1/auth/tokens/{bob_me['token_id']}", headers=bearer(alice_token)
    )
    assert other.status_code == 404
    assert (await anon.get("/v1/jobs", headers=bearer(bob_token))).status_code == 200


@pytest.mark.security
async def test_login_backoff_per_user_and_audit(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    wrong = {"name": "alice", "password": "not-the-password"}
    for _ in range(2):  # login_free_failures = 2
        response = await anon.post("/v1/auth/login", json=wrong)
        assert response.status_code == 401
    blocked = await anon.post("/v1/auth/login", json=wrong)
    assert blocked.status_code == 401  # the third failure starts the block
    during = await anon.post(
        "/v1/auth/login", json={"name": "alice", "password": ALICE_PW}
    )
    assert during.status_code == 429  # even the right password
    assert during.json()["error"]["reason"] == "rate_limited_user"
    assert int(during.headers["retry-after"]) >= 1
    # Another user is not affected; an unknown name gets the same 401 shape.
    assert await login(anon, "bob", BOB_PW)
    unknown = await anon.post(
        "/v1/auth/login", json={"name": "mallory", "password": "whatever-123"}
    )
    assert unknown.status_code == 401
    assert unknown.json()["error"]["message"] == "wrong user name or password"
    async with engine.connect() as db:
        rows = (
            await db.execute(
                text(
                    "SELECT action, details->>'reason' FROM audit_event "
                    "WHERE action IN ('login', 'login_failed') ORDER BY id"
                )
            )
        ).all()
    reasons = [r[1] for r in rows if r[0] == "login_failed"]
    assert reasons.count("bad_password") == 3 and "unknown_user" in reasons
    assert [r[0] for r in rows].count("login") == 1  # bob


@pytest.mark.security
async def test_login_backoff_per_source(
    pg_settings: DatabaseSettings, users: dict[str, str]
) -> None:
    settings = make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 1.0, "login_rate_burst": 3},
        **tolerant_liveness(),
    )
    async with (
        ControllerRuntime.start(settings) as runtime,
        httpx.AsyncClient(base_url=_base(runtime), timeout=30) as client,
    ):
        codes = []
        for name in ("alice", "bob", "nobody", "alice"):
            response = await client.post(
                "/v1/auth/login", json={"name": name, "password": "x" * 12}
            )
            codes.append(response.status_code)
        assert codes == [401, 401, 401, 429]
        assert response.json()["error"]["reason"] == "rate_limited_source"


# ---------------------------------------------------------------- ownership


async def _follow_close(runtime: ControllerRuntime, job_id: str, token: str) -> Any:
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"
    async with connect(url, additional_headers=bearer(token)) as ws:
        frames: list[dict[str, Any]] = []
        with contextlib.suppress(ConnectionClosed):
            while True:
                frames.append(json.loads(await ws.recv()))
        return ws.close_code, ws.close_reason, frames


@pytest.mark.security
async def test_another_users_job_is_404_on_every_job_route(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    (project / "main.py").write_text(PROGRAM)
    manifest = {
        "name": "mine",
        "entrypoint": ["python", "main.py"],
        "outputs": ["results/"],
    }
    alice_token = await login(anon, "alice", ALICE_PW)
    bob_token = await login(anon, "bob", BOB_PW)
    admin_token = await login(anon, "admin", ADMIN_PW)
    async with (
        httpx.AsyncClient(
            base_url=_base(runtime), headers=bearer(alice_token), timeout=30
        ) as alice,
        httpx.AsyncClient(
            base_url=_base(runtime), headers=bearer(bob_token), timeout=30
        ) as bob,
        httpx.AsyncClient(
            base_url=_base(runtime), headers=bearer(admin_token), timeout=30
        ) as admin,
    ):
        job_id = await submit(
            alice, project, agent.node_id, manifest, idempotency_key="alice-key-1"
        )
        job = await wait_job(alice, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        digest = job["attempts"][0]["output_digest"].removeprefix("sha256:")
        logs = await alice.get(f"/v1/jobs/{job_id}/logs")
        assert logs.status_code == 200 and b"alice out" in logs.content
        assert (await alice.get(f"/v1/blobs/{digest}")).status_code == 200

        # Bob: every job route says "no such job" - the same as a missing one.
        missing = (await bob.get(f"/v1/jobs/{new_job_id()}")).json()
        for method, path in (
            ("GET", f"/v1/jobs/{job_id}"),
            ("POST", f"/v1/jobs/{job_id}/cancel"),
            ("GET", f"/v1/jobs/{job_id}/logs"),
            ("GET", f"/v1/jobs/{job_id}/decisions"),
        ):
            response = await bob.request(method, path)
            assert response.status_code == 404, (method, path)
            assert response.json() == missing, (method, path)
        assert (await bob.get(f"/v1/blobs/{digest}")).status_code == 404
        listed = (await bob.get("/v1/jobs")).json()
        assert job_id not in {j["job_id"] for j in listed["jobs"]}
        _code, reason, frames = await _follow_close(runtime, job_id, bob_token)
        assert reason == "job_not_found" and frames == []
        # Same Idempotency-Key, same request: Bob does not get Alice's job.
        conflict = await bob.post(
            "/v1/jobs",
            json={
                "manifest": manifest,
                "bundle_digest": job["bundle_digest"],
                "placement": {"node_id": agent.node_id},
            },
            headers={"idempotency-key": "alice-key-1"},
        )
        assert conflict.status_code == 409
        assert job_id not in conflict.text

        # Alice and the admin see it everywhere.
        _code, _reason, frames = await _follow_close(runtime, job_id, alice_token)
        assert frames and frames[-1].get("end")
        for client in (alice, admin):
            assert (await client.get(f"/v1/jobs/{job_id}")).status_code == 200
            assert (await client.get(f"/v1/jobs/{job_id}/decisions")).status_code == 200
            assert (await client.get(f"/v1/blobs/{digest}")).status_code == 200
        assert job_id in {
            j["job_id"] for j in (await admin.get("/v1/jobs")).json()["jobs"]
        }


@pytest.mark.security
async def test_invites_and_node_revoke_are_admin_only(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    async with (
        await _as(runtime, anon, "bob", BOB_PW) as bob,
        await _as(runtime, anon, "admin", ADMIN_PW) as admin,
    ):
        node = str(new_node_id())
        for method, path in (
            ("POST", "/v1/fabric/invites"),
            ("GET", "/v1/fabric/invites"),
            ("POST", f"/v1/fabric/invites/{new_invite_id()}/revoke"),
            ("POST", f"/v1/nodes/{node}/revoke"),
        ):
            response = await bob.request(method, path)
            assert response.status_code == 403, (method, path)
        created = await admin.post("/v1/fabric/invites", json={})
        assert created.status_code == 201
        assert (await admin.post(f"/v1/nodes/{node}/revoke")).status_code == 404


@pytest.mark.security
async def test_no_password_or_token_in_any_log_event(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    with structlog.testing.capture_logs() as events:
        token = await login(anon, "alice", ALICE_PW)
        await anon.post(
            "/v1/auth/login", json={"name": "alice", "password": "wrong-" + ALICE_PW}
        )
        await anon.get("/v1/jobs", headers=bearer(token))
        await anon.get("/v1/jobs", headers=bearer(token[:-1] + "A"))
        await asyncio.sleep(0)
    dumped = repr(events)
    assert ALICE_PW not in dumped
    assert token not in dumped and token[5:] not in dumped
    assert any(e.get("event") == "controller.auth.login" for e in events)
