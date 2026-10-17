"""The ``/v1`` routers (roadmap 0.3.12).

**Trust boundary (brief §5e D14).** Every endpoint here is **read-only** and, in
V0.3, **unauthenticated**. Nothing mutates a node, a session or the database: there
is no delete, no drain, no command. That is only safe because the controller binds
to loopback or one LAN address by default and refuses a wildcard bind without
``[controller] allow_any_bind`` - do not expose this port to an untrusted network.
Authentication, node identity and authorization arrive in V0.7 (ADR-0008); until
then, anyone who can reach port 47110 can read the fabric's inventory (hostnames,
hardware, addresses, utilisation). ``docs/OPERATIONS.md`` says the same to operators.

Bounds: every list endpoint has a documented default and maximum ``limit``, and a
value outside the range is a 400 with the typed envelope, never a full table scan.
Reads of the registry run one statement each and never hold a session across an
await they do not need.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Path, Query, Response

from meshforge.common.errors import InvalidIdError
from meshforge.common.ids import validate_node_id
from meshforge.controller.api.context import FabricView, get_fabric
from meshforge.controller.api.errors import ApiError
from meshforge.controller.api.models import (
    NodeDetail,
    NodeList,
    NodeResources,
    NodeSummary,
    SessionList,
)
from meshforge.controller.api.response_models import (
    MAX_METRICS_WINDOW_S,
    MIN_METRICS_WINDOW_S,
    MetricPoint,
    NodeMetrics,
    ServerClock,
)
from meshforge.controller.api.shape import (
    node_detail,
    node_summary,
    resource_sample,
    resource_snapshot,
    session_summary,
)
from meshforge.controller.events.watcher import metrics_frame

__all__ = [
    "DEFAULT_LIVE_LIMIT",
    "DEFAULT_NODE_LIMIT",
    "DEFAULT_SESSION_LIMIT",
    "DEFAULT_SNAPSHOT_LIMIT",
    "MAX_LIVE_LIMIT",
    "MAX_NODE_LIMIT",
    "MAX_SESSION_LIMIT",
    "MAX_SNAPSHOT_LIMIT",
    "NODE_ID_MAX_LENGTH",
    "router",
]

DEFAULT_SNAPSHOT_LIMIT: Final = 240
"""Four hours of 1-minute buckets - a sensible default window for one node."""
MAX_SNAPSHOT_LIMIT: Final = 10_080
"""One week of 1-minute buckets; the retention default is 7 days."""
DEFAULT_LIVE_LIMIT: Final = 30
"""Newest live samples returned alongside the history (1 minute at 2 s)."""
MAX_LIVE_LIMIT: Final = 1_000
"""Above the live ring's own capacity, so the ring is the real bound."""
DEFAULT_SESSION_LIMIT: Final = 50
MAX_SESSION_LIMIT: Final = 500
DEFAULT_NODE_LIMIT: Final = 500
"""Far above any plausible fabric (the lab has 4), so the default is never a
surprise truncation - but it is still a bound, not "every row"."""
MAX_NODE_LIMIT: Final = 5_000
NODE_ID_MAX_LENGTH: Final = 32
"""``node_`` + ULID = 31; the path parameter is refused above this before parsing."""

router = APIRouter(prefix="/v1", tags=["fabric"])

Fabric = Annotated[FabricView, Depends(get_fabric)]
NodeId = Annotated[str, Path(max_length=NODE_ID_MAX_LENGTH)]


def _checked_node_id(value: str) -> str:
    """Canonical ``node_<ULID>`` or a 400 that never echoes the caller's value."""
    try:
        return validate_node_id(value)
    except InvalidIdError:
        raise ApiError.invalid("node_id is not a canonical node_<ULID>") from None


def _as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """A naive query timestamp means UTC (the columns are ``timestamptz``)."""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


@router.get("/nodes", response_model=NodeList, summary="Every known node")
async def list_nodes(
    fabric: Fabric,
    limit: Annotated[
        int, Query(ge=1, le=MAX_NODE_LIMIT, description="Nodes returned, by node_id.")
    ] = DEFAULT_NODE_LIMIT,
) -> NodeList:
    """All enrolled nodes with their live state (D16: three separate fields).

    The table holds one row per node that ever enrolled, not one per event, so
    ``limit`` exists to keep the promise that *every* list endpoint is bounded
    rather than to page through a large result. There is deliberately no offset or
    cursor yet (a V0.8 conversation): ``count`` is the number of rows returned, and
    no ``total`` is served because counting every node would mean a second query on
    a path an operator polls with ``--watch``.
    """
    now = fabric.clock()
    records = await fabric.registry.list_nodes(limit=limit)
    nodes: list[NodeSummary] = [
        node_summary(
            record,
            live=fabric.manager.get(record.node_id),
            sample=fabric.resources.latest(record.node_id),
            now=now,
        )
        for record in records
    ]
    return NodeList(nodes=tuple(nodes), count=len(nodes))


@router.get("/nodes/{node_id}", response_model=NodeDetail, summary="One node")
async def get_node(fabric: Fabric, node_id: NodeId) -> NodeDetail:
    checked = _checked_node_id(node_id)
    now = fabric.clock()
    record = await fabric.registry.get_node(checked)
    if record is None:
        raise ApiError.not_found("node")
    session = (
        await fabric.registry.get_session(record.last_session_id)
        if record.last_session_id
        else None
    )
    cache = await fabric.cache.summary(checked) if fabric.cache is not None else None
    return node_detail(
        record,
        live=fabric.manager.get(checked),
        sample=fabric.resources.latest(checked),
        session=session,
        now=now,
        cache=cache,
    )


