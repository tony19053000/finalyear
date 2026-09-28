"""D44 enforcement on the REST surface: missing/wrong/malformed bearer -> 401 envelope.

A tiny app with the real error handlers, the real app-wide mutation guard and the
real per-route dependency; a real token file and verifier. No socket.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from fastapi import APIRouter, Depends, FastAPI

from meshforge.controller.api.auth import (
    AUTH_STATE_KEY,
    MAX_AUTHORIZATION_CHARS,
    ApiAuth,
    FabricTokenRequired,
    guard_mutations,
)
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.fabric_token import FabricTokenVerifier, load_or_create_token

pytestmark = pytest.mark.asyncio


def _app(verifier: FabricTokenVerifier | None) -> FastAPI:
    app = FastAPI(dependencies=[Depends(guard_mutations)])
    setattr(app.state, AUTH_STATE_KEY, ApiAuth(verifier=verifier))
    install_error_handlers(app)
    router = APIRouter(prefix="/v1")

    @router.get("/nodes")
    async def open_read() -> dict[str, str]:
        return {"ok": "open"}

    @router.get("/jobs")
    async def job_read(_auth: FabricTokenRequired) -> dict[str, str]:
        return {"ok": "jobs"}

    @router.post("/jobs")
    async def job_create() -> dict[str, str]:
        return {"ok": "created"}

    @router.delete("/nodes/{node_id}")
    async def some_future_mutation(node_id: str) -> dict[str, str]:
        return {"ok": "deleted"}

    @router.put("/blobs/{digest}")
    async def blob_put(digest: str) -> dict[str, str]:
        return {"ok": "blob route checks its own credentials"}

    app.include_router(router)
    return app


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "fabric.token"


@pytest_asyncio.fixture
async def client(token_path: Path) -> AsyncIterator[httpx.AsyncClient]:
    token, _ = load_or_create_token(token_path)
    verifier = FabricTokenVerifier(token_path, clock=lambda: 0.0, initial=token)
    transport = httpx.ASGITransport(app=_app(verifier))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture
def token(token_path: Path, client: httpx.AsyncClient) -> str:
    return token_path.read_text().strip()


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


PROTECTED = [("GET", "/v1/jobs"), ("POST", "/v1/jobs"), ("DELETE", "/v1/nodes/x")]


@pytest.mark.parametrize(("method", "url"), PROTECTED)
async def test_protected_calls_need_the_token(
    client: httpx.AsyncClient, token: str, method: str, url: str
) -> None:
    response = await client.request(method, url)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"
    assert response.headers["www-authenticate"] == "Bearer"
    ok = await client.request(method, url, headers=_auth(token))
    assert ok.status_code == 200


@pytest.mark.parametrize(
    "header",
    [
        "Bearer " + "x" * 43,
        "Basic dXNlcjpwYXNz",
        "Bearer",
        "Bearer ",
        "bearer",
        "Token abc",
        "Bearer " + "x" * MAX_AUTHORIZATION_CHARS,
    ],
)
async def test_wrong_or_malformed_headers_are_401(
    client: httpx.AsyncClient, header: str
) -> None:
    response = await client.get("/v1/jobs", headers={"Authorization": header})
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "unauthorized"
    assert "x" * 43 not in response.text  # never echoes what was sent


async def test_scheme_is_case_insensitive_but_token_is_exact(
    client: httpx.AsyncClient, token: str
) -> None:
    ok = await client.get("/v1/jobs", headers={"Authorization": f"bEaReR {token}"})
    assert ok.status_code == 200
    bad = await client.get(
        "/v1/jobs", headers={"Authorization": f"Bearer {token.swapcase()}"}
    )
    assert bad.status_code == 401
    spaced = await client.get(
        "/v1/jobs", headers={"Authorization": f"Bearer {token} extra"}
    )
    assert spaced.status_code == 401


async def test_a_repeated_authorization_header_is_refused(
    client: httpx.AsyncClient, token: str
) -> None:
    value = _auth(token)["Authorization"]
    response = await client.get(
        "/v1/jobs", headers=[("Authorization", value), ("Authorization", value)]
    )
    assert response.status_code == 401


async def test_token_in_the_query_string_is_not_accepted(
    client: httpx.AsyncClient, token: str
) -> None:
    response = await client.get("/v1/jobs", params={"token": token})
    assert response.status_code == 401


async def test_open_reads_stay_open(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/nodes")).status_code == 200


async def test_blob_routes_are_left_to_their_own_check(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.put("/v1/blobs/abc")).status_code == 200


async def test_no_verifier_fails_closed() -> None:
    transport = httpx.ASGITransport(app=_app(None))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (
            await c.post("/v1/jobs", headers={"Authorization": "Bearer " + "a" * 43})
        ).status_code == 401
        assert (await c.get("/v1/nodes")).status_code == 200
