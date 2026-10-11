"""V0.3 G5 reviewer fix round: the exhaustive handler set and the ``/v1/nodes`` bound.

Two blockers/non-blockers from the G5 review:

* **B1** - ``install_error_handlers`` had no catch-all, so a ``KeyError`` in a
  shaper (or a ``ResponseValidationError``) escaped as Starlette's default
  ``text/plain`` ``Internal Server Error`` with no ``code`` at all, while the
  module docstring claimed "an unexpected exception looks the same to a client".
  The route that raises on purpose lives **here**, never in ``src/``.
* **N1** - ``GET /v1/nodes`` was the one unbounded list endpoint while
  ``ARCHITECTURE`` §20 asserts every list endpoint has a documented default and
  maximum ``limit``.

The catch-all must not shadow the more specific handlers, so each of them is
re-asserted with it installed (Starlette resolves by walking
``type(exc).__mro__``, and routes the ``Exception`` key to a *different*
middleware layer than the rest - the test is what keeps that true).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Annotated, Any, cast

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
import structlog
from fastapi import FastAPI, HTTPException, Query
from mf_api_records import node_record
from mf_users import ADMIN_TOKEN, bearer, static_auth

from meshforge.common.errors import FailureType, MeshForgeError
from meshforge.common.ids import new_node_id
from meshforge.controller.api.errors import (
    GENERIC_5XX_MESSAGE,
    ApiError,
    install_error_handlers,
)
from meshforge.controller.api.routes import DEFAULT_NODE_LIMIT, MAX_NODE_LIMIT
from meshforge.controller.http import create_http_app
from meshforge.controller.registry import (
    NodeRecord,
    Registry,
    RegistryError,
    UnknownNodeError,
)
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.store.errors import DatabaseUnavailableError

pytestmark = pytest.mark.unit

SECRET = "connection to 10.9.8.7 failed: password authentication failed for admin"


def _raising_app() -> FastAPI:
    """A test-local app whose routes raise one exception each, on purpose."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    @app.get("/boom/key-error")
    async def _key_error() -> dict[str, str]:
        payload: dict[str, str] = {}
        return {"value": payload["absent-key-" + SECRET]}

    @app.get("/boom/zero-division")
    async def _zero_division() -> dict[str, float]:
        return {"value": 1 / 0}

    @app.get("/boom/response-validation")
    async def _response_validation() -> dict[str, int]:
        # FastAPI raises ResponseValidationError: not a MeshForgeError and not an
        # HTTPException, so before B1 this was a bare text/plain 500.
        return {"value": "not-an-int"}  # type: ignore[dict-item]

    @app.get("/boom/api-error")
    async def _api_error() -> None:
        raise ApiError.invalid("limit must be between 1 and 10")

    @app.get("/boom/http-exception")
    async def _http_exception() -> None:
        raise HTTPException(status_code=409, detail="already enrolled")

    @app.get("/boom/unknown-node")
    async def _unknown_node() -> None:
        raise UnknownNodeError("no such node")

    @app.get("/boom/registry")
    async def _registry() -> None:
        raise RegistryError(SECRET, details={"operation": "list_nodes"})

    @app.get("/boom/database")
    async def _database() -> None:
        raise DatabaseUnavailableError(SECRET)

    @app.get("/boom/meshforge-4xx")
    async def _meshforge_4xx() -> None:
        raise MeshForgeError("this one is safe", failure_type=FailureType.NOT_FOUND)

    @app.get("/boom/meshforge-5xx")
    async def _meshforge_5xx() -> None:
        raise MeshForgeError(SECRET, failure_type=FailureType.INTERNAL_ERROR)

    @app.get("/validated")
    async def _validated(
        limit: Annotated[int, Query(ge=1, le=10)] = 5,
    ) -> dict[str, int]:
        return {"limit": limit}

    return app


@pytest_asyncio.fixture
async def raising() -> AsyncIterator[httpx.AsyncClient]:
    """``raise_app_exceptions=False``: Starlette's ``ServerErrorMiddleware``
    re-raises after the handler has produced the response (so a real server can
    log it), which the ASGI transport would otherwise surface as the exception
    instead of the HTTP answer a socket client sees."""
    transport = httpx.ASGITransport(app=_raising_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test", headers=bearer(ADMIN_TOKEN)
    ) as c:
        yield c


