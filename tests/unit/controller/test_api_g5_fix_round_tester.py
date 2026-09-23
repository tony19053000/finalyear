"""Independent tester coverage for the V0.3 G5 fix round (B1, N1, N2, N4, N5).

Written from the fix-round brief, not from reading ``errors.py``/``routes.py`` line
by line: the class of bug we are hunting is a *fix meeting an existing feature*
(the G4 pattern in ``docs/SESSION_HANDOFF.md`` §2a) - a catch-all handler that
quietly widens to swallow something it must not, or a new bound that a caller
elsewhere silently stops honouring.

Never imports anything from ``tests/component`` and never modifies ``src/``.

**Second-round addition (see the report for the full writeup).** The section
below headed "the huge exception, done correctly" replaces a test the cut tester
left behind, ``test_a_huge_non_ascii_exception_message_still_yields_the_envelope``,
which wedged the suite for minutes at 100% CPU: it logged a 200 KB exception
through structlog's *default* processor chain, whose cost is superlinear in the
message length (measured 0.43 s at 1 K chars, 2.26 s at 20 K). With logging
configured - how the controller actually runs - the same request is 2-3 ms and
returns the 92-byte typed envelope, so B1 was never at fault.

The Lead first skipped that test with a note suggesting "call
``configure_logging()`` and drop the size to a few KB". That fix is
*incomplete*, which is why the test is now deleted rather than re-enabled: its
``boom`` fixture builds its transport with the default
``httpx.ASGITransport(app=...)``, i.e. ``raise_app_exceptions=True``. Starlette's
``ServerErrorMiddleware`` (see ``errors.py``'s own docstring on where the
``Exception`` handler lives) always **re-raises the original exception after
sending the handler's response**, specifically so a real ASGI server can log it;
under the default transport that re-raise surfaces to the ``httpx`` caller as the
raised exception itself, not a response - so even with logging fixed and the
message shrunk, ``response = await boom.get(...)`` would still raise
``ValueError`` rather than return. A session following that note literally
would have traded a hang for a confusing failure. The tests below use
``raise_app_exceptions=False`` (the same technique the *implementer's* own
``test_api_g5_fix_round.py`` already uses, correctly) and
``structlog.testing.capture_logs()`` (also already used there) instead of
``configure_logging()`` - it replaces the processor chain for the duration of the
``with`` block, which turns out to make even the full, unshrunk 200 KB message
finish in under a millisecond, so no size reduction is needed either.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from typing import cast

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
import structlog
from fastapi import FastAPI
from pydantic import BaseModel

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


# --------------------------------------------------------------------------
# B1, the huge exception, done correctly (see the module docstring).
# --------------------------------------------------------------------------


def _boom_app_no_reraise_needed() -> FastAPI:
    """Same shape as ``_boom_app`` above, plus a route whose *response
    construction itself* fails to encode - a real FastAPI/Starlette failure
    mode distinct from "the handler body raised"."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    @app.get("/boom/huge-non-ascii")
    async def _huge_non_ascii() -> dict[str, str]:
        raise ValueError("é" * 200_000 + "\udcff")  # the skipped test's exact payload

    @app.get("/boom/unencodable-response")
    async def _unencodable_response() -> dict[str, str]:
        # A lone surrogate in the *return value*: FastAPI/Starlette's
        # `JSONResponse.render` does `json.dumps(..., ensure_ascii=False).encode
        # ("utf-8")`, which raises `UnicodeEncodeError` while constructing the
        # response object - before any header is sent. If this were e.g. a
        # `hostname` read back from storage via a path that used
        # `errors="surrogateescape"`, this is exactly the shape it would take.
        return {"hostname": "lap\udcfftop"}

    return app


@pytest_asyncio.fixture
async def boom_observable() -> AsyncIterator[httpx.AsyncClient]:
    """``raise_app_exceptions=False``: the correct technique (already used by
    the implementer's own ``test_api_g5_fix_round.py``) to see the *response* a
    real socket client would receive, rather than the re-raise
    ``ServerErrorMiddleware`` performs afterwards purely for server-side
    logging."""
    transport = httpx.ASGITransport(
        app=_boom_app_no_reraise_needed(), raise_app_exceptions=False
    )
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        yield c


@pytest.mark.asyncio
async def test_the_full_200kb_non_ascii_message_yields_the_envelope_fast(
    boom_observable: httpx.AsyncClient,
) -> None:
    """The skipped test's exact payload (200 KB, non-ASCII, plus a lone
    surrogate), unshrunk, with the two things its skip note's suggested fix
    was missing: a transport that surfaces the response instead of re-raising,
    and ``capture_logs()`` instead of ``configure_logging()`` (both patterns
    already present in the implementer's own file for this exact reason)."""
    t0 = time.perf_counter()
    with structlog.testing.capture_logs():
        response = await boom_observable.get("/boom/huge-non-ascii")
    elapsed = time.perf_counter() - t0
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "code": FailureType.INTERNAL_ERROR.value,
            "message": GENERIC_5XX_MESSAGE,
        }
    }
    assert "é" not in response.text
    assert len(response.content) < 1_000
    assert elapsed < 0.1, f"took {elapsed:.3f}s - should be sub-millisecond"


