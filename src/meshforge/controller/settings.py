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

import re
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator, model_validator

from meshforge.common.config import MeshForgeSettings, SectionModel
from meshforge.common.fabric_token import CONTROLLER_DIR_NAME, default_token_path
from meshforge.common.manifest import PolicyName
from meshforge.common.netaddr import parse_bind
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.peer_signing import KEY_FILE_NAME as PEER_KEY_FILE_NAME
from meshforge.controller.resources import DEFAULT_BUCKET_GRACE_S
from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "BLOBS_DIR_NAME",
    "DEFAULT_GRPC_PORT",
    "DEFAULT_HTTP_PORT",
    "DEFAULT_SERVICE_TYPE",
    "ApiSettings",
    "BlobStoreSettings",
    "ControllerSection",
    "ControllerSettings",
    "DiscoverySettings",
    "GcSettings",
    "JobApiSettings",
    "LivenessSettings",
    "PlannerSettings",
    "ResourceHistorySettings",
]

DEFAULT_GRPC_PORT = 47100
DEFAULT_HTTP_PORT = 47110
DEFAULT_SERVICE_TYPE = "_meshforge-ctl._tcp.local."
"""DNS-SD service type advertised for agents (must match ``[agent.discovery]``)."""
DEFAULT_ADMISSION_TIMEOUT_S = 5.0
"""D41: default Hello admission deadline (below the default 6 s lease)."""
BLOBS_DIR_NAME = "blobs"
LOGS_DIR_NAME = "logs"
_ADVERTISE_URL = re.compile(
    r"https?://(\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]{1,253}):\d{1,5}"
)


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
        default=64 * 1024**3,
        ge=1024 * 1024,
        le=64 * 1024**3,
        description=(
            "Largest blob the store keeps, whole or assembled from parts (V0.5: "
            "64 GiB, the tree limit, so one multi-GB dataset file fits; bundles "
            "stay capped at 1 GiB of content and outputs by [controller.jobs] "
            "max_output_bytes). A larger upload is refused before or while it "
            "streams, and nothing is kept."
        ),
    )
    max_concurrent_uploads: int = Field(
        default=8,
        ge=1,
        le=256,
        description=(
            "Uploads that may stream at once (V0.4 D73); a further one waits up to "
            "upload_wait_s, then is refused resource_busy (503)."
        ),
    )
    upload_wait_s: float = Field(
        default=30.0,
        ge=0.1,
        le=600.0,
        description="How long an upload waits for a free upload slot.",
    )
    free_disk_floor_mb: int = Field(
        default=1024,
        ge=0,
        le=1_048_576,
        description=(
            "Free space the store keeps on its disk: an upload that would go below "
            "it is refused disk_full (507, insufficient storage). 0 = no floor."
        ),
    )

    @field_validator("dir", mode="before")
    @classmethod
    def _dir_not_empty(cls, value: object) -> object:
        return _non_empty_path(value)


