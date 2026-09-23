"""V0.3 G5 unit tests for the ``/v1`` routers and the error envelope (roadmap 0.3.12).

The real app (``create_http_app``) is mounted on a **hand-built**
:class:`~meshforge.controller.api.context.FabricView` whose registry, session table
and resource tracker are fakes, and driven through ``httpx.ASGITransport``: no
socket, no uvicorn, no database, no gRPC. The routers, the response models, the
bounds validation and the error handlers are the real ones.

What these tests are actually guarding:

* the bounds on every list endpoint (1 / max accepted, 0 / max+1 / non-integer 400);
* the single error envelope ``{"error": {"code", "message"}}`` for 400, 404, 503;
* that a message **never echoes a caller-controlled value** - a peer can put
  megabytes of anything in a path parameter, and a driver message can name the
  database host and user;
* that ``/v1/openapi.json`` is served while ``/docs`` and ``/redoc`` are not.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from mf_api_records import (
    T0,
    live_session,
    node_record,
    ring_of,
    sample,
    session_record,
    snapshot_record,
)

from meshforge.common.errors import FailureType
from meshforge.common.ids import new_node_id
from meshforge.controller.api.routes import (
    MAX_LIVE_LIMIT,
    MAX_SESSION_LIMIT,
    MAX_SNAPSHOT_LIMIT,
    NODE_ID_MAX_LENGTH,
)
from meshforge.controller.http import OPENAPI_URL, create_http_app
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import (
    NodeRecord,
    Registry,
    RegistryError,
    SessionRecord,
    SnapshotRecord,
)
from meshforge.controller.resources import LiveRing, LiveSample, ResourceTracker
from meshforge.controller.session.manager import LiveSession, SessionManager
from meshforge.controller.store.errors import DatabaseUnavailableError

pytestmark = pytest.mark.unit

NODE_A = str(new_node_id())
NODE_B = str(new_node_id())


class FakeRegistry:
    """Just the four read methods the ``/v1`` handlers call."""

    def __init__(
        self,
        nodes: Sequence[NodeRecord] = (),
        *,
        sessions: Sequence[SessionRecord] = (),
        snapshots: Sequence[SnapshotRecord] = (),
        raises: Exception | None = None,
    ) -> None:
        self.nodes = list(nodes)
        self.sessions = list(sessions)
        self.snapshots = list(snapshots)
        self.raises = raises
        self.snapshot_calls: list[dict[str, Any]] = []
        self.session_calls: list[dict[str, Any]] = []

    def _check(self) -> None:
        if self.raises is not None:
            raise self.raises

    async def list_nodes(self) -> list[NodeRecord]:
        self._check()
        return list(self.nodes)

    async def get_node(self, node_id: str) -> NodeRecord | None:
        self._check()
        return next((n for n in self.nodes if n.node_id == node_id), None)

    async def get_session(self, session_id: str) -> SessionRecord | None:
        self._check()
        return next((s for s in self.sessions if s.session_id == session_id), None)

    async def list_snapshots(
        self,
        node_id: str,
        *,
        since: dt.datetime | None = None,
        until: dt.datetime | None = None,
        limit: int | None = None,
    ) -> list[SnapshotRecord]:
        self._check()
        self.snapshot_calls.append(
            {"node_id": node_id, "since": since, "until": until, "limit": limit}
        )
        return [s for s in self.snapshots if s.node_id == node_id]

    async def list_sessions(
        self, node_id: str | None = None, *, limit: int | None = None
    ) -> list[SessionRecord]:
        self._check()
        self.session_calls.append({"node_id": node_id, "limit": limit})
        rows = [s for s in self.sessions if node_id is None or s.node_id == node_id]
        return rows[-limit:] if limit is not None else rows


class FakeManager:
    def __init__(self, live: dict[str, LiveSession] | None = None) -> None:
        self._live = live or {}

    def get(self, node_id: str) -> LiveSession | None:
        return self._live.get(node_id)

    def __len__(self) -> int:
        return len(self._live)


class FakeTracker:
    def __init__(self, rings: dict[str, LiveRing] | None = None) -> None:
        self._rings = rings or {}

    def ring(self, node_id: str) -> LiveRing | None:
        return self._rings.get(node_id)

    def latest(self, node_id: str) -> LiveSample | None:
        ring = self._rings.get(node_id)
        return None if ring is None else ring.latest

    def nodes(self) -> list[str]:
        return list(self._rings)


def build_app(
    registry: FakeRegistry,
    *,
    manager: FakeManager | None = None,
    tracker: FakeTracker | None = None,
    now: float = 1_000.0,
) -> Any:
    """The real app with fake collaborators and a frozen monotonic clock."""
    return create_http_app(
        cast("SessionManager", manager or FakeManager()),
        registry=cast("Registry", registry),
        resources=cast("ResourceTracker", tracker or FakeTracker()),
        clock=lambda: now,
    )


def client_for(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://api.test"
    )


@pytest_asyncio.fixture
async def populated() -> AsyncIterator[tuple[httpx.AsyncClient, FakeRegistry]]:
    online = node_record(node_id=NODE_A, state=NodeState.ONLINE, last_session_id="s-a")
    offline = node_record(node_id=NODE_B, state=NodeState.OFFLINE, hardware={})
    session = session_record(NODE_A, session_id="s-a")
    registry = FakeRegistry([online, offline], sessions=[session])
    manager = FakeManager({NODE_A: live_session(session_id="s-a")})
    tracker = FakeTracker({NODE_A: ring_of(sample(received_at=995.0))})
    app = build_app(registry, manager=manager, tracker=tracker, now=1_000.0)
    async with client_for(app) as client:
        yield client, registry


def envelope(response: httpx.Response) -> dict[str, str]:
    body = response.json()
    assert set(body) == {"error"}, body
    error = body["error"]
    assert set(error) == {"code", "message"}, error
    assert isinstance(error["code"], str)
    assert isinstance(error["message"], str)
    return cast("dict[str, str]", error)


# -------------------------------------------------------------- GET /v1/nodes


@pytest.mark.asyncio
async def test_list_nodes_shapes_every_row_and_counts_them(
    populated: tuple[httpx.AsyncClient, FakeRegistry],
) -> None:
    client, _ = populated
    response = await client.get("/v1/nodes")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    by_id = {node["node_id"]: node for node in body["nodes"]}
    assert by_id[NODE_A]["connected"] is True
    assert by_id[NODE_A]["state"] == NodeState.ONLINE.value
    assert by_id[NODE_A]["sample_age_s"] == 5.0
    assert by_id[NODE_B]["connected"] is False
    assert by_id[NODE_B]["sample_age_s"] is None
    assert by_id[NODE_B]["gpus"] == []


@pytest.mark.asyncio
async def test_a_node_online_in_the_database_with_no_live_stream_is_not_connected() -> (
    None
):
    """D16 over HTTP: the controller-restart case, where ``state`` and ``connected``
    must disagree. The database still says ONLINE because the process that wrote it
    is gone; this process holds no stream and has never seen a sample."""
    registry = FakeRegistry([node_record(node_id=NODE_A, state=NodeState.ONLINE)])
    async with client_for(build_app(registry)) as client:
        row = (await client.get("/v1/nodes")).json()["nodes"][0]
        detail = (await client.get(f"/v1/nodes/{NODE_A}")).json()
    for body in (row, detail):
        assert body["state"] == NodeState.ONLINE.value
        assert body["connected"] is False
        assert body["sample_age_s"] is None
        assert body["rtt_ms"] is None


@pytest.mark.asyncio
async def test_list_nodes_on_an_empty_fabric_is_an_empty_list_not_an_error() -> None:
    async with client_for(build_app(FakeRegistry())) as client:
        response = await client.get("/v1/nodes")
    assert response.status_code == 200
    assert response.json() == {"nodes": [], "count": 0}


# --------------------------------------------------------- GET /v1/nodes/{id}


@pytest.mark.asyncio
async def test_node_detail_returns_the_stored_hardware_and_session_fields(
    populated: tuple[httpx.AsyncClient, FakeRegistry],
) -> None:
    client, _ = populated
    body = (await client.get(f"/v1/nodes/{NODE_A}")).json()
    assert body["hardware"] == {"cpu_threads": 8}
    assert body["agent_version"] == "0.1.0.dev0"
    assert body["protocol_version"] == 1
    assert body["session_epoch"] == 3
    assert body["last_session_id"] == "s-a"


@pytest.mark.asyncio
async def test_unknown_node_is_a_404_envelope(
    populated: tuple[httpx.AsyncClient, FakeRegistry],
) -> None:
    client, _ = populated
    unknown = str(new_node_id())
    response = await client.get(f"/v1/nodes/{unknown}")
    assert response.status_code == 404
    error = envelope(response)
    assert error["code"] == FailureType.NOT_FOUND.value
    assert error["message"] == "no such node"
    assert unknown not in response.text


@pytest.mark.asyncio
async def test_a_non_canonical_node_id_is_a_400_that_never_echoes_the_value(
    populated: tuple[httpx.AsyncClient, FakeRegistry],
) -> None:
    """The guard: a path parameter is peer-controlled and must not be reflected."""
    client, _ = populated
    hostile = "node_NOT-A-ULID-<script>"
    response = await client.get(f"/v1/nodes/{hostile}")
    assert response.status_code == 400
    error = envelope(response)
    assert error["code"] == FailureType.INVALID_INPUT.value
    assert hostile not in response.text
    assert "script" not in response.text.lower()
    assert "NOT-A-ULID" not in response.text


@pytest.mark.asyncio
async def test_an_over_long_node_id_is_rejected_before_parsing_and_not_echoed(
    populated: tuple[httpx.AsyncClient, FakeRegistry],
) -> None:
    client, _ = populated
    huge = "n" * (NODE_ID_MAX_LENGTH * 4)
    response = await client.get(f"/v1/nodes/{huge}")
    assert response.status_code == 400
    assert envelope(response)["code"] == FailureType.INVALID_INPUT.value
    assert huge not in response.text
    assert "nnnn" not in response.text


# ------------------------------------------------ GET /v1/nodes/{id}/resources


@pytest.mark.asyncio
async def test_resources_returns_history_and_the_newest_live_samples() -> None:
    snapshots = [snapshot_record(NODE_A)]
    registry = FakeRegistry([node_record(node_id=NODE_A)], snapshots=snapshots)
    tracker = FakeTracker(
        {
            NODE_A: ring_of(
                *(sample(sequence=n, received_at=900.0 + n) for n in range(1, 6))
            )
        }
    )
    app = build_app(registry, tracker=tracker, now=1_000.0)
    async with client_for(app) as client:
        body = (await client.get(f"/v1/nodes/{NODE_A}/resources?live_limit=2")).json()
    assert body["history_count"] == 1
    assert body["live_count"] == 2
    # The *newest* two, oldest first.
    assert [s["sequence"] for s in body["live"]] == [4, 5]
    assert registry.snapshot_calls[0]["limit"] == 240


@pytest.mark.asyncio
async def test_live_limit_zero_returns_history_only() -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    tracker = FakeTracker({NODE_A: ring_of(sample())})
    async with client_for(build_app(registry, tracker=tracker)) as client:
        body = (await client.get(f"/v1/nodes/{NODE_A}/resources?live_limit=0")).json()
    assert body["live"] == []
    assert body["live_count"] == 0


@pytest.mark.asyncio
async def test_resources_for_a_node_with_no_ring_is_empty_not_an_error() -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    async with client_for(build_app(registry)) as client:
        body = (await client.get(f"/v1/nodes/{NODE_A}/resources")).json()
    assert body == {
        "node_id": NODE_A,
        "history": [],
        "history_count": 0,
        "live": [],
        "live_count": 0,
    }


@pytest.mark.asyncio
async def test_resources_of_an_unknown_node_is_a_404_envelope() -> None:
    async with client_for(build_app(FakeRegistry())) as client:
        response = await client.get(f"/v1/nodes/{NODE_A}/resources")
    assert response.status_code == 404
    assert envelope(response)["code"] == FailureType.NOT_FOUND.value


@pytest.mark.asyncio
async def test_since_and_until_are_passed_through_as_utc() -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    async with client_for(build_app(registry)) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources",
            params={"since": "2026-01-02T03:04:05", "until": "2026-01-02T04:00:00Z"},
        )
    assert response.status_code == 200
    call = registry.snapshot_calls[0]
    assert call["since"] == T0
    assert call["until"] == dt.datetime(2026, 1, 2, 4, 0, 0, tzinfo=dt.UTC)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("since", "until"),
    [
        ("2026-01-02T04:00:00Z", "2026-01-02T03:00:00Z"),  # until before since
        ("2026-01-02T04:00:00Z", "2026-01-02T04:00:00Z"),  # equal: empty window
    ],
)
async def test_until_not_after_since_is_a_400_envelope(since: str, until: str) -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    async with client_for(build_app(registry)) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"since": since, "until": until}
        )
    assert response.status_code == 400
    error = envelope(response)
    assert error["code"] == FailureType.INVALID_INPUT.value
    assert "until must be after since" in error["message"]
    # Rejected before the registry is touched at all.
    assert registry.snapshot_calls == []


@pytest.mark.asyncio
async def test_an_unparseable_timestamp_is_a_400_envelope_not_a_500() -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    async with client_for(build_app(registry)) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"since": "yesterday"}
        )
    assert response.status_code == 400
    assert envelope(response)["code"] == FailureType.INVALID_INPUT.value


# ------------------------------------------------------------------- bounds


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, MAX_SNAPSHOT_LIMIT])
async def test_history_limit_boundaries_are_accepted(limit: int) -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    async with client_for(build_app(registry)) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"limit": limit}
        )
    assert response.status_code == 200
    assert registry.snapshot_calls[0]["limit"] == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1, MAX_SNAPSHOT_LIMIT + 1, "many", "1.5", ""])
async def test_a_history_limit_outside_the_range_is_a_400_envelope(
    limit: object,
) -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    async with client_for(build_app(registry)) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"limit": str(limit)}
        )
    assert response.status_code == 400
    error = envelope(response)
    assert error["code"] == FailureType.INVALID_INPUT.value
    assert "limit" in error["message"]
    assert registry.snapshot_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("live_limit", [0, MAX_LIVE_LIMIT])
async def test_live_limit_boundaries_are_accepted(live_limit: int) -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    async with client_for(build_app(registry)) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"live_limit": live_limit}
        )
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("live_limit", [-1, MAX_LIVE_LIMIT + 1, "all"])
async def test_a_live_limit_outside_the_range_is_a_400_envelope(
    live_limit: object,
) -> None:
    registry = FakeRegistry([node_record(node_id=NODE_A)])
    async with client_for(build_app(registry)) as client:
        response = await client.get(
            f"/v1/nodes/{NODE_A}/resources", params={"live_limit": str(live_limit)}
        )
    assert response.status_code == 400
    assert envelope(response)["code"] == FailureType.INVALID_INPUT.value


# ----------------------------------------------------------- GET /v1/sessions


@pytest.mark.asyncio
async def test_sessions_are_bounded_by_default_and_marked_connected(
    populated: tuple[httpx.AsyncClient, FakeRegistry],
) -> None:
    client, registry = populated
    body = (await client.get("/v1/sessions")).json()
    assert body["count"] == 1
    assert body["sessions"][0]["connected"] is True
    assert registry.session_calls[0] == {"node_id": None, "limit": 50}


@pytest.mark.asyncio
async def test_sessions_can_be_filtered_by_node(
    populated: tuple[httpx.AsyncClient, FakeRegistry],
) -> None:
    client, registry = populated
    response = await client.get("/v1/sessions", params={"node_id": NODE_B})
    assert response.status_code == 200
    assert response.json() == {"sessions": [], "count": 0}
    assert registry.session_calls[0]["node_id"] == NODE_B


@pytest.mark.asyncio
async def test_a_bad_session_filter_is_a_400_that_does_not_echo_it(
    populated: tuple[httpx.AsyncClient, FakeRegistry],
) -> None:
    client, registry = populated
    response = await client.get("/v1/sessions", params={"node_id": "not-a-node"})
    assert response.status_code == 400
    assert "not-a-node" not in response.text
    assert registry.session_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, MAX_SESSION_LIMIT])
async def test_session_limit_boundaries_are_accepted(
    populated: tuple[httpx.AsyncClient, FakeRegistry], limit: int
) -> None:
    client, registry = populated
    assert (
        await client.get("/v1/sessions", params={"limit": limit})
    ).status_code == 200
    assert registry.session_calls[0]["limit"] == limit


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, MAX_SESSION_LIMIT + 1, "lots"])
async def test_a_session_limit_outside_the_range_is_a_400_envelope(
    populated: tuple[httpx.AsyncClient, FakeRegistry], limit: object
) -> None:
    client, registry = populated
    response = await client.get("/v1/sessions", params={"limit": str(limit)})
    assert response.status_code == 400
    assert envelope(response)["code"] == FailureType.INVALID_INPUT.value
    assert registry.session_calls == []


# ------------------------------------------------------- database unavailable


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/v1/nodes", "/v1/sessions"])
async def test_a_registry_error_is_a_503_that_leaks_no_driver_text(path: str) -> None:
    """The driver message can name the database host, user and password."""
    secret = "connection to server at 10.1.2.3 failed: password authentication failed"
    registry = FakeRegistry(
        raises=RegistryError(secret, details={"operation": "list_nodes"})
    )
    async with client_for(build_app(registry)) as client:
        response = await client.get(path)
    assert response.status_code == 503
    error = envelope(response)
    assert error["code"] == FailureType.NETWORK_ERROR.value
    assert error["message"] == "the controller's database is unavailable; retry shortly"
    for leak in ("10.1.2.3", "password", "authentication", "server at"):
        assert leak not in response.text


@pytest.mark.asyncio
async def test_a_database_unavailable_error_is_also_a_503_envelope() -> None:
    registry = FakeRegistry(
        raises=DatabaseUnavailableError("pool exhausted at db:5432")
    )
    async with client_for(build_app(registry)) as client:
        response = await client.get("/v1/nodes")
    assert response.status_code == 503
    assert envelope(response)["code"] == FailureType.NETWORK_ERROR.value
    assert "5432" not in response.text


# -------------------------------------------------------- health and schema


@pytest.mark.asyncio
async def test_healthz_still_reports_the_live_session_count() -> None:
    manager = FakeManager({NODE_A: live_session(session_id="s-a")})
    async with client_for(build_app(FakeRegistry(), manager=manager)) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "sessions": 1}


@pytest.mark.asyncio
async def test_the_openapi_schema_is_served_and_covers_every_v1_path() -> None:
    async with client_for(build_app(FakeRegistry())) as client:
        response = await client.get(OPENAPI_URL)
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert {
        "/v1/nodes",
        "/v1/nodes/{node_id}",
        "/v1/nodes/{node_id}/resources",
        "/v1/sessions",
        "/healthz",
    } <= set(paths)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_the_interactive_docs_pages_are_not_served(path: str) -> None:
    """They fetch assets from a public CDN an isolated lab LAN cannot reach."""
    async with client_for(build_app(FakeRegistry())) as client:
        response = await client.get(path)
    assert response.status_code == 404
    assert envelope(response)["code"] == FailureType.NOT_FOUND.value


@pytest.mark.asyncio
async def test_an_unknown_path_and_a_wrong_method_use_the_same_envelope() -> None:
    async with client_for(build_app(FakeRegistry())) as client:
        missing = await client.get("/v1/nope")
        posted = await client.post("/v1/nodes", json={})
    assert missing.status_code == 404
    assert envelope(missing)["code"] == FailureType.NOT_FOUND.value
    # D14: nothing mutates, so a write verb is refused by the router itself.
    assert posted.status_code == 405
    assert envelope(posted)["code"] == FailureType.INVALID_INPUT.value