@pytest.mark.asyncio
async def test_a_response_that_cannot_be_utf8_encoded_still_yields_the_envelope(
    boom_observable: httpx.AsyncClient,
) -> None:
    """A real FastAPI/Starlette failure mode the implementer's own B1 tests do
    not cover: the exception happens while *constructing the response*, not in
    the route body, so it is a ``UnicodeEncodeError`` raised by
    ``JSONResponse.render`` rather than anything ``meshforge`` code raised on
    purpose. B1's catch-all must still produce the typed envelope, not the bare
    Starlette default this whole round exists to eliminate."""
    with structlog.testing.capture_logs():
        response = await boom_observable.get("/boom/unencodable-response")
    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == {
        "error": {
            "code": FailureType.INTERNAL_ERROR.value,
            "message": GENERIC_5XX_MESSAGE,
        }
    }


# --------------------------------------------------------------------------
# N4: does `typing.cast` replacing `assert isinstance` remove a real guard?
# --------------------------------------------------------------------------


def test_n4_cast_provides_zero_runtime_protection_if_ever_misrouted() -> None:
    """Not reachable through the public HTTP surface (Starlette's dispatch only
    calls a registered handler for the exception class - or a subclass - it was
    registered against), but the fix round explicitly asks whether removing the
    ``assert isinstance`` removed a real guard. It did, in the narrow
    defense-in-depth sense: calling a handler directly with the wrong exception
    type used to fail loudly and immediately (``AssertionError``, a clear
    programmer error); now it fails later and more confusingly. This test
    exercises that directly, documents the *current* failure shape, and is a
    canary for it getting worse (e.g. a handler than can be reached with the
    wrong type through some future refactor silently returning a bad body
    instead of erroring at all)."""
    import asyncio as _asyncio

    from meshforge.controller.api.errors import install_error_handlers as _install

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    _install(app)
    handlers = dict(app.exception_handlers)
    from meshforge.controller.api.errors import ApiError

    api_error_handler = handlers[ApiError]

    async def _call() -> None:
        # Call the handler exactly as Starlette's dispatch would, but with a
        # deliberately wrong exception type - the scenario N4 asks about.
        await api_error_handler(cast("httpx.Request", object()), TypeError("wrong"))

    with pytest.raises(AttributeError):
        _asyncio.run(_call())


# --------------------------------------------------------------------------
# B1 does not widen to swallow RequestValidationError's own shaping.
# --------------------------------------------------------------------------


class _StrictModel(BaseModel):
    value: int


def _shaping_app() -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    install_error_handlers(app)

    @app.get("/response-model-mismatch", response_model=_StrictModel)
    async def _mismatch() -> dict[str, str]:
        # Independent of the implementer's own `/boom/response-validation`
        # route: a different mechanism (an explicit `response_model`) reaching
        # the same `ResponseValidationError` class, to confirm it is not the
        # route shape rather than the exception class that made B1's catch-all
        # apply.
        return {"value": "not-an-int"}

    return app


@pytest.mark.asyncio
async def test_a_response_model_mismatch_is_the_envelope_not_a_bare_500() -> None:
    transport = httpx.ASGITransport(app=_shaping_app(), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://api.test") as c:
        with structlog.testing.capture_logs():
            response = await c.get("/response-model-mismatch")
    assert response.status_code == 500
    assert response.json() == {
        "error": {
            "code": FailureType.INTERNAL_ERROR.value,
            "message": GENERIC_5XX_MESSAGE,
        }
    }


# --------------------------------------------------------------------------
# Gap found via mutation testing: the earlier `test_max_node_limit_boundary_
# is_a_real_sql_bound_not_a_python_slice` above only checks that MAX_NODE_LIMIT
# is *accepted* - it does not check that one past it is *rejected*. Mutating
# `Query(ge=1, le=MAX_NODE_LIMIT, ...)` to drop the `le=` bound entirely makes
# every test in this file except the one below still pass (verified, then
# reverted - `git diff src/` is clean).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_past_max_node_limit_is_rejected_not_silently_accepted(
    many_nodes: tuple[httpx.AsyncClient, _OrderedRegistry],
) -> None:
    client, registry = many_nodes
    response = await client.get("/v1/nodes", params={"limit": MAX_NODE_LIMIT + 1})
    assert response.status_code == 400
    assert registry.calls == []  # refused before the registry is ever touched