class GcSettings(SectionModel):
    """``[controller.gc]`` - GC and scrub of the controller's blob store (V0.5 G5).

    Never evicted: anything a non-terminal job references (tree, bundle, inputs,
    outputs, logs), pinned named inputs, and blobs used within ``min_age_s``.
    The scrub re-hashes stored blobs at a bounded rate and removes corrupt ones.
    """

    enabled: bool = Field(default=True, description="Run GC passes and the scrub.")
    cas_bytes: int = Field(
        default=100 * 1024**3,
        ge=0,
        description="Quota of the controller's blob store (bytes; 0 = none).",
    )
    min_age_s: float = Field(
        default=86_400.0,
        ge=0.0,
        le=365 * 86_400.0,
        description=(
            "Blobs used (uploaded, answered 'have', downloaded) within this many "
            "seconds are never evicted - this covers a client between have/need "
            "and its submit, and recent outputs not downloaded yet."
        ),
    )
    interval_s: float = Field(
        default=3600.0, ge=1.0, le=86_400.0, description="A GC pass runs this often."
    )
    batch: int = Field(
        default=256, ge=1, le=10_000, description="Victims removed per transaction."
    )
    scrub_bytes_per_s: int = Field(
        default=32 * 1024**2,
        ge=1024,
        description="Scrub read rate (bytes/s): bounded so it never starves uploads.",
    )
    scrub_interval_s: float = Field(
        default=86_400.0,
        ge=1.0,
        le=30 * 86_400.0,
        description=(
            "Pause between full scrub cycles over the store; suspected blobs "
            "(an agent's integrity_error on a controller-served tree) are "
            "checked at once."
        ),
    )


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
    # --- outputs (V0.4 G5, D73) -------------------------------------------
    max_output_bytes: int = Field(
        default=2 * 1024**3,
        ge=1024 * 1024,
        le=64 * 1024**3,
        description=(
            "Bytes one attempt may upload in total (outputs archive + final logs); "
            "enforced by the attempt's upload grant (D73)."
        ),
    )
    max_output_blobs: int = Field(
        default=3,
        ge=1,
        le=64,
        description="Blobs one attempt may upload (outputs archive + 2 log files).",
    )
    # --- logs (V0.4 G5, D71/D72) -------------------------------------------
    max_log_bytes: int = Field(
        default=64 * 1024 * 1024 + 64 * 1024,
        ge=64 * 1024,
        le=8 * 1024**3,
        description=(
            "Per attempt and stream: a live log chunk ending past this is refused "
            "(the agent caps its logs at [agent.execution] max_log_bytes plus a "
            "marker line)."
        ),
    )
    log_queue_chunks: int = Field(
        default=4096,
        ge=16,
        le=1_000_000,
        description=(
            "Live log chunks waiting for the log writer; past it chunks are "
            "dropped (a recorded gap) so a flood never slows a session."
        ),
    )
    log_subscriber_queue: int = Field(
        default=1024,
        ge=8,
        le=100_000,
        description="Log events buffered per follower; a slower one is disconnected.",
    )
    max_log_subscribers: int = Field(
        default=8, ge=1, le=1024, description="Concurrent log followers per job."
    )
    # --- dispatch (V0.4 G4, D65/D66/D69) ------------------------------------
    dispatch_interval_s: float = Field(
        default=1.0,
        ge=0.05,
        le=60.0,
        description=(
            "Dispatcher tick: queued jobs are also picked up this often (a submit "
            "wakes it at once), and the backstop/orphan sweeps run on it."
        ),
    )
    dispatch_batch: int = Field(
        default=100, ge=1, le=10_000, description="Queued jobs examined per tick."
    )
    max_attempts_per_node: int = Field(
        default=8,
        ge=1,
        le=64,
        description=(
            "Upper bound on live attempts per node, whatever its slots say "
            "(V0.6 D105: each node's own CPU/GPU slots - [agent.execution] "
            "max_cpu_jobs / max_gpu_jobs - decide below it; V0.4's default was 1)."
        ),
    )
    backstop_grace_s: float = Field(
        default=30.0,
        ge=1.0,
        le=3600.0,
        description=(
            "D69: an attempt still RUNNING this long past its job's time limit is "
            "failed deadline_exceeded by the controller and cancelled."
        ),
    )
    reconnect_grace_s: float = Field(
        default=15.0,
        ge=0.0,
        le=3600.0,
        description=(
            "After a controller start, how long a node may take to reconnect "
            "before its queued jobs fail node_lost and its live attempts are lost."
        ),
    )
    fetch_token_grace_s: float = Field(
        default=3600.0,
        ge=10.0,
        le=86_400.0,
        description=(
            "Fetch-token lifetime beyond the job's time limit (covers the env "
            "phase and output upload, D60); shortened at attempt end."
        ),
    )
    fetch_token_end_grace_s: float = Field(
        default=60.0,
        ge=0.0,
        le=3600.0,
        description="How long a fetch token stays valid after its attempt ended.",
    )
    status_queue_size: int = Field(
        default=10_000,
        ge=16,
        le=1_000_000,
        description=(
            "Agent attempt reports waiting to be stored; above this a report is not "
            "acked (the agent replays it after its next reconnect)."
        ),
    )
    # --- V0.4 debt carried into V0.5 (G3, D94) -------------------------------
    upload_deadline_s: float | None = Field(
        default=None,
        ge=60.0,
        le=7 * 86_400.0,
        description=(
            "D94: an attempt still UPLOADING this long after it entered UPLOADING "
            "(the job's COLLECTING transition, persisted - a controller restart "
            "keeps it) is failed output_error and its upload grant revoked. Empty = "
            "upload_deadline_base_s + max_output_bytes / upload_min_rate_bytes_s."
        ),
    )
    upload_deadline_base_s: float = Field(
        default=600.0,
        ge=30.0,
        le=86_400.0,
        description=(
            "Fixed part of the derived upload deadline (collecting + packing the "
            "outputs, the final logs, retries)."
        ),
    )
    upload_min_rate_bytes_s: int = Field(
        default=1024 * 1024,
        ge=64 * 1024,
        le=10 * 1024**3,
        description=(
            "Slowest upload rate the derived deadline still allows for a full "
            "max_output_bytes upload (default 1 MiB/s: 2 GiB -> 2048 s + base)."
        ),
    )
    log_retention_s: float = Field(
        default=7 * 86_400.0,
        ge=3600.0,
        le=365 * 86_400.0,
        description=(
            "D94: log files of an attempt whose job is terminal are deleted this "
            "long after the job's last change. A non-terminal job's logs are "
            "never deleted."
        ),
    )
    log_gc_interval_s: float = Field(
        default=3600.0,
        ge=10.0,
        le=86_400.0,
        description="How often the controller sweeps the logs folder (D94).",
    )
    # --- peers (V0.5 G4, D89/D98/D100) ---------------------------------------
    max_peer_sources: int = Field(
        default=4,
        ge=0,
        le=16,
        description=(
            "D100: ONLINE nodes holding a job's tree listed as extra blob sources "
            "next to the controller (never the target node). 0 = controller only."
        ),
    )
    peer_token_ttl_s: int = Field(
        default=600,
        ge=60,
        le=600,
        description=(
            "D98: lifetime of a signed peer blob token (at most 10 minutes); "
            "renewed with every StartAttempt and RefreshFetchToken."
        ),
    )
    log_gc_batch: int = Field(
        default=1000,
        ge=1,
        le=100_000,
        description=(
            "Attempts whose log files are old enough that one sweep checks "
            "against the database; the rest wait for the next sweep."
        ),
    )
    # --- reliability floor (V0.6 G1, D116) ---------------------------------
    env_bad_ttl_s: float = Field(
        default=3600.0,
        ge=0.0,
        le=7 * 86_400.0,
        description=(
            "D116: after an attempt fails env_error, that node + environment "
            "request is remembered as known-bad this long (in memory; a restart "
            "forgets it) so the planner can avoid it. 0 = never remembered."
        ),
    )
    known_bad_envs_max: int = Field(
        default=10_000,
        ge=1,
        le=1_000_000,
        description="Known-bad (node, env) entries kept; the oldest is dropped.",
    )

    @property
    def upload_deadline_total_s(self) -> float:
        """The effective UPLOADING deadline in seconds (see ``upload_deadline_s``)."""
        if self.upload_deadline_s is not None:
            return self.upload_deadline_s
        return (
            self.upload_deadline_base_s
            + self.max_output_bytes / self.upload_min_rate_bytes_s
        )


