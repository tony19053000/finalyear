"""V1.0 G3 (D413, reviewer M2): the full route inventory needs credentials.

Builds the **real** :func:`meshforge.controller.http.create_http_app` with every
optional router mounted (the same inert collaborators as
:func:`~meshforge.controller.http.openapi_document`), walks every HTTP and
WebSocket route (nested routers and mounts included) and calls each one

* without credentials,
* with a well-formed but unknown API token,
* with a wrong legacy fabric token,
* with an unknown browser-session cookie (+ CSRF header),
* with a node certificate of a **foreign** fabric (mtls mode) plus a wrong token.

Every call must be refused - 401, 403, or 410 (the setup API after setup,
D392) - and a WebSocket must be closed before it is accepted. Exceptions are
only the explicit :data:`ALLOWED` list, each with its decision. The list is
pinned against ``OPEN_ROUTES`` both ways, so a new open route must be added
here with its reason, and an allow-list entry that no longer exists fails.

No socket: ``httpx.ASGITransport`` and Starlette's test client. The socket
half (every non-loopback listener demands TLS + mTLS or a token) is
``tests/component/controller/test_v10_g3_listener_auth.py``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import re
from collections.abc import AsyncIterator, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Final

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from fastapi import APIRouter, FastAPI
from fastapi.routing import iter_route_contexts
from mf_users import StaticUsers
from starlette.routing import BaseRoute, Mount, Route, WebSocketRoute
from starlette.types import ASGIApp, Receive, Scope, Send

from meshforge.common import pki
from meshforge.common.api_token import BROWSER_SESSION_PREFIX, new_api_token
from meshforge.common.ids import new_fabric_id, new_node_id
from meshforge.controller.api.auth import OPEN_ROUTES, ApiAuth
from meshforge.controller.auth.browser import CSRF_COOKIE, CSRF_HEADER, SESSION_COOKIE
from meshforge.controller.blobs import BlobStore
from meshforge.controller.fabric_token import FabricTokenVerifier, load_or_create_token
from meshforge.controller.http import OPENAPI_URL, create_http_app
from meshforge.controller.http_tls import TLS_PEER_STATE_KEY, NodeCertPolicy

pytestmark = [pytest.mark.unit, pytest.mark.security]

ALLOWED: Final[dict[tuple[str, str], str]] = {
    ("GET", "/healthz"): "D148: the liveness probe is the one open read",
    ("GET", OPENAPI_URL): "RE-I2 accepted: the API's shape, no fabric data",
    ("HEAD", OPENAPI_URL): "RE-I2 accepted: HEAD of the schema (Starlette route)",
    ("POST", "/v1/auth/login"): "D141/D148: the way in (password -> token)",
    ("POST", "/v1/session"): "D170: the browser's way in",
    ("GET", "/v1/session"): "D191 b: 'signed out' is 200 {session: null}",
    ("DELETE", "/v1/session"): "D170: logout clears the cookies even when dead",
}
"""(method, route template) -> decision: the only routes that answer without
credentials. Everything else must refuse."""
REFUSED: Final = frozenset({401, 403, 410})
SETUP_PREFIX: Final = "/v1/setup/"


class _Users(StaticUsers):
    """:class:`StaticUsers` that also takes the token kind (browser sessions)."""

    async def authenticate(self, token: str, *, kind: object = None) -> Any:
        return await super().authenticate(token)


def _inert() -> Any:
    return SimpleNamespace()


def _foreign_cert_der() -> bytes:
    """A real node certificate - of another fabric's CA (the wrong cert)."""
    fabric = new_fabric_id()
    node = new_node_id()
    ca = pki.create_ca(fabric)
    key = pki.generate_key()
    cert = pki.issue_node_cert(
        ca, pki.build_csr(key, fabric_id=fabric, node_id=node),
        node_id=node, fabric_id=fabric,
    )  # fmt: skip
    from cryptography.hazmat.primitives.serialization import Encoding

    return cert.public_bytes(Encoding.DER)


