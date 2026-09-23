"""Response models of the read-only ``/v1`` surface (roadmap 0.3.12).

**The three liveness notions (brief §5e D16) are three fields and never collapse
into one "online" boolean**, because they legitimately disagree:

``state``
    the persisted :class:`~meshforge.controller.liveness.NodeState` (ARCHITECTURE
    §12.3). Authoritative for *policy* - what the planner and the operator should
    believe about a node - and survives a controller restart, which is exactly why
    it can say ``ONLINE`` about a node the current process has never heard from.
``connected``
    does the running :class:`~meshforge.controller.session.manager.SessionManager`
    hold a live stream for this node **right now**? In memory only; authoritative
    for "can the controller send this node a command this second".
``sample_age_s``
    seconds since the newest live resource sample arrived, measured on the
    controller's **monotonic** clock (``LiveSample.received_at``), never on a wall
    clock - wall clocks skew between laptops and jump. Authoritative for "how fresh
    are the numbers below". ``null`` means no sample has ever arrived.

Timestamps are ISO-8601 with an explicit UTC offset. Field order is the declaration
order below and is part of the contract (a diff of the JSON should stay readable).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from meshforge.controller.liveness import NodeState

__all__ = [
    "DiskUsage",
    "GpuUsage",
    "NodeDetail",
    "NodeList",
    "NodeResources",
    "NodeSummary",
    "ResourceSample",
    "ResourceSnapshot",
    "SessionList",
    "SessionSummary",
]


def _utc_iso(value: dt.datetime) -> str:
    """Always an explicit offset, always UTC (a naive value is read as UTC)."""
    moment = value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)
    return moment.astimezone(dt.UTC).isoformat()


type Utc = Annotated[dt.datetime, PlainSerializer(_utc_iso, return_type=str)]


class ApiModel(BaseModel):
    """Frozen, no extra fields, declaration order preserved in the JSON."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class GpuUsage(ApiModel):
    index: int
    util_percent: float
    vram_used_bytes: int
    vram_total_bytes: int


class DiskUsage(ApiModel):
    mount: str
    free_bytes: int
    total_bytes: int


class NodeSummary(ApiModel):
    """One row of ``GET /v1/nodes``. See the module docstring for D16."""

    node_id: str
    hostname: str
    os: str
    platform: str
    state: NodeState
    connected: bool
    last_seen_at: Utc
    sample_age_s: float | None = Field(
        default=None,
        description="Seconds since the newest live sample (monotonic); null = none.",
    )
    rtt_ms: float | None = None
    rtt_ewma_ms: float | None = None
    cpu_percent: float | None = None
    ram_used_bytes: int | None = None
    ram_total_bytes: int | None = None
    gpus: tuple[GpuUsage, ...] = ()


class NodeDetail(NodeSummary):
    """``GET /v1/nodes/{node_id}``: the summary plus what only one node needs."""

    first_seen_at: Utc
    session_epoch: int
    last_session_id: str | None = None
    agent_version: str | None = Field(
        default=None,
        description="From the node's most recent session row (live when connected).",
    )
    protocol_version: int | None = None
    peer_address: str | None = None
    hardware: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "The ``Hello.hardware`` summary exactly as the node sent it (proto field "
            "names; 64-bit values are JSON strings). The agent's richer local facts - "
            "per-probe status, duration, reason - are deliberately not on the wire: "
            "run `meshforge-agent hardware show` on that laptop (D13)."
        ),
    )


class NodeList(ApiModel):
    nodes: tuple[NodeSummary, ...]
    count: int


class ResourceSnapshot(ApiModel):
    """One 1-minute downsample bucket from ``resource_snapshot``."""

    bucket_start: Utc
    samples: int
    cpu_percent_mean: float
    cpu_percent_max: float
    ram_used_bytes_mean: int
    ram_used_bytes_max: int
    ram_total_bytes: int
    gpu: dict[str, Any] = Field(default_factory=dict)
    disks: dict[str, Any] = Field(default_factory=dict)


class ResourceSample(ApiModel):
    """One sample still in the controller's in-memory live ring."""

    sequence: int
    session_epoch: int
    observed_at: Utc = Field(description="The agent's wall clock when it sampled.")
    age_s: float = Field(description="Controller monotonic age, like sample_age_s.")
    cpu_percent: float
    ram_used_bytes: int
    ram_total_bytes: int
    gpus: tuple[GpuUsage, ...] = ()
    disks: tuple[DiskUsage, ...] = ()


class NodeResources(ApiModel):
    """``GET /v1/nodes/{node_id}/resources``: history plus the newest live samples."""

    node_id: str
    history: tuple[ResourceSnapshot, ...]
    history_count: int
    live: tuple[ResourceSample, ...]
    live_count: int


class SessionSummary(ApiModel):
    session_id: str
    node_id: str
    session_epoch: int
    started_at: Utc
    ended_at: Utc | None = None
    end_reason: str | None = None
    peer_address: str
    agent_version: str
    protocol_version: int
    last_rtt_ms: float | None = None
    rtt_ewma_ms: float | None = None
    connected: bool = Field(
        description="This exact session is the one the manager holds right now."
    )


class SessionList(ApiModel):
    sessions: tuple[SessionSummary, ...]
    count: int
