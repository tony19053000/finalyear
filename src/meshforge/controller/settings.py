"""Controller settings (``[controller]`` + ``[database]`` sections, roadmap 0.3.6).

Layout follows :mod:`meshforge.common.config`: one TOML file may serve every process
on a machine, so controller-specific keys live in their own ``[controller]`` table
(``MESHFORGE_CONTROLLER__GRPC_BIND=...`` from the environment) and the database
section is shared with the store tooling (``MESHFORGE_DATABASE__*``).

Ports (ARCHITECTURE §23): gRPC ``47100``, REST ``47110`` - an unassigned range,
distinct from the V0.2 spike ports (472xx-473xx). Both default to ``127.0.0.1``:
a controller that should serve other laptops is given that laptop's LAN IP
explicitly; wildcard / public binds need ``allow_any_bind`` (CLAUDE.md §9).

This module imports no controller-extra package so ``meshforge-controller --version``
works on a base install.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator

from meshforge.common.config import MeshForgeSettings, SectionModel
from meshforge.common.fabric_token import CONTROLLER_DIR_NAME, default_token_path
from meshforge.common.netaddr import parse_bind
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.resources import DEFAULT_BUCKET_GRACE_S
from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "BLOBS_DIR_NAME",
    "DEFAULT_GRPC_PORT",
    "DEFAULT_HTTP_PORT",
    "DEFAULT_SERVICE_TYPE",
    "BlobStoreSettings",
    "ControllerSection",
    "ControllerSettings",
    "DiscoverySettings",
    "JobApiSettings",
    "LivenessSettings",
    "ResourceHistorySettings",
]

DEFAULT_GRPC_PORT = 47100
DEFAULT_HTTP_PORT = 47110
DEFAULT_SERVICE_TYPE = "_meshforge-ctl._tcp.local."
"""DNS-SD service type advertised for agents (must match ``[agent.discovery]``)."""
DEFAULT_ADMISSION_TIMEOUT_S = 5.0
"""D41: default Hello admission deadline (below the default 6 s lease)."""
BLOBS_DIR_NAME = "blobs"


def _non_empty_path(value: object) -> object:
    """``""`` would silently mean the current directory (same rule as data_dir)."""
    if isinstance(value, str) and not value.strip():
        raise ValueError("must not be empty (it would mean the current directory)")
    return value


class BlobStoreSettings(SectionModel):
    """``[controller.blobs]`` - the controller's blob store (brief D48, ADR-0003).

    Layout ``<dir>/sha256/<2 hex>/<64 hex>`` plus ``<dir>/tmp`` for uploads in
    flight (swept at start). V0.5 adds chunking, peers and GC on the same layout.
    """

    dir: Path | None = Field(
        default=None,
        description="Blob store root; default <data_dir>/controller/blobs.",
    )
    max_blob_bytes: int = Field(
        default=2 * 1024**3,
        ge=1024 * 1024,
        le=64 * 1024**3,
        description=(
            "Largest blob one PUT may store (bundles are capped at 1 GiB of "
            "uncompressed content by default, outputs likewise). A larger upload "
            "is refused before or while it streams, and nothing is kept."
        ),
    )

    @field_validator("dir", mode="before")
    @classmethod
    def _dir_not_empty(cls, value: object) -> object:
        return _non_empty_path(value)


class JobApiSettings(SectionModel):
    """``[controller.jobs]`` - bounds of the job submission API (brief D50)."""

    max_request_bytes: int = Field(
        default=256 * 1024,
        ge=4 * 1024,
        le=16 * 1024 * 1024,
        description=(
            "Largest POST /v1/jobs body. A manifest is a few hundred bytes; the "
            "project itself travels as a blob, never in this body."
        ),
    )
    fetch_token_capacity: int = Field(
        default=10_000,
        ge=16,
        le=1_000_000,
        description=(
            "Live per-attempt fetch tokens kept in memory (D48); the oldest is "
            "dropped above this, so a flood of attempts cannot grow the controller."
        ),
    )


class DiscoverySettings(SectionModel):
    """``[controller.discovery]`` - mDNS advertisement of the gRPC listener (0.3.9).

    Best effort: a host without multicast keeps serving and logs
    ``controller.discovery.unavailable``. Turn it off on CI or when agents are
    always configured with an explicit ``controller_address``.
    """

    advertise: bool = Field(
        default=True,
        description="Register the gRPC listener via mDNS/DNS-SD at start-up.",
    )
    service_type: str = Field(
        default=DEFAULT_SERVICE_TYPE,
        description="DNS-SD service type; agents browse for exactly this.",
    )

    @field_validator("service_type")
    @classmethod
    def _service_type_shape(cls, value: str) -> str:
        value = value.strip()
        if not value.endswith("._tcp.local.") or not value.startswith("_"):
            raise ValueError("service_type must look like _name._tcp.local.")
        return value


class ResourceHistorySettings(SectionModel):
    """``[controller.resources]`` - live ring + snapshot history (roadmap 0.3.11).

    Two levels (ARCHITECTURE §19): a per-node in-memory ring of the last few
    minutes, and 1-minute buckets written to ``resource_snapshot`` by the snapshot
    flusher. ``bucket_s`` is a setting so component tests can use 1 s buckets;
    ``retention_days`` is swept periodically rather than by table partitioning,
    which is premature at a handful of nodes.
    """

    live_ring_size: int = Field(
        default=150,
        ge=1,
        le=10_000,
        description=(
            "Samples kept in memory per node (150 = 5 minutes at one per 2 s)."
        ),
    )
    bucket_s: float = Field(
        default=60.0,
        gt=0,
        le=3600.0,
        description="Length of one history bucket written to resource_snapshot.",
    )
    bucket_grace_s: float = Field(
        default=DEFAULT_BUCKET_GRACE_S,
        ge=0,
        le=3600.0,
        description=(
            "How long after its end a history bucket still accepts late samples "
            "before it is closed and written (D26). The default (20 s) covers the "
            "reconnect + replay window of a blip: one lease (6 s) + criterion 2's "
            "restore bound (10 s, which the 5 s backoff cap keeps) + the replay "
            "itself. A sample later than this stays in the live ring, is left out "
            "of history (never merged into a stored row) and is logged as "
            "controller.resources.sample_late. Rows appear bucket_s + this after "
            "their bucket starts; the shutdown flush does not wait for it."
        ),
    )
    flush_interval_s: float = Field(
        default=10.0,
        gt=0,
        le=3600.0,
        description="How often closed buckets are written to the database.",
    )
    retention_days: float = Field(
        default=7.0,
        gt=0,
        le=3650.0,
        description="Snapshot rows older than this are deleted by the sweep.",
    )
    retention_sweep_s: float = Field(
        default=3600.0,
        gt=0,
        description="How often the retention delete runs.",
    )
    max_tracked_nodes: int = Field(
        default=256,
        ge=1,
        le=10_000,
        description=(
            "Nodes with a live ring; the least recently updated is evicted above "
            "this so a flood of unknown node_ids cannot grow the controller."
        ),
    )
    max_cursor_nodes: int = Field(
        default=16_384,
        ge=1,
        le=1_000_000,
        description=(
            "Nodes with a duplicate-detection cursor (three fields each), bounded "
            "separately from and far above max_tracked_nodes: evicting a live ring "
            "only loses history, while evicting a cursor lets a reconnecting "
            "agent's replayed report be counted twice. Evictions are counted and "
            "logged as controller.resources.cursor_evicted."
        ),
    )
    max_open_buckets: int = Field(
        default=8,
        ge=1,
        le=1_000,
        description=(
            "Open (not yet closed) history buckets per node; more than a couple "
            "means an agent clock that jumped. The oldest is dropped and counted."
        ),
    )


class LivenessSettings(SectionModel):
    """``[controller.liveness]`` - heartbeat/lease timing (ADR-0002 Updates §5).

    Mirrors :class:`~meshforge.controller.liveness.LivenessConfig`; the agent receives
    ``heartbeat_interval_s`` and the lease (``heartbeat_interval_s * lease_multiplier``)
    in ``HelloAck``, so retuning here retunes the fleet.
    """

    heartbeat_interval_s: float = Field(
        default=2.0,
        ge=0.1,
        le=30.0,
        description=(
            "Agents send one Heartbeat per interval. Bounded to the agent's clamp "
            "[0.1 s, 30 s] so a configured value is never silently changed on the "
            "agent side."
        ),
    )
    miss_allowance_s: float = Field(
        default=0.5,
        ge=0,
        description="Extra silence tolerated beyond one interval before SUSPECT.",
    )
    lease_multiplier: int = Field(
        default=3,
        ge=1,
        description="Lease = heartbeat_interval_s x this; silence past it = OFFLINE.",
    )

    @model_validator(mode="after")
    def _consistent(self) -> LivenessSettings:
        # LivenessConfig enforces offline_after_s > suspect_after_s; surface its
        # message as a validation error.
        self.to_config()
        return self

    def to_config(self) -> LivenessConfig:
        return LivenessConfig(
            heartbeat_interval_s=self.heartbeat_interval_s,
            miss_allowance_s=self.miss_allowance_s,
            lease_multiplier=self.lease_multiplier,
        )


class ControllerSection(SectionModel):
    """``[controller]`` section."""

    grpc_bind: str = Field(
        default=f"127.0.0.1:{DEFAULT_GRPC_PORT}",
        description=(
            "ip:port the agent gRPC server listens on (bracket IPv6). Port 0 picks "
            "a free port (tests). Must be a LAN/loopback IP unless allow_any_bind."
        ),
    )
    http_bind: str = Field(
        default=f"127.0.0.1:{DEFAULT_HTTP_PORT}",
        description="ip:port of the REST API (V0.3: GET /healthz only).",
    )
    allow_any_bind: bool = Field(
        default=False,
        description=(
            "Permit binding the wildcard (0.0.0.0/::) or a public address. Off by "
            "default: the controller accepts remote code execution requests."
        ),
    )
    accepted_protocol_versions: tuple[int, int] = Field(
        default=(1, 1),
        description=(
            "Inclusive [min, max] of Hello.protocol_version values accepted; "
            "anything else (and UNSPECIFIED=0) is rejected with PROTOCOL_VERSION."
        ),
    )
    liveness: LivenessSettings = Field(default_factory=LivenessSettings)
    discovery: DiscoverySettings = Field(default_factory=DiscoverySettings)
    resources: ResourceHistorySettings = Field(default_factory=ResourceHistorySettings)
    outbound_queue_size: int = Field(
        default=1000,
        ge=1,
        description=(
            "Controller->agent messages buffered per session; overflow closes the "
            "session as protocol_error instead of dropping silently."
        ),
    )
    hello_timeout_s: float = Field(
        default=5.0,
        gt=0,
        description="A new stream must send Hello within this or is closed.",
    )
    admission_timeout_s: float = Field(
        default=DEFAULT_ADMISSION_TIMEOUT_S,
        ge=0.5,
        le=30.0,
        description=(
            "Deadline for admitting one Hello (lock wait + registry round trips, "
            "V0.4 D41); past it the agent is told UNAVAILABLE and retries. Must be "
            "below the lease (heartbeat_interval_s x lease_multiplier): while a "
            "Hello is admitted, the node's older session gets no lease events. "
            "Unset: 5 s, or lease - 1 s (at least 0.5 s) when the lease is shorter."
        ),
    )
    shutdown_drain_s: float = Field(
        default=5.0,
        ge=0,
        description="Max time to drain sessions and servers on SIGINT/SIGTERM.",
    )
    dedupe_window: int = Field(
        default=10_000,
        ge=1,
        description="message_ids remembered per session for duplicate detection.",
    )
    token_file: Path | None = Field(
        default=None,
        description=(
            "The fabric token (D44, ADR-0014); created on first start. Default "
            "<data_dir>/controller/fabric.token. `meshforge-controller token show` "
            "prints it, `token rotate` replaces it (picked up within 1 s)."
        ),
    )
    blobs: BlobStoreSettings = Field(default_factory=BlobStoreSettings)
    jobs: JobApiSettings = Field(default_factory=JobApiSettings)

    @field_validator("grpc_bind", "http_bind")
    @classmethod
    def _bind_shape(cls, value: str) -> str:
        # Syntax only here (allow_any is another field); the LAN rule is applied
        # by ControllerSettings once all fields are known.
        parse_bind(value, allow_any=True)
        return value.strip()

    @field_validator("token_file", mode="before")
    @classmethod
    def _token_file_not_empty(cls, value: object) -> object:
        return _non_empty_path(value)

    @field_validator("accepted_protocol_versions")
    @classmethod
    def _version_range(cls, value: tuple[int, int]) -> tuple[int, int]:
        lo, hi = value
        if lo < 1:
            raise ValueError("min protocol version must be >= 1 (0 is UNSPECIFIED)")
        if hi < lo:
            raise ValueError("max protocol version must be >= min")
        return value

    @model_validator(mode="before")
    @classmethod
    def _admission_default(cls, data: Any) -> Any:
        """Unset ``admission_timeout_s`` follows a shortened lease (D41), so an
        operator who only retunes the heartbeat never trips the rule below."""
        if not isinstance(data, dict) or "admission_timeout_s" in data:
            return data
        try:
            liveness = LivenessSettings.model_validate(data.get("liveness") or {})
        except ValidationError:
            return data  # reported by the liveness field's own validation
        offline_s = liveness.to_config().offline_after_s
        default = min(DEFAULT_ADMISSION_TIMEOUT_S, max(0.5, offline_s - 1.0))
        return {**data, "admission_timeout_s": default}

    @model_validator(mode="after")
    def _admission_within_lease(self) -> ControllerSection:
        offline_s = self.liveness.to_config().offline_after_s
        if not self.admission_timeout_s < offline_s:
            raise ValueError(
                f"admission_timeout_s ({self.admission_timeout_s}) must be below the "
                f"OFFLINE edge ({offline_s} s = heartbeat_interval_s x "
                "lease_multiplier): a pending Hello suspends its node's lease events"
            )
        return self

    @model_validator(mode="after")
    def _binds_allowed(self) -> ControllerSection:
        parse_bind(self.grpc_bind, allow_any=self.allow_any_bind)
        parse_bind(self.http_bind, allow_any=self.allow_any_bind)
        return self

    def accepts_protocol_version(self, version: int) -> bool:
        lo, hi = self.accepted_protocol_versions
        return lo <= version <= hi


class ControllerSettings(MeshForgeSettings):
    """Everything the controller process needs (``[controller]`` + ``[database]``)."""

    controller: ControllerSection = Field(default_factory=ControllerSection)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    @property
    def token_path(self) -> Path:
        """Where the fabric token lives (``[controller] token_file`` or the default)."""
        return self.controller.token_file or default_token_path(self.data_dir)

    @property
    def blobs_dir(self) -> Path:
        """Blob store root (``[controller.blobs] dir`` or the default)."""
        return self.controller.blobs.dir or (
            self.data_dir / CONTROLLER_DIR_NAME / BLOBS_DIR_NAME
        )
