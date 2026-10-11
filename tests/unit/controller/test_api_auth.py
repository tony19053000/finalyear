"""REST authentication (V0.7 G4, D141/D148; was D44): one user dependency.

A tiny app with the real error handlers, the real app-wide guard
(:func:`guard_requests`) and the real per-route dependencies; users come from an
in-memory directory, the fabric token from a real token file and verifier. No
socket. Pinned: every route but ``/healthz`` and the login needs a user (D148,
node reads included); one 401 for missing / malformed / unknown; the legacy
fabric token is refused unless switched on, then it is an admin and its use is
noted; admin routes are 403 for a user; a directory failure is 503, never a
silent anonymous.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from fastapi import APIRouter, FastAPI
from fastapi import Depends as FastDepends
from mf_users import (
    ADMIN_TOKEN,
    ALICE,
    ALICE_TOKEN,
    StaticUsers,
    bearer,
)

from meshforge.common.api_token import new_api_token
from meshforge.controller.api.auth import (
    AUTH_STATE_KEY,
    MAX_AUTHORIZATION_CHARS,
    AdminUser,
    ApiAuth,
    CurrentUser,
    guard_requests,
)
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.fabric_token import FabricTokenVerifier, load_or_create_token
from meshforge.controller.store.errors import DatabaseUnavailableError

pytestmark = [pytest.mark.asyncio, pytest.mark.unit]


def _app(auth: ApiAuth) -> FastAPI:
    app = FastAPI(dependencies=[FastDepends(guard_requests)])
    setattr(app.state, AUTH_STATE_KEY, auth)
    install_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    router = APIRouter(prefix="/v1")

    @router.get("/nodes")
    async def node_read() -> dict[str, str]:
        return {"ok": "nodes"}

    @router.get("/jobs")
    async def job_read(user: CurrentUser) -> dict[str, str | None]:
        return {"user": user.user_id}

    @router.post("/jobs")
    async def job_create() -> dict[str, str]:
        return {"ok": "created"}

    @router.delete("/nodes/{node_id}")
    async def some_future_mutation(node_id: str) -> dict[str, str]:
        return {"ok": "deleted"}

    @router.post("/admin-only")
    async def admin_only(admin: AdminUser) -> dict[str, str]:
        return {"actor": admin.actor}

    @router.post("/auth/login")
    async def fake_login() -> dict[str, str]:
        return {"ok": "login needs no token"}

    @router.put("/blobs/{digest}")
    async def blob_put(digest: str) -> dict[str, str]:
        return {"ok": "blob route checks its own credentials"}

    app.include_router(router)
    return app


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "fabric.token"


def _verifier(token_path: Path) -> FabricTokenVerifier:
    token, _ = load_or_create_token(token_path)
    return FabricTokenVerifier(token_path, clock=lambda: 0.0, initial=token)


@pytest.fixture
def users() -> StaticUsers:
    return StaticUsers()


async def _client(auth: ApiAuth) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(auth)), base_url="http://t"
    )


@pytest_asyncio.fixture
async def client(
    token_path: Path, users: StaticUsers
) -> AsyncIterator[httpx.AsyncClient]:
    async with await _client(ApiAuth(verifier=_verifier(token_path), users=users)) as c:
        yield c


PROTECTED = [
    ("GET", "/v1/jobs"),
    ("POST", "/v1/jobs"),
    ("DELETE", "/v1/nodes/x"),
    ("GET", "/v1/nodes"),  # D148: node reads need a user too
]


@pytest.mark.parametrize(("method", "url"), PROTECTED)
async def test_every_route_needs_a_user(
    client: httpx.AsyncClient, method: str, url: str
) -> None:
    response = await client.request(method, url)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert "compute login" in response.json()["error"]["message"]
    assert response.headers["www-authenticate"] == "Bearer"
    ok = await client.request(method, url, headers=bearer(ALICE_TOKEN))
    assert ok.status_code == 200


async def test_only_healthz_and_login_are_open(client: httpx.AsyncClient) -> None:
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.post("/v1/auth/login")).status_code == 200


async def test_route_gets_the_principal(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/jobs", headers=bearer(ALICE_TOKEN))
    assert response.json() == {"user": ALICE.user_id}


@pytest.mark.parametrize(
    "header",
    [
        "Bearer " + "x" * 43,
        "Bearer " + new_api_token().get_secret_value(),  # well-formed, unknown
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "bearer",
        "Token abc",
        "Bearer " + "x" * MAX_AUTHORIZATION_CHARS,
    ],
)
async def test_wrong_unknown_or_malformed_headers_are_one_401(
    client: httpx.AsyncClient, header: str
) -> None:
    response = await client.get("/v1/jobs", headers={"Authorization": header})
    assert response.status_code == 401
    missing = await client.get("/v1/jobs")
    assert response.json() == missing.json()  # one message for every reason
    assert "x" * 43 not in response.text  # never echoes what was sent


async def test_scheme_is_case_insensitive_but_token_is_exact(
    client: httpx.AsyncClient,
) -> None:
    ok = await client.get(
        "/v1/jobs", headers={"Authorization": f"bEaReR {ALICE_TOKEN}"}
    )
    assert ok.status_code == 200
    bad = await client.get(
        "/v1/jobs", headers={"Authorization": f"Bearer {ALICE_TOKEN.swapcase()}"}
    )
    assert bad.status_code == 401
    spaced = await client.get(
        "/v1/jobs", headers={"Authorization": f"Bearer {ALICE_TOKEN} extra"}
    )
    assert spaced.status_code == 401


async def test_a_repeated_authorization_header_is_refused(
    client: httpx.AsyncClient,
) -> None:
    value = bearer(ALICE_TOKEN)["authorization"]
    response = await client.get(
        "/v1/jobs", headers=[("Authorization", value), ("Authorization", value)]
    )
    assert response.status_code == 401


async def test_token_in_the_query_string_is_not_accepted(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/v1/jobs", params={"token": ALICE_TOKEN})
    assert response.status_code == 401


async def test_blob_routes_are_left_to_their_own_check(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.put("/v1/blobs/abc")).status_code == 200


async def test_admin_route_is_403_for_a_user_and_200_for_an_admin(
    client: httpx.AsyncClient,
) -> None:
    refused = await client.post("/v1/admin-only", headers=bearer(ALICE_TOKEN))
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "unauthorized"
    assert (await client.post("/v1/admin-only")).status_code == 401
    ok = await client.post("/v1/admin-only", headers=bearer(ADMIN_TOKEN))
    assert ok.status_code == 200
    assert ok.json()["actor"].startswith("user:usr_")


async def test_legacy_fabric_token_is_refused_by_default(
    client: httpx.AsyncClient, token_path: Path, users: StaticUsers
) -> None:
    fabric = token_path.read_text().strip()  # noqa: ASYNC240 - tiny test file
    response = await client.get("/v1/jobs", headers=bearer(fabric))
    assert response.status_code == 401
    assert users.legacy_sources == []


async def test_legacy_fabric_token_when_switched_on_is_a_noted_admin(
    token_path: Path, users: StaticUsers
) -> None:
    auth = ApiAuth(
        verifier=_verifier(token_path), users=users, legacy_fabric_token=True
    )
    fabric = token_path.read_text().strip()  # noqa: ASYNC240 - tiny test file
    async with await _client(auth) as c:
        ok = await c.post("/v1/admin-only", headers=bearer(fabric))
        assert ok.status_code == 200
        assert ok.json()["actor"] == "rest:fabric-token"
        assert (await c.get("/v1/jobs", headers=bearer(fabric))).json() == {
            "user": None
        }
        wrong = await c.get("/v1/jobs", headers=bearer("y" * 43))
        assert wrong.status_code == 401
    assert len(users.legacy_sources) == 2


async def test_no_directory_fails_closed(token_path: Path) -> None:
    async with await _client(ApiAuth(verifier=_verifier(token_path))) as c:
        response = await c.get("/v1/jobs", headers=bearer(ALICE_TOKEN))
        assert response.status_code == 401
        assert (await c.get("/healthz")).status_code == 200


async def test_directory_failure_is_503_not_anonymous(
    client: httpx.AsyncClient, users: StaticUsers
) -> None:
    users.fail = DatabaseUnavailableError("db down")
    response = await client.get("/v1/jobs", headers=bearer(ALICE_TOKEN))
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "network_error"


async def test_guard_and_route_share_one_lookup(
    client: httpx.AsyncClient, users: StaticUsers
) -> None:
    calls = 0
    original = users.authenticate

    async def counting(token: str):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return await original(token)

    users.authenticate = counting  # type: ignore[method-assign]
    assert (
        await client.get("/v1/jobs", headers=bearer(ALICE_TOKEN))
    ).status_code == 200
    assert calls == 1