class _WithPeerCert:
    """ASGI wrapper: what uvicorn's peer-cert protocol puts in ``scope['state']``."""

    def __init__(self, app: ASGIApp, der: bytes) -> None:
        self.app = app
        self.der = der

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in ("http", "websocket"):
            scope = {
                **scope,
                "state": {**scope.get("state", {}), TLS_PEER_STATE_KEY: self.der},
            }
        await self.app(scope, receive, send)


def _app(tmp_path: Path) -> FastAPI:
    token_path = tmp_path / "fabric.token"
    token, _ = load_or_create_token(token_path)
    auth = ApiAuth(
        verifier=FabricTokenVerifier(token_path, clock=lambda: 0.0, initial=token),
        users=_Users(),
        legacy_fabric_token=True,  # the strictest case: the token is checked
        node_certs=NodeCertPolicy(
            fabric_id=new_fabric_id(),
            clock=lambda: dt.datetime.now(dt.UTC),
        ),
    )
    return create_http_app(
        [],  # type: ignore[arg-type]  # the session manager: len() = 0 live streams
        registry=_inert(),
        resources=_inert(),
        clock=lambda: 0.0,
        auth=auth,
        blobs=BlobStore(tmp_path / "blobs", max_blob_bytes=10**6),
        jobs=SimpleNamespace(inputs=_inert(), prepare=_inert()),  # type: ignore[arg-type]
        network=_inert(),
        invites=_inert(),
        revocations=_inert(),
        users=_inert(),
        secrets=_inert(),
        events=_inert(),
        alerts=_inert(),
        telemetry=_inert(),
        bench=SimpleNamespace(runner=None),  # type: ignore[arg-type]
        planner=_inert(),
        planner_eval=_inert(),
        ml_dataset=_inert(),
        ml_models=_inert(),
        ml_predictions=_inert(),
        ml_classify=_inert(),
        decision_replay=_inert(),
        web_dist=None,
    )


def walk(
    routes: Sequence[BaseRoute], prefix: str = ""
) -> Iterator[tuple[str, BaseRoute]]:
    """(full path template, route) of every endpoint. FastAPI >= 0.141 keeps an
    included router as one lazy ``_IncludedRouter`` entry (nested routers inside
    it); :func:`fastapi.routing.iter_route_contexts` expands them with their
    effective prefix. Starlette mounts are recursed here."""
    for context in iter_route_contexts(routes):
        route = context.original_route
        if isinstance(route, Mount):
            yield from walk(list(route.routes), prefix + route.path)
            continue
        # a WebSocket's context has no path of its own; its route has the full one
        path = context.path or getattr(route, "path", None)
        if not isinstance(route, Route | WebSocketRoute) or not isinstance(path, str):
            raise AssertionError(f"unknown route kind {type(route)!r}")
        yield prefix + path, route


_PARAM: Final = re.compile(r"\{(\w+)(?::(\w+))?\}")
_VALUES: Final = {"int": "1", "float": "1.0", "path": "a/b"}
_BY_NAME: Final = {"sha256": "ab" * 32, "digest": "ab" * 32, "index": "0"}
"""Well-formed values, so a refusal is the credential check, not a 422."""


def concrete(template: str) -> str:
    """The template with each parameter filled with a value its convertor accepts."""
    return _PARAM.sub(
        lambda m: _BY_NAME.get(m.group(1)) or _VALUES.get(m.group(2) or "", "x" * 8),
        template,
    )


def _http_cases(app: FastAPI) -> list[tuple[str, str, str]]:
    return [
        (method, template, concrete(template))
        for template, route in walk(list(app.routes))
        if isinstance(route, Route)
        for method in sorted(route.methods or ())
    ]


def _ws_cases(app: FastAPI) -> list[tuple[str, str]]:
    return [
        (template, concrete(template))
        for template, route in walk(list(app.routes))
        if isinstance(route, WebSocketRoute)
    ]


WRONG_API_TOKEN: Final = new_api_token().get_secret_value()
WRONG_SESSION: Final = BROWSER_SESSION_PREFIX + "A" * 43
CREDENTIALS: Final[dict[str, dict[str, str]]] = {
    "none": {},
    "unknown_api_token": {"authorization": f"Bearer {WRONG_API_TOKEN}"},
    "wrong_fabric_token": {"authorization": "Bearer " + "x" * 43},
    "unknown_session_cookie": {
        "cookie": f"{SESSION_COOKIE}={WRONG_SESSION}; {CSRF_COOKIE}=c",
        CSRF_HEADER: "c",
    },
}