class PlannerSettings(SectionModel):
    """``[controller.planner]`` - automatic placement (V0.6 G2, D102-D108).

    The planner runs on every dispatch tick over a snapshot of the fabric; the
    defaults below stand in for numbers nothing has measured yet, and every
    estimate built on them is shown as ``assumed`` by ``compute explain``.
    """

    default_policy: PolicyName = Field(
        default="cost_model",
        description=(
            "D107: the placement policy for a job that names none (manifest "
            "`policy:` or POST placement.policy override it): random, "
            "round_robin, least_loaded, strongest_suitable, resource_score, "
            "cache_affinity or cost_model."
        ),
    )
    priority_aging_s: float = Field(
        default=600.0,
        ge=1.0,
        le=86_400.0,
        description=(
            "D106: a queued job's effective priority rises by 1 per this many "
            "seconds of waiting, up to the maximum priority (10), so a low "
            "priority job is never starved."
        ),
    )
    default_bw_bytes_s: float = Field(
        default=50e6,
        ge=1e3,
        le=1e11,
        description=(
            "Assumed bandwidth (bytes/s) for the transfer estimate until links "
            "are measured (V0.6 G4)."
        ),
    )
    default_compute_s: float = Field(
        default=60.0,
        ge=0.0,
        le=7 * 86_400.0,
        description=(
            "Assumed run time of a workload with no recorded history on any node."
        ),
    )
    default_env_s: float = Field(
        default=0.0,
        ge=0.0,
        le=86_400.0,
        description="Assumed environment build time until it is measured (G4).",
    )
    resource_weights: tuple[float, float, float] = Field(
        default=(1.0, 1.0, 1.0),
        description=(
            "resource_score policy weights: (free RAM in GiB, free slots, "
            "hardware score normalised to the best candidate). Each 0-1000."
        ),
    )
    history_runs: int = Field(
        default=20,
        ge=1,
        le=1000,
        description=(
            "Most recent successful runs per (workload, node) the compute-time "
            "median is taken over."
        ),
    )
    retry_exclude_s: float = Field(
        default=600.0,
        ge=0.0,
        le=86_400.0,
        description=(
            "V0.6 D110: after an automatically placed attempt is lost or fails "
            "transfer_error / integrity_error / timeout, its retry avoids that "
            "node for this many seconds (unless no other node could ever run "
            "the job). 0 = no exclusion by time (vram_oom still excludes GPUs "
            "no larger than the one that ran out)."
        ),
    )

    @field_validator("resource_weights")
    @classmethod
    def _weights(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(not 0.0 <= w <= 1000.0 for w in value):
            raise ValueError("each resource weight must be between 0 and 1000")
        return value


class ApiSettings(SectionModel):
    """``[controller.api]`` - how agents reach this controller's REST port."""

    advertise_url: str | None = Field(
        default=None,
        description=(
            "Base URL agents fetch bundles from (http://host:port, no path). Empty "
            "= each agent uses the host it dialled for gRPC and this REST port."
        ),
    )

    @field_validator("advertise_url")
    @classmethod
    def _url_shape(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().rstrip("/")
        if not _ADVERTISE_URL.fullmatch(value):
            raise ValueError(
                "advertise_url must be http(s)://host:port with no path, e.g. "
                "http://192.168.1.10:47110"
            )
        return value


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
    command_resend_s: float = Field(
        default=10.0,
        ge=1.0,
        le=300.0,
        description=(
            "V0.6 D113: a GcRequest / PrepareEnv / ProbeLink the agent has not "
            "acked this long after it was sent is sent again (same message_id: "
            "the agent drops a copy it already applied). Attempt commands are "
            "re-sent by Reconcile instead."
        ),
    )
    command_resend_tries: int = Field(
        default=5,
        ge=0,
        le=20,
        description=(
            "Re-sends of one unacked command before it is logged "
            "session.command_unacked and no longer re-sent (0 = never re-send)."
        ),
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
    peer_key_file: Path | None = Field(
        default=None,
        description=(
            "V0.5 D98: Ed25519 key that signs peer blob tokens; created on first "
            "start (0600, refused when other users can read it). Default "
            "<data_dir>/controller/peer_signing.key. Its public half goes to every "
            "agent in HelloAck; keeping the file keeps issued tokens valid across "
            "a restart."
        ),
    )
    trust_peer_endpoints: bool = Field(
        default=False,
        description=(
            "V0.5 D99: accept an agent's advertised peer endpoint (Hello."
            "peer_endpoint) even when its host differs from the address the "
            "session comes from. Only for NAT'd lab setups; off by default so a "
            "node cannot advertise another machine as its own."
        ),
    )
    blobs: BlobStoreSettings = Field(default_factory=BlobStoreSettings)
    gc: GcSettings = Field(default_factory=GcSettings)
    jobs: JobApiSettings = Field(default_factory=JobApiSettings)
    planner: PlannerSettings = Field(default_factory=PlannerSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)

    @field_validator("grpc_bind", "http_bind")
    @classmethod
    def _bind_shape(cls, value: str) -> str:
        # Syntax only here (allow_any is another field); the LAN rule is applied
        # by ControllerSettings once all fields are known.
        parse_bind(value, allow_any=True)
        return value.strip()

    @field_validator("token_file", "peer_key_file", mode="before")
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
    def peer_key_path(self) -> Path:
        """Peer-token signing key (``[controller] peer_key_file`` or the default)."""
        return self.controller.peer_key_file or (
            self.data_dir / CONTROLLER_DIR_NAME / PEER_KEY_FILE_NAME
        )

    @property
    def logs_dir(self) -> Path:
        """Attempt log files (``<data_dir>/controller/logs``, D71)."""
        return self.data_dir / CONTROLLER_DIR_NAME / LOGS_DIR_NAME

    @property
    def gc_dir(self) -> Path:
        """GC last-use index of the blob store (``<data_dir>/controller/gc``)."""
        return self.data_dir / CONTROLLER_DIR_NAME / "gc"

    @property
    def blobs_dir(self) -> Path:
        """Blob store root (``[controller.blobs] dir`` or the default)."""
        return self.controller.blobs.dir or (
            self.data_dir / CONTROLLER_DIR_NAME / BLOBS_DIR_NAME
        )