@router.get(
    "/nodes/{node_id}/resources",
    response_model=NodeResources,
    summary="One node's resource history and newest live samples",
)
async def get_node_resources(
    fabric: Fabric,
    node_id: NodeId,
    since: Annotated[
        dt.datetime | None,
        Query(description="Inclusive lower bound on bucket_start (ISO-8601, UTC)."),
    ] = None,
    until: Annotated[
        dt.datetime | None,
        Query(description="Exclusive upper bound on bucket_start (ISO-8601, UTC)."),
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_SNAPSHOT_LIMIT, description="History rows.")
    ] = DEFAULT_SNAPSHOT_LIMIT,
    live_limit: Annotated[
        int, Query(ge=0, le=MAX_LIVE_LIMIT, description="Newest live samples.")
    ] = DEFAULT_LIVE_LIMIT,
) -> NodeResources:
    """1-minute downsampled history from the database plus the in-memory ring.

    The two differ on purpose: history is complete and durable but at most one
    minute of resolution, the ring is 2-second resolution and only the last few
    minutes of *this* controller process.
    """
    checked = _checked_node_id(node_id)
    start, end = _as_utc(since), _as_utc(until)
    if start is not None and end is not None and end <= start:
        raise ApiError.invalid("until must be after since")
    now = fabric.clock()
    if await fabric.registry.get_node(checked) is None:
        raise ApiError.not_found("node")
    history = await fabric.registry.list_snapshots(
        checked, since=start, until=end, limit=limit
    )
    ring = fabric.resources.ring(checked)
    samples = ring.snapshot()[-live_limit:] if ring is not None and live_limit else []
    return NodeResources(
        node_id=checked,
        history=tuple(resource_snapshot(row) for row in history),
        history_count=len(history),
        live=tuple(resource_sample(sample, now) for sample in samples),
        live_count=len(samples),
    )


@router.get(
    "/nodes/{node_id}/metrics",
    response_model=NodeMetrics,
    summary="One node's live metrics for sparklines (in-memory, last 5 minutes)",
)
async def get_node_metrics(
    fabric: Fabric,
    node_id: NodeId,
    window: Annotated[
        int,
        Query(
            ge=MIN_METRICS_WINDOW_S,
            le=MAX_METRICS_WINDOW_S,
            description="Seconds back from now (the ring holds about 5 minutes).",
        ),
    ] = MAX_METRICS_WINDOW_S,
) -> NodeMetrics:
    """V0.8 G1 (D173): the controller's in-memory live ring of the node's
    heartbeat samples (one per ~2 s, the last ``live_ring_size`` of *this*
    process) - nothing is read from or written to PostgreSQL. Oldest first.
    ``age_s`` is measured on the controller's monotonic clock. Empty after a
    controller restart until the node reports again; V0.9 replaces the source
    (durable telemetry) behind the same shape."""
    checked = _checked_node_id(node_id)
    if await fabric.registry.get_node(checked) is None:
        raise ApiError.not_found("node")
    now = fabric.clock()
    ring = fabric.resources.ring(checked)
    samples = [] if ring is None else ring.snapshot()
    points = tuple(
        MetricPoint(
            **metrics_frame(checked, sample),  # type: ignore[arg-type]
            age_s=round(max(0.0, now - sample.received_at), 3),
        )
        for sample in samples
        if now - sample.received_at <= window
    )
    return NodeMetrics(
        node_id=checked, window_s=window, samples=points, count=len(points)
    )


@router.get(
    "/clock",
    response_model=ServerClock,
    summary="The controller's wall clock (for an NTP-style offset estimate)",
)
async def get_clock(fabric: Fabric, response: Response) -> ServerClock:
    """V0.9 G0 (D213): the controller's ``time.time()`` when it answers, so a
    client can estimate the offset between its clock and the controller's from
    round trips (``meshforge.common.clock_offset``: offset ± RTT/2) before it
    compares a controller timestamp (an event's ``recv_at``, a transition's
    ``at``) with its own clock. Authenticated like every route; reads nothing.
    ``Cache-Control: no-store``: a cached answer would silently ruin the
    estimate (fix round 1, DEFECT-6). The time source is the app's
    ``FabricView.wall_clock`` (V0.9 G1, D226 item 0.4)."""
    response.headers["Cache-Control"] = "no-store"
    return ServerClock(server_time=round(fabric.wall_clock(), 6))


@router.get("/sessions", response_model=SessionList, summary="Recent sessions")
async def list_sessions(
    fabric: Fabric,
    node_id: Annotated[
        str | None, Query(max_length=NODE_ID_MAX_LENGTH, description="Filter by node.")
    ] = None,
    limit: Annotated[
        int, Query(ge=1, le=MAX_SESSION_LIMIT, description="Most recent sessions.")
    ] = DEFAULT_SESSION_LIMIT,
) -> SessionList:
    """The most recent ``limit`` sessions, oldest first.

    One row per accepted stream, so this table grows with every reconnect - hence
    the hard cap rather than "everything".
    """
    checked = _checked_node_id(node_id) if node_id is not None else None
    records = await fabric.registry.list_sessions(checked, limit=limit)
    sessions = tuple(
        session_summary(record, live=fabric.manager.get(record.node_id))
        for record in records
    )
    return SessionList(sessions=sessions, count=len(sessions))