@pytest.fixture(scope="module")
def module_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("route-inventory")


@pytest.fixture(scope="module")
def app(module_tmp: Path) -> FastAPI:
    return _app(module_tmp)


@pytest_asyncio.fixture
async def clients(app: FastAPI) -> AsyncIterator[dict[str, httpx.AsyncClient]]:
    plain = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )
    certed = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_WithPeerCert(app, _foreign_cert_der())),
        base_url="http://t",
    )
    async with plain, certed:
        yield {"plain": plain, "foreign_cert": certed}


# ----------------------------------------------------------------- inventory
def test_the_inventory_is_complete(app: FastAPI) -> None:
    """Every OpenAPI operation is walked; the walk sees more (WebSockets)."""
    document = app.openapi()
    documented = {
        (method.upper(), path)
        for path, item in document["paths"].items()
        for method in item
        if method in ("get", "post", "put", "patch", "delete")
    }
    walked = {(m, t) for m, t, _ in _http_cases(app)}
    assert documented <= walked, sorted(documented - walked)
    assert len(walked) >= 75, len(walked)  # all optional routers are mounted
    assert {t for t, _ in _ws_cases(app)} >= {
        "/v1/events/ws",
        "/v1/jobs/{job_id}/logs/ws",
    }


def test_allow_list_matches_the_open_routes_both_ways(app: FastAPI) -> None:
    walked = {(m, t) for m, t, _ in _http_cases(app)}
    assert set(ALLOWED) <= walked, sorted(set(ALLOWED) - walked)
    open_now = {r for r in OPEN_ROUTES if not r[1].startswith(SETUP_PREFIX)}
    assert open_now | {("GET", OPENAPI_URL), ("HEAD", OPENAPI_URL)} == set(ALLOWED), (
        "OPEN_ROUTES changed: add the route to ALLOWED with its decision, or "
        "remove a stale entry"
    )
    for reason in ALLOWED.values():
        assert reason.split(":")[0].strip(), reason


# ------------------------------------------------------------------ HTTP calls
PART_TEMPLATE: Final = "/v1/blobs/{sha256}/parts/{index}"
_PART: Final = 64 * 1024  # common.cas.MIN_CHUNK_SIZE: a one-part blob
PART_HEADERS: Final = {
    "x-meshforge-blob-size": str(_PART),
    "x-meshforge-chunk-size": str(_PART),
    "x-meshforge-chunk-sha256": "cd" * 32,
}
"""A well-formed part upload, so the refusal is the credential check (its
header format is validated before the grant lookup, which needs the size;
nothing is read or stored first - V1.0 G3 report)."""


async def _call(
    client: httpx.AsyncClient,
    method: str,
    template: str,
    url: str,
    headers: dict[str, str],
) -> httpx.Response:
    if (method, template) == ("PUT", PART_TEMPLATE):
        return await client.request(
            method, url, headers={**headers, **PART_HEADERS}, content=b"\0" * _PART
        )
    return await client.request(method, url, headers=headers)


async def refusal_failures(
    app: FastAPI, clients: dict[str, httpx.AsyncClient]
) -> tuple[list[str], int]:
    """(every non-refused call, how many calls) over the whole inventory."""
    failures: list[str] = []
    checked = 0
    for method, template, url in _http_cases(app):
        if (method, template) in ALLOWED:
            continue
        for client_name, client in clients.items():
            for cred_name, headers in CREDENTIALS.items():
                if client_name == "foreign_cert" and cred_name == "none":
                    continue  # test_a_foreign_cert_alone_opens_nothing
                response = await _call(client, method, template, url, headers)
                checked += 1
                if response.status_code not in REFUSED:
                    failures.append(
                        f"{method} {template} [{client_name}/{cred_name}] -> "
                        f"{response.status_code} {response.text[:120]}"
                    )
                elif template.startswith(SETUP_PREFIX) and response.status_code != 410:
                    failures.append(f"{method} {template}: setup is not 410 Gone")
                if WRONG_API_TOKEN in response.text or WRONG_SESSION in response.text:
                    failures.append(f"{method} {template}: echoed a credential")
    return failures, checked


