"""Response models of the read-only ``/v1`` surface (roadmap 0.3.12).

**The three liveness notions (brief §5e D16) are three fields and never collapse
into one "online" boolean**, because they legitimately disagree:

``state``
    the persisted :class:`~meshforge.controller.liveness.NodeState` (ARCHITECTURE
    §12.3). Authoritative for *policy* - what the planner and the operator should
    believe about a node. Since V0.3 G6 (D19) it follows how the stream ended: a
    clean end (the agent half-closed, or the controller shut down) is ``offline``
    at once; an abrupt one (dropped connection, cancelled call) is ``suspect``,
    becoming ``offline`` at the normal lease edge unless the agent reconnects
    first. A controller start-up marks every leftover live state ``offline``. It
    is written asynchronously (liveness is decided in memory), so it can trail
    the decision by the database's write latency - longer while PostgreSQL is
    unreachable (``registry.write_failed`` in the log).
``connected``
    does the running :class:`~meshforge.controller.session.manager.SessionManager`
    hold a live stream for this node **right now**? In memory only; authoritative
    for "can the controller send this node a command this second".
``sample_age_s``
    seconds since the newest live resource sample arrived, measured on the
    controller's **monotonic** clock (``LiveSample.received_at``), never on a wall
    clock - wall clocks skew between laptops and jump. Authoritative for "how fresh
    are the numbers below". ``null`` means no sample has ever arrived.

They still disagree legitimately: ``suspect`` with ``connected`` false is a node
whose stream dropped and whose lease is running out (it may yet reconnect);
``online`` with ``connected`` false only lasts until the queued write lands.

**Hardware scores (roadmap 0.3.4, D38)** are ``scores`` on every node, with a
``status`` that is never inferred from a number:

``measured``
    the node's newest validated score run is stored (``run_id``, ``measured_at``,
    ``conditions``, one entry per metric). A metric is either ``ok`` with a
    ``mean`` or ``unavailable``/``failed`` with a ``reason`` and ``mean: null`` -
    never a ``0.0`` standing in for "unknown".
``pending``
    nothing is stored for the hardware the node's latest ``Hello`` described,
    and the node scores itself (it sent the ``hardware-scores`` capability): a
    run is expected after its first ONLINE.
``none``
    nothing is stored and the node does not score (scoring disabled, hardware
    discovery failed, or an agent older than 0.3.4).

Timestamps are ISO-8601 with an explicit UTC offset. Field order is the declaration
order below and is part of the contract (a diff of the JSON should stay readable).
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from meshforge.controller.liveness import NodeState

__all__ = [
    "DiskUsage",
    "GpuUsage",
    "NodeDetail",
    "NodeList",
    "NodeResources",
    "NodeScores",
    "NodeSummary",
    "ResourceSample",
    "ResourceSnapshot",
    "ScoreConditions",
    "ScoreMetric",
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


class ScoreMetric(ApiModel):
    """One metric of a score run (D31). ``mean``/``stdev`` are null unless ``ok``."""

    name: str
    gpu_index: int | None = Field(
        default=None,
        description="NVML index (= hardware.gpus[].index) for GPU metrics.",
    )
    status: str = Field(description="ok | unavailable | failed")
    unit: str | None = None
    mean: float | None = None
    stdev: float | None = None
    n: int = 0
    backend: str | None = None
    backend_version: str | None = None
    threads_effective: int | None = None
    lower_bound: bool = False
    throttling_suspected: bool | None = None
    reason: str | None = Field(
        default=None, description="Why the metric is not ok (snake_case code)."
    )
    detail: str = ""


class ScoreConditions(ApiModel):
    """How the run was measured (D36): recorded, never used to refuse."""

    on_battery: bool | None = None
    cpu_load_percent_before: float | None = None
    background_load_high: bool | None = None
    priority_lowered: bool = False
    budget_s: float
    duration_s: float
    budget_exhausted: bool = False


class NodeScores(ApiModel):
    """``scores`` on a node. See the module docstring for the three statuses."""

    status: Literal["measured", "pending", "none"]
    run_id: str | None = None
    measured_at: Utc | None = Field(
        default=None, description="The agent's wall clock when the run finished."
    )
    score_version: int | None = None
    conditions: ScoreConditions | None = None
    metrics: tuple[ScoreMetric, ...] = ()


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
    scores: NodeScores = Field(default_factory=lambda: NodeScores(status="none"))


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
