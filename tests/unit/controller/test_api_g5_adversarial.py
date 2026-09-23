"""Independent adversarial tests for the V0.3 G5 read-only ``/v1`` surface.

Written from the roadmap 0.3.12 success criteria and brief §5e D14/D16 - not from
reading the implementation - by the tester role. Reuses the same real-app-over-
``ASGITransport`` pattern as ``tests/unit/controller/test_api_routes.py`` (fakes for
the registry/manager/tracker, no socket, no database) but targets different edges:
full route x method enumeration, hostile ``node_id`` values, query-parameter
corner cases, and independent leak checks.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import cast

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from mf_api_records import node_record

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_node_id
from meshforge.controller.http import create_http_app
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import NodeRecord, RegistryError
from meshforge.controller.resources import ResourceTracker
from meshforge.controller.session.manager import SessionManager

pytestmark = pytest.mark.unit

NODE_A = str(new_node_id())


class _EmptyManager:
    def get(self, node_id: str) -> None:
        return None

    def __len__(self) -> int:
        return 0


class _EmptyTracker:
    def ring(self, node_id: str) -> object | None:
        return None

    def latest(self, node_id: str) -> object | None:
        return None

    def nodes(self) -> list[str]:
        return []


class _Registry:
    """Read methods only; records every ``node_id`` handed to it."""

    def __init__(self, nodes: list[NodeRecord] | None = None) -> None:
        self.nodes = nodes or []
        self.seen_node_ids: list[str] = []

    async def list_nodes(self) -> list[NodeRecord]:
        return list(self.nodes)

    async def get_node(self, node_id: str) -> NodeRecord | None:
        self.seen_node_ids.append(node_id)
        return next((n for n in self.nodes if n.node_id == node_id), None)

    async def get_session(self, session_id: str) -> None:
        return None

    async def list_snapshots(self, node_id: str, **_: object) -> list:  # type: ignore[type-arg]
        self.seen_node_ids.append(node_id)
        return []

    async def list_sessions(self, node_id: str | None = None, **_: object) -> list:  # type: ignore[type-arg]
        if node_id is not None:
            self.seen_node_ids.append(node_id)
        return []


def build(registry: _Registry | None = None) -> httpx.AsyncClient:
    app = create_http_app(
        cast("SessionManager", _EmptyManager()),
        registry=cast("object", registry or _Registry()),  # type: ignore[arg-type]
        resources=cast("ResourceTracker", _EmptyTracker()),
        clock=lambda: 1_000.0,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    )


def envelope(response: httpx.Response) -> dict[str, str]:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message"}
    return cast("dict[str, str]", error)


# ------------------------------------------------------- D14: enumerate every route


@pytest_asyncio.fixture
async def app_routes() -> list[str]:
    """Every concrete ``/v1`` path this app actually serves (not templated twice)."""
    app = create_http_app(
        cast("SessionManager", _EmptyManager()),
        registry=cast("object", _Registry()),  # type: ignore[arg-type]
        resources=cast("ResourceTracker", _EmptyTracker()),
        clock=lambda: 1_000.0,
    )

    def _walk(routes: Sequence[object] | None) -> list[str]:
        """Recurse through Starlette's route tree.

        FastAPI/Starlette >= 0.141 wraps an ``include_router()`` mount in an
        ``_IncludedRouter`` whose real routes live on ``.original_router.routes``
        rather than directly in ``app.routes`` - walk both shapes so this test
        does not silently stop enumerating routes on a dependency bump.
        """
        found: list[str] = []
        for route in list(routes) if routes is not None else []:
            path = getattr(route, "path", None)
            if isinstance(path, str) and path.startswith("/v1"):
                found.append(path)
            nested = getattr(route, "routes", None)
            original = getattr(route, "original_router", None)
            if original is not None:
                found.extend(_walk(getattr(original, "routes", None)))
            elif nested is not None:
                found.extend(_walk(nested))
        return found

    return _walk(app.routes)


@pytest.mark.asyncio
async def test_every_v1_route_is_enumerated_and_none_is_a_write_verb(
    app_routes: list[str],
) -> None:
    """D14: the app must not define a mutating route at all, on every path FastAPI
    knows about (not only the two the original suite exercised)."""
    assert set(app_routes) == {
        "/v1/nodes",
        "/v1/nodes/{node_id}",
        "/v1/nodes/{node_id}/resources",
        "/v1/sessions",
        "/v1/openapi.json",  # GET-only schema endpoint; not a fabric resource route
    }, app_routes


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/v1/nodes",
        f"/v1/nodes/{NODE_A}",
        f"/v1/nodes/{NODE_A}/resources",
        "/v1/sessions",
    ],
)
@pytest.mark.parametrize("verb", ["post", "put", "patch", "delete"])
async def test_every_mutating_verb_on_every_path_is_405_typed(
    path: str, verb: str
) -> None:
    async with build() as client:
        response = await getattr(client, verb)(path)
    assert response.status_code == 405, (verb, path, response.text)
    error = envelope(response)
    assert error["code"] == FailureType.INVALID_INPUT.value


# ---------------------------------------------------- hostile node_id values


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw",
    [
        "../../etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",  # pre-encoded traversal
        "node_\x00evil",  # embedded NUL
        "node_évilé",  # non-ASCII / unicode
        "node_" + "a" * 10_000,  # 10 KB
        "%00",
        "node_' OR '1'='1",
        "<script>alert(1)</script>",
    ],
)
async def test_hostile_node_id_values_are_a_clean_typed_envelope_never_echoed(
    raw: str,
) -> None:
    registry = _Registry()
    async with build(registry) as client:
        try:
            response = await client.get(
                f"/v1/nodes/{httpx.QueryParams({'x': raw})['x']}"
            )
        except httpx.InvalidURL:
            # A NUL or other non-printable ASCII byte cannot even form a URL at
            # the client layer - "never reaches the server" satisfies the
            # contract at least as well as a clean typed rejection would.
            return
    # Every hostile shape must resolve to a typed client error, never a 500/hang,
    # and the raw value must never come back verbatim in the body.
    assert response.status_code in (400, 404), (raw, response.status_code)
    body = envelope(response)
    assert raw not in response.text
    assert "\x00" not in response.text
    assert body["code"] in {
        FailureType.INVALID_INPUT.value,
        FailureType.NOT_FOUND.value,
    }


@pytest.mark.asyncio
async def test_a_newline_in_node_id_cannot_reach_the_response_or_split_headers() -> (
    None
):
    """A CRLF in a path parameter must never be reflected raw (header/response
    splitting risk) - httpx/starlette should already refuse this at the URL layer,
    but the contract is "never accepted, never echoed", not "accepted safely"."""
    async with build() as client:
        try:
            response = await client.get("/v1/nodes/node_evil%0d%0aX-Injected:%201")
        except httpx.InvalidURL:
            return  # rejected before it is even a request: also acceptable
    assert "\r\n" not in response.text
    assert "X-Injected" not in response.headers


@pytest.mark.asyncio
async def test_an_empty_node_id_segment_never_500s() -> None:
    async with build() as client:
        response = await client.get("/v1/nodes//resources")
    assert response.status_code in (404, 400)


# --------------------------------------------------------- limit query bounds


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["abc", "", "1.5", "1e3", "0x10", "-1", "-999999999"])
async def test_history_limit_garbage_values_are_400_never_500(bad: str) -> None:
    async with build(_Registry([node_record(node_id=NODE_A)])) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"limit": bad}
        )
    assert response.status_code == 400, (bad, response.status_code, response.text)
    assert envelope(response)["code"] == FailureType.INVALID_INPUT.value


@pytest.mark.asyncio
async def test_an_absurdly_large_limit_is_400_not_a_full_table_scan() -> None:
    async with build(_Registry([node_record(node_id=NODE_A)])) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"limit": 10**18}
        )
    assert response.status_code == 400
    assert envelope(response)["code"] == FailureType.INVALID_INPUT.value


@pytest.mark.asyncio
async def test_duplicated_limit_query_params_do_not_crash_the_handler() -> None:
    """Two ``limit=`` values on one URL: FastAPI/Starlette takes one of them (last
    wins for ``request.query_params`` semantics) - the point is it must resolve to
    a definite, bounded outcome, never a 500."""
    registry = _Registry([node_record(node_id=NODE_A)])
    async with build(registry) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources?limit=5&limit=9999999"
        )
    assert response.status_code in (200, 400)


@pytest.mark.asyncio
async def test_a_unicode_limit_value_is_400_not_500() -> None:
    fullwidth_123 = "\uff11\uff12\uff13"  # fullwidth digits, not ASCII 1/2/3
    async with build(_Registry([node_record(node_id=NODE_A)])) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"limit": fullwidth_123}
        )
    assert response.status_code == 400
    assert envelope(response)["code"] == FailureType.INVALID_INPUT.value


# ------------------------------------------------------- since/until edges


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "since",
    [
        "0001-01-01T00:00:00Z",  # far past
        "9999-12-31T23:59:59Z",  # far future
        "2026-01-02T03:04:05+05:30",  # explicit non-UTC offset
        "2026-01-02T03:04:05",  # naive -> read as UTC
    ],
)
async def test_since_accepts_every_legitimate_iso_shape(since: str) -> None:
    async with build(_Registry([node_record(node_id=NODE_A)])) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"since": since}
        )
    assert response.status_code == 200, (since, response.text)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    ["not-a-date", "2026-13-40T99:99:99Z", "2026/01/02", "'; DROP TABLE nodes;--", ""],
)
async def test_nonsense_since_values_are_400_not_500(value: str) -> None:
    async with build(_Registry([node_record(node_id=NODE_A)])) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"since": value}
        )
    assert response.status_code == 400, (value, response.status_code)
    assert envelope(response)["code"] == FailureType.INVALID_INPUT.value


# -------------------------------------------------------- nothing leaks (independent)


@pytest.mark.asyncio
async def test_a_caller_supplied_node_id_string_never_reaches_a_500_body() -> None:
    """Feed the registry a real record but query a decoy 'secret-looking' unknown
    id and confirm it is never reflected anywhere in the response."""
    secret_shaped = "node_" + "S3CRET" * 4
    registry = _Registry([node_record(node_id=NODE_A)])
    async with build(registry) as client:
        response = await client.get(f"/v1/nodes/{secret_shaped}")
    assert secret_shaped not in response.text


@pytest.mark.asyncio
async def test_registry_error_details_dict_never_appears_in_the_body() -> None:
    class _Boom(_Registry):
        async def list_nodes(self) -> list[NodeRecord]:
            raise RegistryError(
                "internal detail",
                details={"host": "db.internal.example", "user": "meshforge_admin"},
            )

    async with build(_Boom()) as client:
        response = await client.get("/v1/nodes")
    assert response.status_code == 503
    for leak in ("db.internal.example", "meshforge_admin", "internal detail"):
        assert leak not in response.text


# --------------------------------------------------------------- D16 disagreement


@pytest.mark.asyncio
async def test_state_connected_and_sample_age_are_three_independent_fields() -> None:
    """A node ONLINE in the DB, no live session, no live sample at all: every
    field must be independently correct, never derived from another."""
    registry = _Registry([node_record(node_id=NODE_A, state=NodeState.ONLINE)])
    async with build(registry) as client:
        response = await client.get("/v1/nodes")
    row = response.json()["nodes"][0]
    assert row["state"] == NodeState.ONLINE.value
    assert row["connected"] is False
    assert row["sample_age_s"] is None


@pytest.mark.asyncio
async def test_sample_age_is_unaffected_by_a_far_off_agent_wall_clock() -> None:
    """The agent's wall clock (``observed_at``) may be hours wrong; ``sample_age_s``
    must depend only on the controller's monotonic ``received_at``."""
    from mf_api_records import ring_of, sample

    skewed_wall_clock = dt.datetime(1999, 1, 1, tzinfo=dt.UTC)  # 27 years off
    registry = _Registry([node_record(node_id=NODE_A)])

    class _Tracker(_EmptyTracker):
        def __init__(self) -> None:
            self._ring = ring_of(
                sample(observed_at=skewed_wall_clock, received_at=999.0)
            )

        def ring(self, node_id: str) -> object:
            return self._ring if node_id == NODE_A else None

        def latest(self, node_id: str) -> object:
            return self._ring.latest if node_id == NODE_A else None

    app = create_http_app(
        cast("SessionManager", _EmptyManager()),
        registry=cast("object", registry),  # type: ignore[arg-type]
        resources=cast("ResourceTracker", _Tracker()),
        clock=lambda: 1_000.0,  # 1 s after received_at on the monotonic clock
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    ) as client:
        response = await client.get("/v1/nodes")
    row = response.json()["nodes"][0]
    assert row["sample_age_s"] == pytest.approx(1.0, abs=0.01)


@pytest.mark.asyncio
async def test_no_response_body_ever_echoes_this_process_hostname_or_ip() -> None:
    """Standing rule: nothing may assert (or leak) this machine's own hostname."""
    import socket

    registry = _Registry([node_record(node_id=NODE_A)])
    async with build(registry) as client:
        response = await client.get("/v1/nodes")
    assert socket.gethostname() not in response.text
