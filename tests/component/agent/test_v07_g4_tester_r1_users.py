"""V0.7 G4 tester round 1: users, API tokens, D148 and cross-user isolation,
derived from D141 / D148 / brief 5d items 3 and 6 and THREAT_MODEL RE-S*/RE-E*/CB-E3.

Real PostgreSQL, a real controller (HTTPS), a real agent where a job is needed.
"""

from __future__ import annotations

import asyncio
import contextlib
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

import hashlib

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_exec_harness import ExecAgent, submit, wait_job
from mf_users import bearer, create_user, login
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from meshforge.common.ids import new_job_id, new_node_id
from meshforge.controller.auth.users import UserStore
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(240)]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"

LONG_PROGRAM = """\
import time
for i in range(40):
    print("tick", i, flush=True)
    time.sleep(0.5)
"""
QUICK_PROGRAM = """\
import os
os.makedirs("results", exist_ok=True)
open("results/r.txt", "w").write("alice result\\n")
print("alice out", flush=True)
"""


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={
            **USERS_ONLY,
            "login_free_failures": 3,
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


@pytest_asyncio.fixture
async def agent(grpc_target: str, data_dir: Path) -> AsyncIterator[ExecAgent]:
    running = await ExecAgent.start(grpc_target, data_dir)
    yield running
    await running.stop()


def _client(runtime: ControllerRuntime, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=_base(runtime), headers=bearer(token), timeout=30)


def _project(tmp_path: Path, program: str) -> tuple[Path, dict[str, Any]]:
    project = tmp_path / "proj"
    project.mkdir(exist_ok=True)
    (project / "main.py").write_text(program)
    return project, {
        "name": "mine",
        "entrypoint": ["python", "main.py"],
        "outputs": ["results/"],
    }


# --------------------------------------------------------------- login input


_NUL_XFAIL = pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT T-3 (minor): a NUL character in the login name reaches PostgreSQL "
        "and surfaces as 503 'the user database is unavailable' to an "
        "unauthenticated caller (client input must be a 4xx / the same 401)"
    ),
)


@pytest.mark.parametrize(
    "body",
    [
        pytest.param({"name": "alice\x00", "password": ALICE_PW}, marks=_NUL_XFAIL),
        {"name": "alice", "password": "pw\x00pw-pw-pw"},
        pytest.param({"name": "\x00", "password": "x"}, marks=_NUL_XFAIL),
        {"name": "alice", "password": "pw-\ud800-lone-surrogate"},
        {"name": "al\ud800ice", "password": "whatever-123"},
        {"name": "alice", "password": "é" * 5000},
        {
            "name": "\uff41\uff4c\uff49\uff43\uff45",
            "password": ALICE_PW,
        },  # full-width look-alike
        {"name": "\u0430lice", "password": ALICE_PW},  # Cyrillic a
        {"name": "alice", "password": ["list"]},
        {"name": None, "password": "x"},
        {"name": "alice", "password": ALICE_PW, "role": "admin"},
    ],
    ids=[
        "nul-name",
        "nul-password",
        "only-nul",
        "surrogate-password",
        "surrogate-name",
        "huge-password",
        "fullwidth-name",
        "cyrillic-name",
        "password-list",
        "null-name",
        "extra-role-field",
    ],
)
async def test_hostile_login_bodies_get_a_clean_4xx_and_never_a_token(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    body: dict[str, Any],
) -> None:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8", "surrogatepass")
    # A lone surrogate cannot be UTF-8; send the JSON escape instead.
    if any(isinstance(v, str) and "\ud800" in v for v in body.values()):
        payload = json.dumps(body, ensure_ascii=True).encode()
    response = await anon.post(
        "/v1/auth/login", content=payload, headers={"content-type": "application/json"}
    )
    assert 400 <= response.status_code < 500, (response.status_code, response.text)
    assert '"token"' not in response.text and "mfat_" not in response.text
    assert ALICE_PW not in response.text
    # The controller is still healthy and the real user can still log in.
    assert (await anon.get("/healthz")).status_code == 200
    assert await login(anon, "alice", ALICE_PW)