def _error(response: httpx.Response) -> dict[str, str]:
    body: Any = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message"}, error
    return {"code": str(error["code"]), "message": str(error["message"])}


# ------------------------------------------------------- B1: the catch-all


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/boom/key-error", "/boom/zero-division", "/boom/response-validation"],
)
async def test_an_unhandled_exception_is_the_typed_envelope_not_a_bare_500(
    raising: httpx.AsyncClient, path: str
) -> None:
    response = await raising.get(path)
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    error = _error(response)
    assert error["code"] == FailureType.INTERNAL_ERROR.value
    assert error["message"] == GENERIC_5XX_MESSAGE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    ["/boom/key-error", "/boom/zero-division", "/boom/response-validation"],
)
async def test_an_unhandled_exception_leaks_neither_its_text_nor_a_traceback(
    raising: httpx.AsyncClient, path: str
) -> None:
    """The exception's own text can carry a key, a query value or driver output."""
    body = (await raising.get(path)).text
    for leak in ("Traceback", "KeyError", "ZeroDivision", "absent-key", SECRET, ".py"):
        assert leak not in body, body
    assert "Internal Server Error" not in body  # Starlette's text/plain default


@pytest.mark.asyncio
async def test_the_unhandled_traceback_reaches_the_log_instead(
    raising: httpx.AsyncClient,
) -> None:
    with structlog.testing.capture_logs() as logs:
        await raising.get("/boom/key-error")
    events = [entry for entry in logs if entry["event"] == "controller.api.unhandled"]
    assert len(events) == 1
    assert events[0]["log_level"] == "error"
    assert events[0]["exc_info"] is True  # `.exception`, so the traceback is kept


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "status", "code", "message"),
    [
        (
            "/boom/api-error",
            400,
            FailureType.INVALID_INPUT.value,
            "limit must be between 1 and 10",
        ),
        ("/boom/http-exception", 409, FailureType.CONFLICT.value, "already enrolled"),
        ("/boom/unknown-node", 404, FailureType.NOT_FOUND.value, "no such node"),
        (
            "/boom/registry",
            503,
            FailureType.NETWORK_ERROR.value,
            "the controller's database is unavailable; retry shortly",
        ),
        (
            "/boom/database",
            503,
            FailureType.NETWORK_ERROR.value,
            "the controller's database is unavailable; retry shortly",
        ),
        ("/boom/meshforge-4xx", 404, FailureType.NOT_FOUND.value, "this one is safe"),
        (
            "/boom/meshforge-5xx",
            500,
            FailureType.INTERNAL_ERROR.value,
            GENERIC_5XX_MESSAGE,
        ),
    ],
)
async def test_the_catch_all_does_not_shadow_a_more_specific_handler(
    raising: httpx.AsyncClient, path: str, status: int, code: str, message: str
) -> None:
    """Every specific handler still owns its own status and code.

    If the ``Exception`` handler ever shadowed these, each would become a generic
    500 - which is precisely the regression this parametrisation exists to catch.
    """
    response = await raising.get(path)
    assert response.status_code == status
    assert _error(response) == {"code": code, "message": message}


@pytest.mark.asyncio
async def test_a_validation_error_still_names_the_field_and_not_the_value(
    raising: httpx.AsyncClient,
) -> None:
    response = await raising.get("/validated", params={"limit": "0"})
    assert response.status_code == 400
    error = _error(response)
    assert error["code"] == FailureType.INVALID_INPUT.value
    assert "limit" in error["message"]


@pytest.mark.asyncio
async def test_an_unknown_path_is_a_404_envelope_not_fastapis_detail(
    raising: httpx.AsyncClient,
) -> None:
    response = await raising.get("/no-such-route")
    assert response.status_code == 404
    assert _error(response)["code"] == FailureType.NOT_FOUND.value