@pytest.mark.asyncio
async def test_every_http_route_refuses_without_valid_credentials(
    app: FastAPI, clients: dict[str, httpx.AsyncClient]
) -> None:
    failures, checked = await refusal_failures(app, clients)
    assert checked > 500, checked
    assert failures == [], "\n".join(failures)


@pytest.mark.asyncio
async def test_the_check_catches_a_planted_open_route(tmp_path: Path) -> None:
    """Meta: a route that skips the guard (it sits under the blob prefix, which
    checks its own credentials) and answers 200 is reported."""
    app = _app(tmp_path)
    leak = APIRouter(prefix="/v1/blobs")

    @leak.get("/{sha256}/planted-leak")
    async def planted(sha256: str) -> dict[str, str]:
        return {"secret": "fabric data"}

    app.include_router(leak)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as plain:
        failures, _ = await refusal_failures(app, {"plain": plain})
    assert any("planted-leak" in f and "-> 200" in f for f in failures), failures


@pytest.mark.asyncio
async def test_a_foreign_cert_alone_opens_nothing(
    app: FastAPI, clients: dict[str, httpx.AsyncClient]
) -> None:
    client = clients["foreign_cert"]
    for method, template, url in _http_cases(app):
        if (method, template) in ALLOWED:
            continue
        response = await _call(client, method, template, url, {})
        assert response.status_code in REFUSED, (method, template, response.status_code)


@pytest.mark.asyncio
async def test_allowed_routes_are_really_open(
    app: FastAPI, clients: dict[str, httpx.AsyncClient]
) -> None:
    """The allow-list is not wider than needed: these do answer without a user
    (a 4xx for a missing body still means the guard let it through)."""
    client = clients["plain"]
    assert (await client.get("/healthz")).status_code != 401
    schema = await client.get(OPENAPI_URL)
    assert schema.status_code == 200 and "paths" in schema.json()


# ---------------------------------------------------------------- WebSockets
async def ws_first_message(
    app: ASGIApp, path: str, headers: dict[str, str]
) -> dict[str, Any]:
    """Drive one WebSocket handshake at the ASGI level; the app's first message
    (``websocket.close`` = refused before accept, ``websocket.accept`` = let in)."""
    sent: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    connected = False
    never = asyncio.Event()

    async def receive() -> dict[str, Any]:
        nonlocal connected
        if not connected:
            connected = True
            return {"type": "websocket.connect"}
        await never.wait()
        return {"type": "websocket.disconnect", "code": 1000}  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        await sent.put(dict(message))

    scope: dict[str, Any] = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]
        + [(b"host", b"t")],
        "client": ("127.0.0.1", 50000),
        "server": ("t", 80),
        "subprotocols": [],
        "state": {},
    }
    task = asyncio.ensure_future(app(scope, receive, send))  # type: ignore[arg-type]
    try:
        return await asyncio.wait_for(sent.get(), timeout=10)
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _refused(first: dict[str, Any]) -> bool:
    if first["type"] == "websocket.close":
        return int(first.get("code", 1000)) in (1008, 4401, 4403)
    if first["type"] == "websocket.http.response.start":
        return int(first["status"]) in REFUSED
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize("cred_name", sorted(CREDENTIALS))
async def test_every_websocket_is_closed_before_accept(
    app: FastAPI, cred_name: str
) -> None:
    cases = _ws_cases(app)
    assert cases
    for template, url in cases:
        first = await ws_first_message(app, url, CREDENTIALS[cred_name])
        assert _refused(first), (template, cred_name, first)


@pytest.mark.asyncio
async def test_websocket_with_a_foreign_cert_is_closed(app: FastAPI) -> None:
    wrapped = _WithPeerCert(app, _foreign_cert_der())
    for template, url in _ws_cases(app):
        for headers in ({}, {"authorization": f"Bearer {WRONG_API_TOKEN}"}):
            first = await ws_first_message(wrapped, url, headers)
            assert _refused(first), (template, first)