async def test_malformed_and_non_json_login_bodies_are_clean_4xx(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    for content in (b"", b"{", b"null", b"[]", b"\xff\xfe\x00", b"x" * 10_000):
        response = await anon.post(
            "/v1/auth/login",
            content=content,
            headers={"content-type": "application/json"},
        )
        assert 400 <= response.status_code < 500, (content[:10], response.status_code)
    # Not a form post either.
    response = await anon.post(
        "/v1/auth/login", data={"name": "alice", "password": ALICE_PW}
    )
    assert 400 <= response.status_code < 500


# --------------------------------------------------------------- enumeration


async def test_login_failures_are_indistinguishable_in_status_body_and_headers(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    store = UserStore(engine)
    await store.set_disabled("bob", disabled=True, actor="t")
    shapes = []
    for name, pw in (
        ("alice", "wrong-password-9"),  # real user, wrong password
        ("nobody", "wrong-password-9"),  # unknown user
        ("bob", BOB_PW),  # disabled user, RIGHT password
    ):
        response = await anon.post(
            "/v1/auth/login", json={"name": name, "password": pw}
        )
        headers = {
            k: v
            for k, v in response.headers.items()
            if k not in {"date", "content-length", "x-request-id"}
        }
        shapes.append((response.status_code, response.json(), headers))
    assert shapes[0] == shapes[1] == shapes[2], shapes


async def test_login_timing_does_not_reveal_which_names_exist(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    """Each branch burns one scrypt verification: medians stay within 2.5x."""

    async def sample(name: str, n: int = 7) -> float:
        times = []
        for i in range(n):
            t0 = time.perf_counter()
            # Distinct unknown names so no per-name backoff block ever applies
            # (3 free failures); the real names are rotated across users.
            who = name if name != "unknown" else f"nobody{i}-{time.monotonic_ns()}"
            await anon.post(
                "/v1/auth/login", json={"name": who, "password": "wrong-password-9"}
            )
            times.append(time.perf_counter() - t0)
        return statistics.median(times)

    known = await sample("admin", 3)  # stays within the 3 free failures
    unknown = await sample("unknown", 7)
    known_b = await sample("bob", 3)
    ratio = max(known, known_b) / unknown
    assert 0.4 < ratio < 2.5, (known, known_b, unknown)
    assert unknown > 0.005, "no password work was done for an unknown user"


async def test_name_variants_share_one_failure_counter(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    """3 free failures; spelling variants of one name must not each get a
    fresh allowance (that would multiply a guesser's budget)."""
    variants = ["alice", "ALICE", " alice ", "Alice", "aLiCe", "alice"]
    codes = []
    for name in variants:
        r = await anon.post(
            "/v1/auth/login", json={"name": name, "password": "wrong-password-9"}
        )
        codes.append(r.status_code)
    right = await anon.post(
        "/v1/auth/login", json={"name": "alice", "password": ALICE_PW}
    )
    assert 429 in codes or right.status_code == 429, (codes, right.status_code)


async def test_a_correct_login_clears_the_counter_and_other_users_are_unaffected(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    for _ in range(2):
        await anon.post(
            "/v1/auth/login", json={"name": "alice", "password": "wrong-password-9"}
        )
    assert await login(anon, "alice", ALICE_PW)
    for _ in range(2):  # would be the 3rd/4th failure had success not cleared it
        r = await anon.post(
            "/v1/auth/login", json={"name": "alice", "password": "wrong-password-9"}
        )
        assert r.status_code == 401


async def test_a_login_flood_does_not_starve_authenticated_requests(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    async with httpx.AsyncClient(base_url=_base(runtime), timeout=60) as flood:

        async def hit(i: int) -> int:
            r = await flood.post(
                "/v1/auth/login",
                json={"name": f"ghost{i}", "password": "wrong-password-9"},
            )
            return r.status_code

        storm = asyncio.gather(*[hit(i) for i in range(60)])
        await asyncio.sleep(0.2)
        t0 = time.perf_counter()
        r = await anon.get("/v1/jobs", headers=bearer(token))
        health = await anon.get("/healthz")
        took = time.perf_counter() - t0
        codes = await storm
    assert r.status_code == 200 and health.status_code == 200
    assert took < 5.0, f"authenticated reads stalled {took:.1f}s behind a login flood"
    assert set(codes) <= {401, 429}


# --------------------------------------------------------------- token forms


async def test_token_presentation_variants_never_authenticate(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    me = (await anon.get("/v1/auth/whoami", headers=bearer(token))).json()
    async with engine.connect() as db:
        stored = (
            await db.execute(
                text("SELECT token_hash FROM api_token WHERE id = :i"),
                {"i": me["token_id"]},
            )
        ).scalar_one()
    stored_hex = (
        stored.hex() if isinstance(stored, (bytes, memoryview)) else str(stored)
    )
    bad_bearers = [
        token[:-1],
        token + "A",
        token[:-1] + ("A" if token[-1] != "A" else "B"),
        token.upper(),
        "mfat_" + "A" * 43,
        stored_hex,
        "mfat_" + stored_hex[:43],
        hashlib.sha256(token.encode()).hexdigest(),
        "Bearer " + token,
        token.replace("mfat_", ""),
        "null",
    ]
    for bad in bad_bearers:
        r = await anon.get("/v1/jobs", headers={"authorization": f"Bearer {bad}"})
        assert r.status_code == 401, bad
    # Other places a token must not be accepted from.
    for kwargs in (
        {"params": {"token": token}},
        {"params": {"access_token": token}},
        {"params": {"api_key": token}},
        {"headers": {"cookie": f"token={token}; session={token}"}},
        {"headers": {"x-api-key": token}},
        {"headers": {"x-auth-token": token}},
        {"headers": {"authorization": f"Basic {token}"}},
        {"headers": {"authorization": f"Token {token}"}},
        {"headers": {"authorization": token}},
        {"headers": {"authorization": f"Bearer {token} extra"}},
    ):
        r = await anon.get("/v1/jobs", **kwargs)
        assert r.status_code == 401, kwargs
    # A repeated Authorization header is ambiguous: refused.
    r = await anon.get(
        "/v1/jobs",
        headers=[
            ("authorization", f"Bearer {token}"),
            ("authorization", f"Bearer {token}"),
        ],
    )
    assert r.status_code == 401
    # Oversized header: refused without error.
    r = await anon.get("/v1/jobs", headers={"authorization": "Bearer " + "A" * 100_000})
    assert r.status_code in (400, 401, 431)
    # The real thing still works, with scheme case-insensitivity per RFC 7235.
    assert (await anon.get("/v1/jobs", headers=bearer(token))).status_code == 200
    # The 401 never echoes what was sent.
    r = await anon.get("/v1/jobs", headers={"authorization": "Bearer mfat_secretprobe"})
    assert "secretprobe" not in r.text and r.status_code == 401


async def test_a_token_in_the_url_of_the_log_websocket_is_not_accepted(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{new_job_id()}/logs/ws"
    for suffix in (f"?token={token}", f"?access_token={token}"):
        with pytest.raises(Exception) as raised:
            async with connect(url + suffix) as ws:
                await ws.recv()
        assert not isinstance(raised.value, AssertionError)


async def test_open_routes_are_exactly_healthz_login_blobs_and_docs(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    """D148 from the live OpenAPI document: every (method, path) not on the
    allow-list answers 401 to an anonymous caller."""
    spec = (await anon.get("/v1/openapi.json")).json()
    allowed = {("get", "/healthz"), ("post", "/v1/auth/login")}
    node, job = str(new_node_id()), str(new_job_id())
    digest = "0" * 64
    checked = 0
    for path, methods in spec["paths"].items():
        concrete = (
            path.replace("{node_id}", node)
            .replace("{job_id}", job)
            .replace("{sha256}", digest)
            .replace("{token_id}", "tok_01J0000000000000000000000A")
            .replace("{invite_id}", "inv_01J0000000000000000000000A")
            .replace("{name}", "x")
            .replace("{version}", "1")
            .replace("{index}", "0")
        )
        for method in methods:
            if method.lower() not in {"get", "post", "put", "patch", "delete", "head"}:
                continue
            if (method.lower(), path) in allowed or path.startswith("/v1/blobs/"):
                continue
            r = await anon.request(method.upper(), concrete)
            assert r.status_code == 401, (method, path, r.status_code)
            checked += 1
    assert checked >= 20, checked


# --------------------------------------------------------------- lifecycle


async def test_a_token_of_a_deleted_user_dies_and_orphaned_jobs_are_admin_only(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    project, manifest = _project(tmp_path, QUICK_PROGRAM)
    alice_token = await login(anon, "alice", ALICE_PW)
    bob_token = await login(anon, "bob", BOB_PW)
    admin_token = await login(anon, "admin", ADMIN_PW)
    async with (
        _client(runtime, alice_token) as alice,
        _client(runtime, bob_token) as bob,
        _client(runtime, admin_token) as admin,
    ):
        job_id = await submit(alice, project, agent.node_id, manifest)
        await wait_job(alice, job_id, timeout_s=60)
        async with engine.begin() as db:
            await db.execute(text("DELETE FROM app_user WHERE name = 'alice'"))
        assert (await alice.get("/v1/jobs")).status_code == 401
        assert (await alice.get("/v1/auth/whoami")).status_code == 401
        # The job survives, owner NULL: admin only, a normal user gets 404.
        assert (await bob.get(f"/v1/jobs/{job_id}")).status_code == 404
        assert (await admin.get(f"/v1/jobs/{job_id}")).status_code == 200
        assert job_id not in {
            j["job_id"] for j in (await bob.get("/v1/jobs")).json()["jobs"]
        }
        # A NEW user with the dead user's name does not inherit anything.
        await create_user(engine, "alice", "another-pw-12345")
        fresh = await login(anon, "alice", "another-pw-12345")
        async with _client(runtime, fresh) as again:
            assert (await again.get(f"/v1/jobs/{job_id}")).status_code == 404
            assert (await again.get("/v1/jobs")).json()["jobs"] == []
        assert (await alice.get("/v1/jobs")).status_code == 401


async def test_role_changes_and_disable_act_on_the_next_request(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    admin_token = await login(anon, "admin", ADMIN_PW)
    async with _client(runtime, admin_token) as admin:
        assert (await admin.get("/v1/fabric/invites")).status_code == 200
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE app_user SET role = 'user' WHERE name='admin'")
            )
        assert (await admin.get("/v1/fabric/invites")).status_code == 403, (
            "a demoted admin kept admin rights on an existing token"
        )
        assert (await admin.get("/v1/auth/tokens?all=true")).status_code == 403
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE app_user SET role = 'admin' WHERE name='admin'")
            )
        assert (await admin.get("/v1/fabric/invites")).status_code == 200
        async with engine.begin() as db:
            await db.execute(
                text("UPDATE app_user SET disabled = true WHERE name='admin'")
            )
        assert (await admin.get("/v1/fabric/invites")).status_code == 401


async def test_a_normal_user_cannot_escalate_through_any_route_or_field(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    bob_token = await login(anon, "bob", BOB_PW)
    admin_token = await login(anon, "admin", ADMIN_PW)
    async with (
        _client(runtime, bob_token) as bob,
        _client(runtime, admin_token) as admin,
    ):
        # Tokens: only your own; `all` is admin; another user's token id is 404.
        admin_me = (await admin.get("/v1/auth/whoami")).json()
        assert (await bob.get("/v1/auth/tokens?all=true")).status_code == 403
        assert (await bob.get("/v1/auth/tokens?all=1")).status_code == 403
        assert (await bob.get("/v1/auth/tokens?all=TRUE")).status_code == 403
        r = await bob.delete(f"/v1/auth/tokens/{admin_me['token_id']}")
        assert r.status_code == 404
        assert (await admin.get("/v1/auth/whoami")).status_code == 200
        # The login/token routes take no role or user field.
        r = await anon.post(
            "/v1/auth/login",
            json={"name": "bob", "password": BOB_PW, "role": "admin", "user": "admin"},
        )
        assert r.status_code == 422 or r.status_code == 400
        # Admin-only calls, with and without bodies.
        for method, path, body in (
            ("POST", "/v1/fabric/invites", {}),
            ("POST", "/v1/fabric/invites", {"label": "x", "ttl_s": 999999}),
            ("GET", "/v1/fabric/invites", None),
            ("POST", f"/v1/nodes/{agent.node_id}/revoke", None),
            ("POST", f"/v1/nodes/{new_node_id()}/revoke", None),
        ):
            r = await bob.request(method, path, json=body)
            assert r.status_code == 403, (method, path, r.status_code)
        # The agent is still not revoked after all that.
        nodes = (await admin.get(f"/v1/nodes/{agent.node_id}")).json()
        assert nodes.get("state") not in {"revoked"}
        # Mass assignment on submit: an owner field is refused, not honoured.
        project, manifest = _project(tmp_path, QUICK_PROGRAM)
        with pytest.raises(AssertionError):
            await submit(
                bob,
                project,
                agent.node_id,
                manifest,
                body={"owner_user_id": users["admin"]},
            )
        # An honest submit is owned by bob, not by whoever the body claims.
        job_id = await submit(bob, project, agent.node_id, manifest)
        await wait_job(bob, job_id, timeout_s=60)
        listed = (await admin.get("/v1/jobs")).json()["jobs"]
        mine = [j for j in listed if j["job_id"] == job_id]
        assert mine and json.dumps(mine[0]).count(users["admin"]) == 0


# ------------------------------------------------------------- isolation


async def test_bob_learns_nothing_of_alices_running_job_from_the_other_reads(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    """RE-E1 judged by what Bob can still see: node, session, resource and
    network views must not carry Alice's job or attempt ids or her manifest."""
    project, manifest = _project(tmp_path, LONG_PROGRAM)
    manifest["name"] = "alice-secret-project-name"
    alice_token = await login(anon, "alice", ALICE_PW)
    bob_token = await login(anon, "bob", BOB_PW)
    async with (
        _client(runtime, alice_token) as alice,
        _client(runtime, bob_token) as bob,
    ):
        job_id = await submit(alice, project, agent.node_id, manifest)
        job = await wait_job(alice, job_id, states=frozenset({"running"}), timeout_s=60)
        attempt_id = job["attempts"][0]["attempt_id"]
        await asyncio.sleep(1.0)  # let a resource update carry the attempt
        seen: dict[str, str] = {}
        for path in (
            "/v1/nodes",
            f"/v1/nodes/{agent.node_id}",
            f"/v1/nodes/{agent.node_id}/resources",
            "/v1/sessions",
            "/v1/network",
            "/v1/data",
            "/v1/jobs",
        ):
            r = await bob.get(path)
            assert r.status_code == 200, (path, r.status_code)
            seen[path] = r.text
        leaks = {
            path: [
                needle
                for needle in (job_id, attempt_id, "alice-secret-project-name", "tick")
                if needle in body
            ]
            for path, body in seen.items()
        }
        leaks = {k: v for k, v in leaks.items() if v}
        assert not leaks, f"another user's job is visible to Bob: {leaks}"
        await alice.post(f"/v1/jobs/{job_id}/cancel")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT T-1 (minor): an already-open log WebSocket is authenticated at "
        "the handshake only; disabling the user or revoking the token does not "
        "close it, so RE-S1 'a revoke/expiry/disable acts on the next request' "
        "does not hold for a long-running follow"
    ),
)
async def test_disabling_a_user_closes_their_open_log_follow(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    project, manifest = _project(tmp_path, LONG_PROGRAM)
    alice_token = await login(anon, "alice", ALICE_PW)
    async with _client(runtime, alice_token) as alice:
        job_id = await submit(alice, project, agent.node_id, manifest)
        await wait_job(alice, job_id, states=frozenset({"running"}), timeout_s=60)
        url = f"wss://127.0.0.1:{runtime.http_port}/v1/jobs/{job_id}/logs/ws"
        async with connect(url, additional_headers=bearer(alice_token)) as ws:
            first = json.loads(await asyncio.wait_for(ws.recv(), 15))
            assert first
            await UserStore(engine).set_disabled("alice", disabled=True, actor="t")
            t0 = time.monotonic()
            closed = False
            with contextlib.suppress(asyncio.TimeoutError):
                while time.monotonic() - t0 < 8:
                    try:
                        await asyncio.wait_for(ws.recv(), 3)
                    except ConnectionClosed:
                        closed = True
                        break
            assert closed, "a disabled user is still receiving logs after 8 s"


# ------------------------------------------------------------------- blobs


async def test_bob_cannot_download_alices_inputs_or_outputs_by_digest(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    agent: ExecAgent,
    tmp_path: Path,
) -> None:
    project, manifest = _project(tmp_path, QUICK_PROGRAM)
    alice_token = await login(anon, "alice", ALICE_PW)
    bob_token = await login(anon, "bob", BOB_PW)
    async with (
        _client(runtime, alice_token) as alice,
        _client(runtime, bob_token) as bob,
    ):
        job_id = await submit(alice, project, agent.node_id, manifest)
        job = await wait_job(alice, job_id, timeout_s=60)
        assert job["state"] == "succeeded", job
        bundle = job["bundle_digest"].removeprefix("sha256:")
        output = job["attempts"][0]["output_digest"].removeprefix("sha256:")
        assert (await alice.get(f"/v1/blobs/{output}")).status_code == 200
        for digest in (bundle, output):
            r = await bob.get(f"/v1/blobs/{digest}")
            assert r.status_code == 404, (digest, r.status_code)
            absent = await bob.get(f"/v1/blobs/{'ab' * 32}")
            assert r.json() == absent.json()
            # Ranges and conditional requests do not bypass the owner check.
            r = await bob.get(f"/v1/blobs/{digest}", headers={"range": "bytes=0-10"})
            assert r.status_code == 404
            r = await bob.get(f"/v1/blobs/{digest}", headers={"if-none-match": "*"})
            assert r.status_code in (404,)
        # An anonymous caller gets nothing either.
        assert (await anon.get(f"/v1/blobs/{output}")).status_code == 401
        assert (await anon.head(f"/v1/blobs/{output}")).status_code == 401
        # path tricks
        for tricky in (
            f"/v1/blobs/{output.upper()}",
            f"/v1/blobs/{output}/",
            f"/v1/blobs//{output}",
            f"/v1/blobs/%2e%2e/blobs/{output}",
            f"/v1/blobs/{output}%00",
        ):
            r = await bob.get(tricky)
            assert r.status_code in (307, 400, 404, 405, 422), (tricky, r.status_code)
            if r.status_code == 307:  # slash redirect: the target is still refused
                assert (await bob.get(r.headers["location"])).status_code == 404


def _upload_fixture() -> tuple[bytes, str, list[bytes], int]:
    chunk = 64 * 1024
    data = b"".join(bytes([i % 251]) * chunk for i in range(3))
    parts = [data[i * chunk : (i + 1) * chunk] for i in range(3)]
    return data, hashlib.sha256(data).hexdigest(), parts, chunk


def _part_headers(data: bytes, chunk: int, part: bytes) -> dict[str, str]:
    return {
        "x-meshforge-blob-size": str(len(data)),
        "x-meshforge-chunk-size": str(chunk),
        "x-meshforge-chunk-sha256": hashlib.sha256(part).hexdigest(),
    }


async def _alice_uploads(alice: httpx.AsyncClient) -> httpx.Response:
    data, digest, parts, chunk = _upload_fixture()
    for index, part in enumerate(parts):
        r = await alice.put(
            f"/v1/blobs/{digest}/parts/{index}",
            content=part,
            headers=_part_headers(data, chunk, part),
        )
        assert r.status_code in (200, 201), (index, r.status_code, r.text)
    return await alice.post(f"/v1/blobs/{digest}/complete")


@pytest.mark.xfail(
    strict=True,
    reason=(
        "DEFECT T-2 (minor): any user may PUT a part for any digest; a part "
        "planted before the owner's upload is kept, so the owner's complete "
        "fails integrity (CB-E3 lists only existence as accepted). Needs the "
        "digest of the victim's blob (public data) and a race."
    ),
)
async def test_bob_cannot_poison_alices_resumable_upload(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    data, digest, _parts, chunk = _upload_fixture()
    alice_token = await login(anon, "alice", ALICE_PW)
    bob_token = await login(anon, "bob", BOB_PW)
    async with (
        _client(runtime, alice_token) as alice,
        _client(runtime, bob_token) as bob,
    ):
        garbage = b"\x66" * chunk
        await bob.put(
            f"/v1/blobs/{digest}/parts/0",
            content=garbage,
            headers=_part_headers(data, chunk, garbage),
        )
        done = await _alice_uploads(alice)
        assert done.status_code in (200, 201), (done.status_code, done.text)


async def test_after_a_poisoned_complete_the_owner_can_simply_retry(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    """Whatever Bob planted, the failed complete clears the staging so an
    honest retry succeeds (the damage is one wasted upload, not a stuck digest)."""
    data, digest, _parts, chunk = _upload_fixture()
    alice_token = await login(anon, "alice", ALICE_PW)
    bob_token = await login(anon, "bob", BOB_PW)
    async with (
        _client(runtime, alice_token) as alice,
        _client(runtime, bob_token) as bob,
    ):
        garbage = b"\x66" * chunk
        await bob.put(
            f"/v1/blobs/{digest}/parts/0",
            content=garbage,
            headers=_part_headers(data, chunk, garbage),
        )
        await _alice_uploads(alice)
        retry = await _alice_uploads(alice)
        assert retry.status_code in (200, 201), (retry.status_code, retry.text)
        assert (await bob.head(f"/v1/blobs/{digest}")).status_code in (200, 404)