@pytest.mark.asyncio
async def test_a_handler_never_depends_on_assert_being_enabled() -> None:
    """N4: the production handlers narrow with ``cast``, not ``assert``.

    ``python -O`` strips asserts, so a handler that *checked* its type with one
    would behave differently in an optimised run. The module must contain none.
    """
    import inspect

    from meshforge.controller.api import errors as errors_module

    source = inspect.getsource(errors_module.install_error_handlers)
    assert "assert " not in source
    assert "noqa: S101" not in source


# ------------------------------------------------ N1: GET /v1/nodes is bounded


class _LimitRegistry:
    """Just ``list_nodes``, recording the ``limit`` the route passed down."""

    def __init__(self, nodes: Sequence[NodeRecord]) -> None:
        self.nodes = sorted(nodes, key=lambda record: record.node_id)
        self.limits: list[int | None] = []

    async def list_nodes(self, *, limit: int | None = None) -> list[NodeRecord]:
        self.limits.append(limit)
        rows = list(self.nodes)
        return rows if limit is None else rows[:limit]


class _NoManager:
    def get(self, node_id: str) -> None:
        return None

    def __len__(self) -> int:
        return 0


class _NoTracker:
    def ring(self, node_id: str) -> None:
        return None

    def latest(self, node_id: str) -> None:
        return None

    def nodes(self) -> list[str]:
        return []


def _nodes_app(registry: _LimitRegistry) -> FastAPI:
    return create_http_app(
        cast("SessionManager", _NoManager()),
        auth=static_auth(),
        registry=cast("Registry", registry),
        resources=cast("ResourceTracker", _NoTracker()),
        clock=lambda: 1_000.0,
    )


@pytest_asyncio.fixture
async def nodes() -> AsyncIterator[tuple[httpx.AsyncClient, _LimitRegistry]]:
    registry = _LimitRegistry(
        [node_record(node_id=str(new_node_id())) for _ in range(7)]
    )
    transport = httpx.ASGITransport(app=_nodes_app(registry))
    async with httpx.AsyncClient(
        transport=transport, base_url="http://api.test", headers=bearer(ADMIN_TOKEN)
    ) as c:
        yield c, registry


@pytest.mark.asyncio
async def test_the_node_limit_bounds_the_rows_and_the_count(
    nodes: tuple[httpx.AsyncClient, _LimitRegistry],
) -> None:
    client, registry = nodes
    body = (await client.get("/v1/nodes", params={"limit": 3})).json()
    assert len(body["nodes"]) == 3
    assert body["count"] == 3
    assert "total" not in body  # would need a second query; see the route docstring
    assert registry.limits == [3]  # bounded in SQL, not sliced after the read


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, MAX_NODE_LIMIT])
async def test_the_node_limit_boundaries_are_accepted(
    nodes: tuple[httpx.AsyncClient, _LimitRegistry], limit: int
) -> None:
    client, registry = nodes
    assert (await client.get("/v1/nodes", params={"limit": limit})).status_code == 200
    assert registry.limits == [limit]


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, MAX_NODE_LIMIT + 1, "lots", ""])
async def test_a_node_limit_outside_the_range_is_a_400_envelope(
    nodes: tuple[httpx.AsyncClient, _LimitRegistry], limit: object
) -> None:
    client, registry = nodes
    response = await client.get("/v1/nodes", params={"limit": str(limit)})
    assert response.status_code == 400
    assert _error(response)["code"] == FailureType.INVALID_INPUT.value
    assert registry.limits == []  # refused before the database is touched


@pytest.mark.asyncio
async def test_the_default_limit_is_applied_and_published_in_the_schema(
    nodes: tuple[httpx.AsyncClient, _LimitRegistry],
) -> None:
    """The default must be a real bound, not "every row"."""
    client, registry = nodes
    assert (await client.get("/v1/nodes")).json()["count"] == 7
    assert registry.limits == [DEFAULT_NODE_LIMIT]
    schema = (await client.get("/v1/openapi.json")).json()
    parameters = schema["paths"]["/v1/nodes"]["get"]["parameters"]
    limit = next(p for p in parameters if p["name"] == "limit")
    assert limit["schema"]["default"] == DEFAULT_NODE_LIMIT
    assert limit["schema"]["maximum"] == MAX_NODE_LIMIT
    assert limit["schema"]["minimum"] == 1
