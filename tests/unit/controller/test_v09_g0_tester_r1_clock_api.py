"""V0.9 G0 tester round 1: ``GET /v1/clock`` and where ``recv_at`` /
``received_wall`` could leak into JSON (events, REST, JSON columns, OpenAPI)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import cast

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from mf_users import ADMIN_TOKEN, StaticUsers, bearer, static_auth

from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.api import routes
from meshforge.controller.events.watcher import metrics_frame
from meshforge.controller.http import create_http_app
from meshforge.controller.resources import ResourceTracker, live_sample
from meshforge.controller.session.manager import SessionManager
from meshforge.controller.store.errors import DatabaseUnavailableError

pytestmark = pytest.mark.unit


class _M:
    def get(self, node_id: str) -> None:
        return None

    def __len__(self) -> int:
        return 0


class _T:
    def ring(self, node_id: str) -> None:
        return None

    def latest(self, node_id: str) -> None:
        return None

    def nodes(self) -> list[str]:
        return []


class _R:
    async def list_nodes(self, **_: object) -> list:  # type: ignore[type-arg]
        return []


def _app(users: StaticUsers | None = None):  # type: ignore[no-untyped-def]
    return create_http_app(
        cast("SessionManager", _M()),
        auth=static_auth(users),
        registry=cast("object", _R()),  # type: ignore[arg-type]
        resources=cast("ResourceTracker", _T()),
        clock=lambda: 1_000.0,
    )


def _client(headers: dict[str, str] | None = None, users: StaticUsers | None = None):  # type: ignore[no-untyped-def]
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_app(users)),
        base_url="http://api.test",
        headers=headers or {},
    )


@pytest.mark.asyncio
async def test_clock_without_credentials_is_401_and_leaks_no_time() -> None:
    async with _client() as c:
        r = await c.get("/v1/clock")
    assert r.status_code == 401
    assert "server_time" not in r.text
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        {"authorization": "Bearer nope"},
        {"authorization": "Bearer "},
        {"authorization": "Basic Zm9vOmJhcg=="},
        {"authorization": "Bearer " + "a" * 100_000},
        {"cookie": "mf_session=mfbs_garbage"},
        {"cookie": "mf_session=" + ADMIN_TOKEN},  # an API token in the cookie
    ],
)
async def test_clock_with_a_bad_credential_is_401(headers: dict[str, str]) -> None:
    async with _client(headers) as c:
        r = await c.get("/v1/clock")
    assert r.status_code == 401, r.text
    assert "server_time" not in r.text


@pytest.mark.asyncio
async def test_clock_a_bad_header_is_not_rescued_by_a_cookie() -> None:
    async with _client(
        {"authorization": "Bearer nope", "cookie": "mf_session=mfbs_x"}
    ) as c:
        assert (await c.get("/v1/clock")).status_code == 401


@pytest.mark.asyncio
async def test_clock_a_token_that_stops_authenticating_stops_working() -> None:
    users = StaticUsers()
    async with _client(bearer(ADMIN_TOKEN), users) as c:
        assert (await c.get("/v1/clock")).status_code == 200
        users.principals.pop(ADMIN_TOKEN)  # revoked
        r = await c.get("/v1/clock")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_clock_a_user_directory_failure_is_503_never_a_time() -> None:
    users = StaticUsers()
    users.fail = DatabaseUnavailableError("db down")
    async with _client(bearer(ADMIN_TOKEN), users) as c:
        r = await c.get("/v1/clock")
    assert r.status_code == 503 and "server_time" not in r.text


@pytest.mark.asyncio
async def test_clock_shape_and_get_needs_no_csrf_header() -> None:
    async with _client(bearer(ADMIN_TOKEN)) as c:
        r = await c.get("/v1/clock?x=1&x=2&junk=%00")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"server_time"}
    assert isinstance(body["server_time"], float)
    assert abs(body["server_time"] - dt.datetime.now(dt.UTC).timestamp()) < 5


@pytest.mark.asyncio
async def test_clock_is_stamped_per_request_not_cached(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ticks = iter([1000.0, 1001.5, 1003.25])
    monkeypatch.setattr(routes, "wall_clock", lambda: next(ticks))
    async with _client(bearer(ADMIN_TOKEN)) as c:
        got = [(await c.get("/v1/clock")).json()["server_time"] for _ in range(3)]
    assert got == [1000.0, 1001.5, 1003.25]


@pytest.mark.asyncio
async def test_clock_response_is_not_cacheable_by_a_shared_cache() -> None:
    """A cached /v1/clock answer silently ruins an offset estimate; the
    response should say no-store (the page's fetch asks for it itself, a
    second consumer such as G1's per-node probe or the CLI may not)."""
    async with _client(bearer(ADMIN_TOKEN)) as c:
        r = await c.get("/v1/clock")
    cc = r.headers.get("cache-control", "").lower()
    # DEFECT-6 fixed in G0 fix round 1 (dynamic xfail removed).
    assert "no-store" in cc, cc


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
async def test_clock_mutating_verbs_are_refused(method: str) -> None:
    async with _client(bearer(ADMIN_TOKEN)) as c:
        r = await c.request(method, "/v1/clock")
    assert r.status_code in {403, 405}


@pytest.mark.asyncio
async def test_a_nan_clock_source_is_not_served_as_a_time(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(routes, "wall_clock", lambda: float("nan"))
    async with _client(bearer(ADMIN_TOKEN)) as c:
        try:
            r = await c.get("/v1/clock")
        except Exception:  # noqa: BLE001 - the ASGI transport re-raises a handler crash
            return
    assert r.status_code >= 500 or "NaN" not in r.text


@pytest.mark.asyncio
async def test_openapi_documents_clock_with_security_and_recv_at_optional() -> None:
    spec = _app().openapi()
    assert "/v1/clock" in spec["paths"]
    assert set(spec["paths"]["/v1/clock"]) == {"get"}
    schemas = spec["components"]["schemas"]
    assert schemas["ServerClock"]["required"] == ["server_time"]
    point = schemas["MetricPoint"]
    assert "recv_at" in point["properties"]
    assert "recv_at" not in point.get("required", [])  # old clients/records stay valid


# ------------------------------------------------ JSON / JSONB leak probes


def _sample(wall: dt.datetime | None = None, observed: dt.datetime | None = None):  # type: ignore[no-untyped-def]
    now = wall or dt.datetime(2026, 10, 17, 12, 0, 0, 123_456, tzinfo=dt.UTC)
    update = agent_pb2.ResourceUpdate(sequence=3, cpu_percent=1.0)
    update.observed_at.FromDatetime(observed or now)
    return live_sample(update, session_epoch=2, received_at=5.0, wall_clock=lambda: now)


def test_the_node_metrics_payload_is_plain_json() -> None:
    frame = metrics_frame("nod_A", _sample())
    text = json.dumps(frame, allow_nan=False)  # strict: no NaN, no datetime
    assert isinstance(json.loads(text)["recv_at"], float)


def test_recv_at_is_epoch_seconds_of_the_arrival_with_ms_resolution() -> None:
    wall = dt.datetime(2026, 10, 17, 12, 0, 0, 123_456, tzinfo=dt.UTC)
    assert metrics_frame("n", _sample(wall))["recv_at"] == 1792238400.123


def test_the_default_wall_clock_is_timezone_aware_utc() -> None:
    update = agent_pb2.ResourceUpdate(sequence=1)
    s = live_sample(update, session_epoch=1, received_at=1.0)
    assert s.received_wall is not None and s.received_wall.utcoffset() == dt.timedelta(
        0
    )
    assert abs(s.received_wall.timestamp() - dt.datetime.now(dt.UTC).timestamp()) < 5


def test_a_sample_built_without_received_wall_still_makes_a_valid_frame() -> None:
    s = dataclasses.replace(_sample(), received_wall=None)
    frame = metrics_frame("n", s)
    assert frame["recv_at"] is None
    json.dumps(frame, allow_nan=False)


def test_the_only_asdict_consumers_of_live_sample_drop_the_datetime() -> None:
    """Every JSON/JSONB writer that serialises a LiveSample field by field must
    cope with ``received_wall``: the start snapshot is the only one in src/."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src" / "meshforge"
    users = [
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "dataclasses.asdict(sample" in p.read_text(encoding="utf-8")
        or "asdict(sample" in p.read_text(encoding="utf-8")
    ]
    assert users == ["controller/jobs/spec.py"], users


def test_received_wall_is_confined_to_the_live_event_path() -> None:
    """The planner's decision snapshot (and its digest) is built from the
    registry/tracker fields, never from ``received_wall``: a wall-clock stamp
    there would make ``snapshot_digest`` non-deterministic across replays."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[3] / "src" / "meshforge"
    hits = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.py")
        if "received_wall" in p.read_text(encoding="utf-8")
    )
    assert hits == [
        "controller/events/watcher.py",
        "controller/jobs/spec.py",
        "controller/resources.py",
    ], hits
