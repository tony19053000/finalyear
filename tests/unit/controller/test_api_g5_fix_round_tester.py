"""Independent tester coverage for the V0.3 G5 fix round (B1, N1, N2, N4, N5).

Written from the fix-round brief, not from reading ``errors.py``/``routes.py`` line
by line: the class of bug we are hunting is a *fix meeting an existing feature*
(the G4 pattern in ``docs/SESSION_HANDOFF.md`` §2a) - a catch-all handler that
quietly widens to swallow something it must not, or a new bound that a caller
elsewhere silently stops honouring.

Never imports anything from ``tests/component`` and never modifies ``src/``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from fastapi import FastAPI

from meshforge.common.errors import FailureType
from meshforge.controller.api.errors import GENERIC_5XX_MESSAGE, install_error_handlers
from meshforge.controller.api.routes import DEFAULT_NODE_LIMIT, MAX_NODE_LIMIT
from meshforge.controller.http import create_http_app
from meshforge.controller.registry import NodeRecord, Registry
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.manager import SessionManager

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------
# B1's inverse risk: the catch-all must not swallow what must propagate.
# --------------------------------------------------------------------------


def _boom_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    @app.get("/boom/cancelled")
    async def _cancelled() -> dict[str, str]:
        raise asyncio.CancelledError

    @app.get("/boom/base-exception")
    async def _base_exception() -> dict[str, str]:
        # A textbook BaseException that is not a CancelledError: if the handler
        # were ever registered against BaseException (or something equivalent
        # widened it), this would also turn into a 500 instead of propagating.
        raise KeyboardInterrupt

    @app.get("/boom/huge-non-ascii")
    async def _huge_non_ascii() -> dict[str, str]:
        # 200 KB of non-ASCII text plus a lone surrogate glued on: str(exc) must
        # not itself crash the handler (encoding the response, logging it).
        raise ValueError("é" * 200_000 + "\udcff")

    return app


@pytest_asyncio.fixture
async def boom() -> AsyncIterator[httpx.AsyncClient]:
    """Default ``raise_app_exceptions=True``: this is the client-visible check
    for BaseException - if Starlette ever surfaced a 500 instead of letting the
    exception propagate, the ASGI transport would raise it here, and the test
    would then assert on the *response*, proving the swallow happened."""
    transport = httpx.ASGITransport(app=_boom_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        yield c


@pytest.mark.asyncio
async def test_a_cancelled_error_is_not_turned_into_a_500(
    boom: httpx.AsyncClient,
) -> None:
    """``asyncio.CancelledError`` must propagate, not become a typed 500.

    A handler that downgrades cancellation to a 500 breaks graceful shutdown: the
    task cancellation that is supposed to unwind the request would instead look
    like a successful (if failed) HTTP exchange.
    """
    with pytest.raises(asyncio.CancelledError):
        await boom.get("/boom/cancelled")


@pytest.mark.asyncio
async def test_a_non_cancelled_base_exception_also_propagates(
    boom: httpx.AsyncClient,
) -> None:
    with pytest.raises(KeyboardInterrupt):
        await boom.get("/boom/base-exception")


@pytest.mark.skip(
    reason="Lead 2026-09-23: this test HANGS, and not because of the code under test. "
    "It logs a 200 KB exception through structlog's DEFAULT chain (the fixture never "
    "calls configure_logging), which is superlinear in message length: measured 0.43 s "
    "at 1 K chars and 2.26 s at 20 K, so 200 K is minutes of CPU at 100%. With logging "
    "configured - how the controller actually runs - the same request is 2-3 ms and "
    "returns the 92-byte typed envelope, i.e. B1 is correct. Re-enable by calling "
    "configure_logging() in the fixture and dropping the size to a few KB."
)
@pytest.mark.asyncio
async def test_a_huge_non_ascii_exception_message_still_yields_the_envelope(
    boom: httpx.AsyncClient,
) -> None:
    """The exception text is never echoed, so its size and encoding must not be
    able to break the handler itself (a crash here would be a *worse* leak: a
    bare Starlette 500 with no typed code, exactly what B1 was meant to fix)."""
    response = await boom.get("/boom/huge-non-ascii")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body == {
        "error": {
            "code": FailureType.INTERNAL_ERROR.value,
            "message": GENERIC_5XX_MESSAGE,
        }
    }
    assert "é" not in response.text
    assert len(response.content) < 1_000  # nowhere near the 200 KB exception text


@pytest.mark.asyncio
async def test_healthz_is_unaffected_by_the_catch_all_being_installed() -> None:
    """``/healthz`` is mounted after ``install_error_handlers`` in the real
    factory; confirm the catch-all changes nothing about its 200 path."""

    class _NoManager:
        def __len__(self) -> int:
            return 0

    class _NoTracker:
        def nodes(self) -> list[str]:
            return []

    class _NoRegistry:
        pass

    app = create_http_app(
        cast("SessionManager", _NoManager()),
        registry=cast("Registry", _NoRegistry()),
        resources=cast("ResourceTracker", _NoTracker()),
        clock=lambda: 0.0,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        response = await c.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "sessions": 0}


# --------------------------------------------------------------------------
# N1: the new bound meeting the existing /v1/nodes endpoint.
# --------------------------------------------------------------------------


class _OrderedRegistry:
    """Nodes ordered by ``node_id`` the way real SQL ``ORDER BY node_id`` is."""

    def __init__(self, nodes: list[NodeRecord]) -> None:
        self.nodes = sorted(nodes, key=lambda r: r.node_id)
        self.calls: list[int | None] = []

    async def list_nodes(self, *, limit: int | None = None) -> list[NodeRecord]:
        self.calls.append(limit)
        return list(self.nodes) if limit is None else list(self.nodes[:limit])


def _fake_node(i: int) -> NodeRecord:
    from mf_api_records import node_record

    return node_record(node_id=f"node_{i:026d}")


def _nodes_app(registry: _OrderedRegistry) -> FastAPI:
    class _NoManager:
        def get(self, node_id: str) -> None:
            return None

        def __len__(self) -> int:
            return 0

    class _NoTracker:
        def latest(self, node_id: str) -> None:
            return None

        def nodes(self) -> list[str]:
            return []

    return create_http_app(
        cast("SessionManager", _NoManager()),
        registry=cast("Registry", registry),
        resources=cast("ResourceTracker", _NoTracker()),
        clock=lambda: 1_000.0,
    )


@pytest_asyncio.fixture
async def many_nodes() -> AsyncIterator[tuple[httpx.AsyncClient, _OrderedRegistry]]:
    registry = _OrderedRegistry([_fake_node(i) for i in range(12)])
    transport = httpx.ASGITransport(app=_nodes_app(registry))
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        yield c, registry


@pytest.mark.asyncio
async def test_repeated_calls_with_the_same_limit_return_the_same_subset(
    many_nodes: tuple[httpx.AsyncClient, _OrderedRegistry],
) -> None:
    """Two calls with an identical ``limit`` must not silently return different
    windows - the ordering has to be deterministic, not "whatever came back"."""
    client, _ = many_nodes
    first = (await client.get("/v1/nodes", params={"limit": 5})).json()
    second = (await client.get("/v1/nodes", params={"limit": 5})).json()
    assert [n["node_id"] for n in first["nodes"]] == [
        n["node_id"] for n in second["nodes"]
    ]


@pytest.mark.asyncio
async def test_a_duplicated_limit_parameter_is_a_400_or_uses_one_value_never_both(
    many_nodes: tuple[httpx.AsyncClient, _OrderedRegistry],
) -> None:
    """``?limit=1&limit=500`` must not silently combine into something outside
    the declared range - FastAPI/Starlette resolves query duplicates to the last
    occurrence, so this pins that the registry only ever sees one bounded value."""
    client, registry = many_nodes
    response = await client.get("/v1/nodes?limit=1&limit=500")
    assert response.status_code == 200
    assert registry.calls[-1] in (1, 500)  # whichever Starlette picked, not both
    assert isinstance(registry.calls[-1], int)


@pytest.mark.asyncio
async def test_the_no_limit_registry_path_is_unreachable_from_the_route(
    many_nodes: tuple[httpx.AsyncClient, _OrderedRegistry],
) -> None:
    """The route always supplies a ``limit`` (default or explicit) - the "no
    bound at all" branch of ``Registry.list_nodes`` only exists for other,
    non-HTTP callers. Confirms N1 did not leave a request path that skips the
    bound entirely."""
    client, registry = many_nodes
    await client.get("/v1/nodes")
    assert None not in registry.calls
    assert registry.calls == [DEFAULT_NODE_LIMIT]


@pytest.mark.asyncio
async def test_max_node_limit_boundary_is_a_real_sql_bound_not_a_python_slice(
    many_nodes: tuple[httpx.AsyncClient, _OrderedRegistry],
) -> None:
    client, registry = many_nodes
    response = await client.get("/v1/nodes", params={"limit": MAX_NODE_LIMIT})
    assert response.status_code == 200
    assert registry.calls[-1] == MAX_NODE_LIMIT
