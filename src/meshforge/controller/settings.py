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

import math
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Final

from pydantic import Field, ValidationError, field_validator, model_validator

from meshforge.common.cas import DEFAULT_MAX_RESUMABLE, DEFAULT_MAX_RESUMABLE_PER_OWNER
from meshforge.common.config import (
    InstallMode,
    MeshForgeSettings,
    SectionModel,
    install_mode,
)
from meshforge.common.errors import ConfigError
from meshforge.common.fabric_token import CONTROLLER_DIR_NAME, default_token_path
from meshforge.common.manifest import (
    MAX_SPLIT_CHUNKS,
    MAX_SPLIT_ITEMS,
    ObjectiveName,
    PolicyName,
)
from meshforge.common.ml_eval import DEFAULT_ML_EVAL_TIMEOUT_S
from meshforge.common.netaddr import is_loopback_host, parse_bind
from meshforge.common.pki import PkiError, validate_san_host
from meshforge.controller.fabric_ca import (
    CA_DIR_NAME,
    TLS_DIR_NAME,
    server_cert_hosts,
)
from meshforge.controller.liveness import LivenessConfig
from meshforge.controller.peer_signing import KEY_FILE_NAME as PEER_KEY_FILE_NAME
from meshforge.controller.resources import DEFAULT_BUCKET_GRACE_S
from meshforge.controller.secret_box import KEY_FILE_NAME as SECRETS_KEY_FILE_NAME
from meshforge.controller.store.settings import DatabaseSettings

__all__ = [
    "BLOBS_DIR_NAME",
    "DB_DIR_NAME",
    "DEFAULT_ENROLL_PORT",
    "DEFAULT_GRPC_PORT",
    "DEFAULT_HTTP_PORT",
    "DEFAULT_MANAGED_DB_PORT",
    "DEFAULT_SERVICE_TYPE",
    "SETUP_TOKEN_NAME",
    "ApiSettings",
    "AuthSettings",
    "BlobStoreSettings",
    "ControllerSection",
    "ControllerSettings",
    "DbMode",
    "DbSettings",
    "DiscoverySettings",
    "EnrollSettings",
    "GcSettings",
    "JobApiSettings",
    "LivenessSettings",
    "NetworkSettings",
    "PlannerSettings",
    "RenewalSettings",
    "ResourceHistorySettings",
    "SetupSettings",
    "TlsSettings",
]

DEFAULT_GRPC_PORT = 47100
DEFAULT_ENROLL_PORT = 47101
"""V0.7 G2 (D150): the interim TLS enrolment listener (gone in G3)."""
DEFAULT_HTTP_PORT = 47110
DEFAULT_SERVICE_TYPE = "_meshforge-ctl._tcp.local."
"""DNS-SD service type advertised for agents (must match ``[agent.discovery]``)."""
DEFAULT_ADMISSION_TIMEOUT_S = 5.0
"""D41: default Hello admission deadline (below the default 6 s lease)."""
BLOBS_DIR_NAME = "blobs"
DB_DIR_NAME = "db"
"""``<data_dir>/db``: the managed PostgreSQL root (D390), next to ``controller/``
(a sibling, not inside it: the agent package's purge must be able to tell the
controller's state apart by top-level name)."""
LOGS_DIR_NAME = "logs"
SETUP_TOKEN_NAME = "setup-token"  # noqa: S105 - a file name
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
    max_resumable_per_owner: int = Field(
        default=DEFAULT_MAX_RESUMABLE_PER_OWNER,
        ge=1,
        le=DEFAULT_MAX_RESUMABLE,
        description=(
            "Part uploads (resumable transfers) one user or node may have in "
            f"progress at once, under the store-wide {DEFAULT_MAX_RESUMABLE} "
            "(D158): a further new one is refused resource_busy (503) while other "
            "uploaders still start."
        ),
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
    # --- secrets (V0.7 G6, D162) -------------------------------------------
    max_job_secret_bytes: int = Field(
        default=256 * 1024,
        ge=4,
        le=1024 * 1024,
        description=(
            "Bytes of secret values one job may name in total (the sum over its "
            "manifest's `secrets`). A larger job is refused at submit and again "
            "at dispatch (invalid_input, secret_bytes_over_limit). It bounds the "
            "log redactor: every value is searched for in ~14 encodings, so this "
            "many bytes cost about 21x as much memory and tens of milliseconds "
            "per attempt on the controller and on the laptop."
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
        default=60,
        ge=5,
        le=60,
        description=(
            "D98/D147: lifetime of a signed peer blob token (at most 60 s). "
            "Minted with every StartAttempt and RefreshFetchToken, and re-minted "
            "every third of this lifetime while the attempt stages "
            "(RefreshPeerTokens), so a long transfer never needs a longer token."
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
            "node for this many seconds (disk_full too, D126). When no other "
            "node could ever run the job, D129 lifts exclusions early only "
            "onto a node that would take the job now (node_lost/timeout "
            "first, vram_oom last; disk_full never). 0 = no exclusion by time "
            "(vram_oom still excludes GPUs no larger than the one that ran "
            "out)."
        ),
    )

    unbound_scores_max_age_s: float = Field(
        default=7 * 86_400.0,
        ge=0.0,
        le=365 * 86_400.0,
        description=(
            "V0.7 G6 (D161 N3): a hardware-score document stored before V0.7 G5 "
            "carries no node binding and is used only until the node's next "
            "Hello re-binds it. One measured longer ago than this is cleared "
            "(at controller start and every unbound_scores_check_s), so a node "
            "that never returns cannot keep unbound numbers forever. 0 = clear "
            "every unbound document at the next check."
        ),
    )
    unbound_scores_check_s: float = Field(
        default=3600.0,
        ge=1.0,
        le=86_400.0,
        description="How often unbound_scores_max_age_s is applied while running.",
    )
    replay_max_bytes: int = Field(
        default=65_536,
        ge=1024,
        le=1_048_576,
        description=(
            "V0.14 D292: every plan_decision row stores a replay snapshot (the "
            "planner's exact inputs, so the decision can be re-run with any "
            "policy). A replay larger than this many bytes of canonical JSON "
            "is not stored (replay = null; counted and logged as "
            "controller.jobs.replay_over_cap) - it never fails a dispatch."
        ),
    )
    # ------------------------------------------------ V0.14 G2 (D293, D301, D302)
    calibration: str = Field(
        default="auto",
        description=(
            "V0.14 D293/D301: what the planner scales its estimates by. auto = "
            "the active planner_calibration version (refreshed every "
            "calibration_refresh_s; a new version goes live only if it is no "
            "more than 5 % worse than the active one on held-out runs); off = "
            'no calibration; a version number (e.g. "7") = pin that version '
            "(the controller refuses to start if it does not exist)."
        ),
    )
    calibration_window_runs: int = Field(
        default=200,
        ge=1,
        le=1000,
        description=(
            "V0.14 D293: a factor is the median actual/predicted ratio of at "
            "most this many most recent runs of its key (node + workload "
            "family, node, or global)."
        ),
    )
    calibration_shrink_k: float = Field(
        default=5.0,
        gt=0.0,
        le=1000.0,
        description=(
            "V0.14 D293: shrinkage toward 1 - a factor fitted on n runs moves "
            "n / (n + k) of the way from 1 to the median ratio, so one odd run "
            "cannot swing the planner."
        ),
    )
    calibration_clamp: tuple[float, float] = Field(
        default=(0.2, 5.0),
        description=(
            "V0.14 D293: [low, high] bounds of every factor (low < 1 < high)."
        ),
    )
    calibration_refresh_s: float = Field(
        default=1800.0,
        ge=10.0,
        le=7 * 86_400.0,
        description=(
            "V0.14 D293: how often the controller refits the calibration "
            "(calibration = auto only; compute planner calibrate refits now)."
        ),
    )

    # ------------------------------------------------ V0.15 G1 (D333)
    ml_dataset_max_runs: int = Field(
        default=50_000,
        ge=1,
        le=1_000_000,
        description=(
            "V0.15 D333: the ML training dataset (GET /v1/planner/ml/dataset, "
            "compute planner ml dataset) is built from at most this many "
            "newest matched (decision, outcome) runs; the summary says when "
            "older runs were left out (truncated)."
        ),
    )
    # ------------------------------------------------ V0.15 G4 (D336, D363)
    ml_max_interval_log: float = Field(
        default=0.6931471805599453,  # ln 2
        ge=0.0,
        le=20.0,
        description=(
            "V0.15 D336/D338: an ML correction is used only when its interval "
            "(hi - lo, log space) is at most this wide - ln 2 = the interval "
            "spans at most a factor 2; else the term keeps its calibration "
            "(reason interval_too_wide). The activation gate (V0.15 G3) judges "
            "a new model set with the same rule."
        ),
    )
    ml_predict_budget_ms: float = Field(
        default=20.0,
        gt=0.0,
        le=10_000.0,
        description=(
            "V0.15 D363: an ML predict call slower than this is discarded and "
            "the decision falls back to calibration (predictor_error); logged "
            "at most once a minute."
        ),
    )

    # ------------------------------------------------ V0.15 G3 (D339, D358-D361)
    ml: str = Field(
        default="auto",
        description=(
            "V0.15 D339: which ML model set the planner may use. auto = the "
            "pinned set, else the active one (a retrained set goes live only "
            "through the activation gate); off = no ML (plans exactly as "
            'V0.14); a set version (e.g. "sha256:3a1f...") = pin that set '
            "(the controller refuses to start if it does not exist, was "
            "rejected or cannot be loaded)."
        ),
    )
    ml_retrain_s: float = Field(
        default=86_400.0,
        ge=60.0,
        le=30 * 86_400.0,
        description=(
            "V0.15 D361: how often the controller retrains the model set (ml = "
            "auto, with the ml extra installed; skipped when no run ended "
            "since the last set). compute planner ml train retrains now."
        ),
    )
    ml_retrain_timeout_s: float = Field(
        default=3600.0,
        ge=10.0,
        le=86_400.0,
        description=(
            "V0.15 D361: a retrain (a child process) that runs longer is "
            "killed; nothing is stored."
        ),
    )
    ml_eval_timeout_s: float = Field(
        default=DEFAULT_ML_EVAL_TIMEOUT_S,
        ge=1.0,
        le=86_400.0,
        description=(
            "V0.15 G5a (D368): total time one planner evaluation may spend "
            "training its fold models (one per evaluated family, one after "
            "another). A fold still running when it is spent is killed and "
            "the families left are evaluated without an ML model "
            "(no_fold_model, reason eval_budget_exhausted). compute planner "
            "eval waits this default plus 600 s."
        ),
    )
    ml_classifier_timeout_s: float = Field(
        default=900.0,
        ge=1.0,
        le=86_400.0,
        description=(
            "V0.15 G6a (D375, fix r1 N2): total time one classifier examples "
            "build (POST /v1/planner/ml/classifier-examples, compute planner ml "
            "classify) may take, waiting for another build included. Over it "
            "the build stops and the request answers 504 timeout; nothing is "
            "stored either way."
        ),
    )
    ml_replay_load_timeout_s: float = Field(
        default=60.0,
        ge=1.0,
        le=3600.0,
        description=(
            "V0.15 G6a (D364, fix r1 N1): a decision replay (compute planner "
            "replay) waits at most this long for its model set to load; over "
            "it the request answers 503 (retry)."
        ),
    )
    ml_min_rows: int = Field(
        default=200,
        ge=1,
        le=10_000_000,
        description="V0.15 D348: a term is trained only with this many rows.",
    )
    ml_min_families: int = Field(
        default=3,
        ge=1,
        le=1000,
        description="V0.15 D348: ... and this many workload families.",
    )
    ml_family_tolerance_log: float = Field(
        default=0.05,
        gt=0.0,
        le=10.0,
        description=(
            "V0.15 D339/D351: the activation gate rejects a set that is worse "
            "than the calibration by more than this log error on any family."
        ),
    )
    ml_min_effect_log: float = Field(
        default=0.02,
        ge=0.0,
        le=10.0,
        description=(
            "V0.15 D357: a set is activated only if a learned term beats the "
            "calibration by more than this log error on the held-out slice."
        ),
    )
    ml_keep_superseded: int = Field(
        default=5,
        ge=0,
        le=1000,
        description=(
            "V0.15 G3: the blob GC keeps the artefacts of the active, the "
            "pinned and this many newest superseded model sets (the rollback "
            "window); a rejected set's artefacts may be collected."
        ),
    )

    @field_validator("ml", mode="before")
    @classmethod
    def _ml(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError('ml must be "auto", "off" or a set version')  # noqa: TRY004
        value = value.strip()
        if value in ("auto", "off"):
            return value
        algorithm, _, hex_part = value.partition(":")
        if (
            algorithm != "sha256"
            or len(hex_part) != 64
            or any(c not in "0123456789abcdef" for c in hex_part)
        ):
            raise ValueError(
                'ml must be "auto", "off" or a model set version "sha256:<64 hex>"'
            )
        return value

    @field_validator("calibration", mode="before")
    @classmethod
    def _calibration(cls, value: object) -> str:
        # pydantic reports a ValueError as a validation error (not TypeError)
        if isinstance(value, bool) or not isinstance(value, int | str):
            raise ValueError('calibration must be "auto", "off" or a version number')  # noqa: TRY004
        if isinstance(value, int):
            value = str(value)
        value = value.strip()
        if value in ("auto", "off"):
            return value
        if not value.isascii() or not value.isdigit() or int(value) < 1:
            raise ValueError(
                'calibration must be "auto", "off" or a version number >= 1'
            )
        if int(value) >= 2**63:
            raise ValueError("calibration version is too large")
        return str(int(value))

    @field_validator("calibration_clamp")
    @classmethod
    def _clamp(cls, value: tuple[float, float]) -> tuple[float, float]:
        low, high = value
        if not (0.0 < low < 1.0 < high) or not math.isfinite(high) or high > 1000.0:
            raise ValueError(
                "calibration_clamp must be [low, high] with 0 < low < 1 < high <= 1000"
            )
        return value

    # ------------------------------------------------ end V0.14 G2

    # --- V0.14 G3 (D294-D296, D302): risk, objectives, links, latency ----------
    default_objective: ObjectiveName = Field(
        default="min_time",
        description=(
            "D295: what the cost_model policy ranks by for a job that names no "
            "objective (manifest `objective:` or POST placement.objective "
            "override it): min_time (fastest predicted completion), min_network "
            "(fewest bytes moved; time breaks ties) or prefer_local (the "
            "submitter's own node unless another is clearly faster). Rule "
            "policies ignore it."
        ),
    )
    prefer_local_margin: float = Field(
        default=0.25,
        ge=0.0,
        le=0.95,
        description=(
            "D295 prefer_local: another candidate takes the job from the Local "
            "node only when predicted at least this fraction faster."
        ),
    )
    risk_window_days: float = Field(
        default=7.0,
        ge=0.0,
        le=365.0,
        description=(
            "D296: node-caused attempt ends (lost, node_lost, owner_preempted, "
            "disk_full, env_error, transfer_error - never a job's own failure) "
            "over this many days set each node's failure probability and so "
            "the risk term of its estimates. 0 = no risk term."
        ),
    )
    risk_prior: tuple[float, float] = Field(
        default=(1.0, 19.0),
        description=(
            "D296: Beta prior (a, b) of the failure probability "
            "(a + failures) / (a + b + attempts): a node with no history is "
            "assumed to fail a/(a+b) of its attempts (5 %). Each 0.01-1000."
        ),
    )
    min_useful_bw_bytes_s: float = Field(
        default=1e6,
        ge=0.0,
        le=1e11,
        description=(
            "D294 link_infeasible: a candidate that would fetch more than "
            "min_useful_bytes over a freshly measured link slower than this "
            "(bytes/s) is rejected before costing. Stale or unmeasured links "
            "never reject. 0 = off."
        ),
    )
    min_useful_bytes: int = Field(
        default=64 * 1024 * 1024,
        ge=0,
        le=1 << 50,
        description="D294: see min_useful_bw_bytes_s (bytes).",
    )
    decision_latency_budget_ms: float = Field(
        default=100.0,
        gt=0.0,
        le=60_000.0,
        description=(
            "D294 / criterion 3: a dispatch decision (snapshot cut, plan, "
            "replay) that takes longer than this is logged "
            "(controller.jobs.decision_over_budget) and counted."
        ),
    )
    # --- end V0.14 G3 ------------------------------------------------------------

    @field_validator("risk_prior")
    @classmethod
    def _risk_prior(cls, value: tuple[float, float]) -> tuple[float, float]:
        """V0.14 G3: both Beta parameters positive and bounded."""
        if any(not 0.01 <= p <= 1000.0 for p in value):
            raise ValueError("each risk_prior value must be between 0.01 and 1000")
        return value

    @field_validator("resource_weights")
    @classmethod
    def _weights(cls, value: tuple[float, float, float]) -> tuple[float, float, float]:
        if any(not 0.0 <= w <= 1000.0 for w in value):
            raise ValueError("each resource weight must be between 0 and 1000")
        return value


_MIB = 1024 * 1024


STALE_INTERVAL_MARGIN: Final = 1.5
"""``stale_after_s`` must be at least this many probe intervals: a link then
survives one late or failed probe round without going stale."""


class NetworkSettings(SectionModel):
    """``[controller.network]`` - peer network probes -> the network matrix
    (V0.6 G4, D111).

    The controller asks one node at a time to download a few MiB of zeros from
    another node's peer server and stores the measured RTT and throughput per
    ordered pair as an EWMA (``network_link``). Probes are rare and bounded so
    they never flood a personal laptop's Wi-Fi: each pair at most once per
    ``probe_interval_s``, at most one probe per node at a time, at most
    ``probe_budget_bytes_per_min`` across the fabric, never on a node running
    a GPU job or above ``busy_cpu_percent``, one probe started per
    ``probe_gap_s`` (jittered). Controller <-> node links need no probe: they
    come from transfer history.
    """

    enabled: bool = Field(
        default=True, description="Schedule network probes between peers."
    )
    probe_interval_s: float = Field(
        default=900.0,
        ge=10.0,
        le=7 * 86_400.0,
        description="Each ordered node pair is probed at most once per this.",
    )
    probe_gap_s: float = Field(
        default=15.0,
        ge=0.1,
        le=3600.0,
        description=(
            "At most one probe is started per this many seconds (fabric-wide), "
            "+/- probe_jitter."
        ),
    )
    probe_jitter: float = Field(
        default=0.5,
        ge=0.0,
        le=0.9,
        description="Relative jitter of probe_gap_s (0.5 = 0.5x .. 1.5x).",
    )
    probe_budget_bytes_per_min: int = Field(
        default=48 * _MIB,
        ge=0,
        le=10 * 1024 * _MIB,
        description=(
            "Probe bytes allowed across the fabric in any 60 s (a probe in "
            "flight reserves its worst case: probe_bytes + probe_max_bytes). "
            "Must fit one probe."
        ),
    )
    probe_bytes: int = Field(
        default=4 * _MIB,
        ge=1,
        le=8 * _MIB,
        description=(
            "Throughput download size. At least 1: a probe that downloads "
            "nothing measures no bandwidth and could never fill the matrix."
        ),
    )
    probe_max_bytes: int = Field(
        default=8 * _MIB,
        ge=0,
        le=8 * _MIB,
        description=(
            "Second, larger download when the first took less than probe_fast_s."
        ),
    )
    probe_fast_s: float = Field(
        default=0.5,
        ge=0.0,
        le=60.0,
        description="A first download faster than this is repeated at probe_max_bytes.",
    )
    rtt_samples: int = Field(
        default=3, ge=1, le=10, description="0-byte requests; the median is the RTT."
    )
    probe_timeout_s: float = Field(
        default=60.0,
        ge=1.0,
        le=600.0,
        description=(
            "A probe with no result after this is given up (its nodes are free "
            "again; counted as failed)."
        ),
    )
    token_ttl_s: int = Field(
        default=60,
        ge=1,
        le=60,
        description="Lifetime of the probe token the target checks (<= 60 s).",
    )
    busy_cpu_percent: float = Field(
        default=80.0,
        ge=1.0,
        le=100.0,
        description="A node whose latest CPU sample is above this is not probed.",
    )
    cpu_sample_max_age_s: float = Field(
        default=30.0,
        ge=1.0,
        le=3600.0,
        description=(
            "A node whose latest resource sample is older than this is not "
            "probed: its load is unknown, not low (3x the agents' default "
            "[agent.resources] max_report_interval_s of 10 s)."
        ),
    )
    min_bw_bytes_s: float = Field(
        default=1024.0,
        ge=1.0,
        le=1e9,
        description=(
            "A probe reporting less bandwidth than this is refused (counted "
            "as failed, shown by compute network), and the planner never "
            "divides by less: every transfer estimate stays finite (D131)."
        ),
    )
    ewma_alpha: float = Field(
        default=0.3,
        gt=0.0,
        le=1.0,
        description="Weight of a new measurement in the stored EWMA.",
    )
    stale_after_s: float = Field(
        default=3600.0,
        ge=60.0,
        le=30 * 86_400.0,
        description=(
            "A link measured longer ago than this is stale: shown, but the "
            "estimator falls back to transfer history or the default bandwidth."
        ),
    )
    transfer_min_bytes: int = Field(
        default=1 * _MIB,
        ge=1,
        le=1024 * _MIB,
        description=(
            "Controller -> node bandwidth is the median of past controller "
            "transfers of at least this many bytes (smaller ones measure latency)."
        ),
    )
    transfer_history: int = Field(
        default=20,
        ge=1,
        le=1000,
        description="Most recent such transfers per node the median is taken over.",
    )

    @model_validator(mode="after")
    def _budget_fits_a_probe(self) -> NetworkSettings:
        worst = self.probe_bytes + self.probe_max_bytes
        if self.enabled and self.probe_budget_bytes_per_min < worst:
            raise ValueError(
                "probe_budget_bytes_per_min must be at least probe_bytes + "
                f"probe_max_bytes ({worst}), or no probe could ever run"
            )
        return self

    @model_validator(mode="after")
    def _links_outlive_the_interval(self) -> NetworkSettings:
        least = self.probe_interval_s * STALE_INTERVAL_MARGIN
        if self.stale_after_s < least:
            raise ValueError(
                f"stale_after_s ({self.stale_after_s:g}) must be at least "
                f"{STALE_INTERVAL_MARGIN:g} x probe_interval_s ({least:g}): a link "
                "is measured once per interval, so anything less leaves every "
                "link stale between probes"
            )
        return self


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


class TlsSettings(SectionModel):
    """``[controller.tls]`` - the controller's server certificate (V0.7 G1, D134).

    Issued from the fabric CA (``meshforge-controller fabric create``) at
    ``fabric create`` and at every controller start when missing or past 2/3 of
    its life. Its SANs are the fixed name ``meshforge-controller``, the
    ``grpc_bind`` / ``http_bind`` hosts (wildcards skipped), the
    ``[controller.api] advertise_url`` host and ``extra_sans``. Renewed while
    the controller runs, too (``check_interval_s``).
    """

    extra_sans: tuple[str, ...] = Field(
        default=(),
        max_length=32,
        description=(
            "More host names / IP addresses the controller is reached by (e.g. its "
            "LAN IP when it binds 0.0.0.0 with allow_any_bind, or a DNS name). No "
            "wildcards, no IPv6 zone ids (fe80::1%eth0)."
        ),
    )
    server_cert_days: int = Field(
        default=365,
        ge=1,
        le=825,
        description="Lifetime of the controller's server certificate, in days.",
    )
    check_interval_s: float = Field(
        default=3600.0,
        ge=1.0,
        le=86_400.0,
        description=(
            "How often the running controller re-checks its server certificate "
            "and renews it (at 2/3 of its life) or re-issues it (files damaged, "
            "SANs changed). New TLS handshakes use the new certificate at once; "
            "open connections keep theirs (V0.7 G3)."
        ),
    )

    @field_validator("extra_sans")
    @classmethod
    def _hosts_valid(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for host in value:
            try:
                validate_san_host(host)
            except PkiError as exc:
                raise ValueError(exc.message) from None
            if "%" in host:
                # A certificate cannot name an interface; refusing beats silently
                # dropping what the operator typed (bind hosts are stripped instead).
                raise ValueError(
                    f"{host.strip()!r} has an IPv6 zone id ('%...'), which a "
                    "certificate cannot hold; write the address without it"
                )
        return tuple(host.strip() for host in value)


def split_host_port(value: str) -> tuple[str, int]:
    """``host:port`` / ``[v6]:port`` with a DNS name or IP and port 1-65535."""
    raw = value.strip()
    if raw.startswith("["):
        host, sep, rest = raw[1:].partition("]")
        port_text = rest[1:] if sep and rest.startswith(":") else ""
    else:
        host, _, port_text = raw.rpartition(":")
        if ":" in host:
            host = ""
    if not host or not port_text.isdigit() or not 1 <= int(port_text) <= 65535:
        raise ValueError(
            f"{value!r} must be host:port (port 1-65535; bracket IPv6), e.g. "
            "192.168.1.10:47101"
        )
    try:
        validate_san_host(host)
    except PkiError as exc:
        raise ValueError(exc.message) from None
    return host, int(port_text)


class RevocationSettings(SectionModel):
    """``[controller.revocation]`` - node revocation (V0.7 G3, D138).

    ``node revoke`` over REST cuts the node's session at once; a revocation
    written by ``meshforge-controller node revoke`` (another process, straight
    into the database) and a re-enrolment's superseded certificate are picked
    up by a periodic sync of the revocation set.
    """

    poll_interval_s: float = Field(
        default=1.0,
        ge=0.1,
        le=60.0,
        description=(
            "How often the running controller re-reads the revocation set: the "
            "longest a CLI revocation waits before the node's session is cut "
            "and the agents are told (the REST path is immediate)."
        ),
    )


class RenewalSettings(SectionModel):
    """``[controller.renewal]`` - node certificate renewal (V0.7 G4, D140).

    Agents renew their node certificate over the mutual-TLS session port
    (``RenewCertificate``) at two thirds of its life; ``meshforge-agent
    rotate-key`` renews with a new key.
    """

    min_interval_s: float = Field(
        default=600.0,
        ge=1.0,
        le=86_400.0,
        description=(
            "Fewest seconds between two certificates issued to one node "
            "(a misbehaving or stolen-key agent cannot mint certificates in a "
            "loop); a renewal sooner is refused rate_limited and retried."
        ),
    )
    node_cert_lifetime_s: float = Field(
        default=30 * 86_400.0,
        ge=60.0,
        le=365 * 86_400.0,
        description=(
            "Life of a renewed node certificate (default 30 days, D136). Short "
            "certificates make revocation cheap; agents renew at 2/3 of it."
        ),
    )


class AuthSettings(SectionModel):
    """``[controller.auth]`` - REST users and API tokens (V0.7 G4, D141).

    Users are created on the controller host (``meshforge-controller user
    create``); ``compute login`` exchanges a name and password for an API token.
    """

    legacy_fabric_token: bool = Field(
        default=False,
        description=(
            "Also accept the old fabric token (D44) on the REST API, as an admin "
            "with no user: one release of overlap for scripts not yet moved to "
            "`compute login`. Each run audits its first use and logs a warning "
            "at most once a minute. Removed in V0.8."
        ),
    )
    token_ttl_days: int = Field(
        default=90,
        ge=1,
        le=365,
        description="Life of an API token issued by a login (D141: 90 days).",
    )
    login_rate_per_minute: float = Field(
        default=10.0,
        gt=0,
        le=600,
        description=(
            "Login attempts allowed per source IP (IPv6: per /64) per minute; "
            "more are answered 429 before any password is checked."
        ),
    )
    login_rate_burst: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Login attempts a source may make at once before the rate applies.",
    )
    login_free_failures: int = Field(
        default=5,
        ge=1,
        le=100,
        description=(
            "Consecutive failed logins for one user name before backoff starts."
        ),
    )
    login_backoff_base_s: float = Field(
        default=1.0,
        gt=0,
        le=60.0,
        description=(
            "First backoff after the free failures; doubles with each further "
            "failure up to login_backoff_max_s. A success clears it."
        ),
    )
    login_backoff_max_s: float = Field(
        default=300.0,
        gt=0,
        le=3600.0,
        description="Longest a user name is blocked after repeated failures.",
    )
    max_concurrent_hashes: int = Field(
        default=4,
        ge=1,
        le=64,
        description=("Password checks (scrypt, ~32 MiB each) run at once; more wait."),
    )
    session_idle_hours: float = Field(
        default=12.0,
        ge=0.25,
        le=168.0,
        description=(
            "V0.8 dashboard (D170): a browser session not used for this long "
            "has ended (the next request or the open event stream is refused "
            "and the user logs in again). Measured to the minute."
        ),
    )
    session_max_days: int = Field(
        default=7,
        ge=1,
        le=30,
        description=(
            "Absolute life of a browser session (D170), however busy: after "
            "this the dashboard asks for the password again."
        ),
    )

    @model_validator(mode="after")
    def _idle_within_life(self) -> AuthSettings:
        if self.session_idle_hours > self.session_max_days * 24:
            raise ValueError(
                "[controller.auth] session_idle_hours cannot exceed "
                "session_max_days (in hours)"
            )
        return self

    stream_recheck_interval_s: float = Field(
        default=1.0,
        ge=0.1,
        le=60.0,
        description=(
            "How often open log streams (WebSocket follows) re-check their API "
            "token - one query for all of them: a revoked or expired token or a "
            "disabled user closes the stream within this time (D157). A revoke "
            "through this controller's REST API closes it at once."
        ),
    )


class EnrollSettings(SectionModel):
    """``[controller.enroll]`` - where laptops join (V0.7 G2, D135/D150).

    G2 serves ``Enroll`` on its **own TLS listener** (server cert from the
    fabric CA, no client certificate asked) while the session port stays
    plaintext; V0.7 G3 folds it into the session port. The listener starts only
    when a fabric CA exists (``meshforge-controller fabric create``).
    """

    enabled: bool = Field(
        default=True, description="Serve Enroll (when a fabric CA exists)."
    )
    bind: str | None = Field(
        default=None,
        description=(
            "ip:port of the enrolment listener. Empty = the grpc_bind host on "
            f"port {DEFAULT_ENROLL_PORT}. Same LAN/loopback rule as grpc_bind."
        ),
    )
    advertise: str | None = Field(
        default=None,
        description=(
            "host:port written into join codes. Empty = the bind address (a "
            "wildcard bind then needs this, or `invite --address`)."
        ),
    )
    invite_ttl_s: int = Field(
        default=900,
        ge=60,
        le=86_400,
        description="Default life of a join code (D135: 15 min; 1 min - 24 h).",
    )
    rate_per_minute: float = Field(
        default=10.0,
        gt=0,
        le=600,
        description=(
            "Enrol attempts allowed per source IP per minute (token bucket); "
            "more are refused RESOURCE_EXHAUSTED without touching the database."
        ),
    )
    rate_burst: int = Field(
        default=5,
        ge=1,
        le=100,
        description="Attempts a source IP may make at once before the rate applies.",
    )
    max_tracked_sources: int = Field(
        default=4096,
        ge=16,
        le=1_000_000,
        description=(
            "Source IPs the rate limiter remembers (oldest forgotten first; a "
            "forgotten IP starts with a full bucket)."
        ),
    )

    @field_validator("bind")
    @classmethod
    def _bind_shape(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parse_bind(value, allow_any=True)
        return value.strip()

    @field_validator("advertise")
    @classmethod
    def _advertise_shape(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        split_host_port(value)
        return value.strip()


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


LOOPBACK_PLAIN_HOSTS: Final = frozenset({"127.0.0.1", "::1"})


class HttpSettings(SectionModel):
    """``[controller.http]`` - extra REST listeners (V0.8 G1, brief Q1)."""

    loopback_plain: str | None = Field(
        default=None,
        description=(
            "Also serve the REST API and dashboard in plain HTTP on this "
            "loopback address (``127.0.0.1:47111`` or ``[::1]:47111``) for a "
            "browser on the controller host itself, which then needs no CA "
            "import. Only 127.0.0.1 or ::1 are accepted - never a LAN or "
            "wildcard address, never a host name. Off by default."
        ),
    )

    @field_validator("loopback_plain")
    @classmethod
    def _loopback_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        bind = parse_bind(value, allow_any=False)
        if bind.host not in LOOPBACK_PLAIN_HOSTS:
            raise ValueError(
                "[controller.http] loopback_plain must bind 127.0.0.1 or [::1] "
                f"(plain HTTP never leaves this machine), got {value}"
            )
        return value


class SetupSettings(SectionModel):
    """``[controller.setup]`` - the one-time "create fabric" page (V0.17 G2, D392).

    A controller in ``mtls`` mode with no fabric CA, no users and a setup token
    (``<data_dir>/controller/setup-token``, made by ``meshforge-controller
    setup url`` - the package's postinst runs it) serves only the setup page,
    in plain HTTP on this loopback address, until the fabric is created; then it
    restarts itself into the normal mode.
    """

    bind: str | None = Field(
        default=None,
        description=(
            "Loopback ip:port of the setup page (127.0.0.1 or [::1] only). "
            "Default: 127.0.0.1 with the http_bind port (47110)."
        ),
    )

    @field_validator("bind")
    @classmethod
    def _loopback_only(cls, value: str | None) -> str | None:
        if value is None:
            return None
        bind = parse_bind(value, allow_any=False)
        if bind.host not in LOOPBACK_PLAIN_HOSTS:
            raise ValueError(
                "[controller.setup] bind must be 127.0.0.1 or [::1] (the setup "
                f"page is plain HTTP and never leaves this machine), got {value}"
            )
        return value


class EventsSettings(SectionModel):
    """``[controller.events]`` - the dashboard's live event stream (V0.8 G1,
    D172): ``/v1/events/ws``. All in memory; lost on restart (clients resync)."""

    ring_size: int = Field(
        default=10_000,
        ge=100,
        le=1_000_000,
        description=(
            "Events kept for reconnecting clients to resume from (``since``); "
            "an older resume point gets ``resync``."
        ),
    )
    queue_size: int = Field(
        default=1_000,
        ge=10,
        le=100_000,
        description=(
            "Events queued per connected client; a client that falls this far "
            "behind is closed 4008 slow_consumer (it reconnects and resumes)."
        ),
    )
    max_subscribers: int = Field(
        default=256, ge=1, le=10_000, description="Open event streams at once."
    )
    watch_interval_s: float = Field(
        default=0.25,
        gt=0,
        le=2.0,
        description=(
            "How often node status and live metrics are diffed into events "
            "(adds at most this much delay)."
        ),
    )
    metrics_interval_s: float = Field(
        default=1.0,
        ge=0.5,
        le=60.0,
        description="At most one node.metrics event per node per this many seconds.",
    )


class TelemetrySettings(SectionModel):
    """``[controller.telemetry]`` - run records and the dataset export (V0.9 G1,
    D211/D212). A record is written for every terminal attempt by a sweep."""

    record_interval_s: float = Field(
        default=2.0,
        ge=0.1,
        le=60.0,
        description=(
            "How often terminal attempts without a run record are looked for "
            "(an attempt change also wakes the sweep)."
        ),
    )
    record_batch: int = Field(
        default=200,
        ge=1,
        le=5_000,
        description="Records written per sweep transaction (a backlog sweeps again).",
    )
    export_max_rows: int = Field(
        default=100_000,
        ge=1,
        le=10_000_000,
        description=(
            "Most run records one Parquet export may hold. The export is "
            "buffered in memory (~2.5 KB per record plus the file)."
        ),
    )


class BenchSettings(SectionModel):
    """``[controller.bench]`` - the unattended campaign runner (V0.9 G5,
    D219/D246; docs/OPERATIONS.md "Running a campaign unattended")."""

    runner: bool = Field(
        default=True,
        description="Run started campaigns in the background (off = manual submit only).",
    )
    tick_s: float = Field(
        default=2.0,
        ge=0.05,
        le=300.0,
        description="How often the runner settles finished runs and submits new ones.",
    )
    max_submit_per_tick: int = Field(
        default=4,
        ge=1,
        le=64,
        description=(
            "Most runs one tick submits (all campaigns together): small budgets "
            "keep a tick short however large the campaign."
        ),
    )
    max_examine_per_tick: int = Field(
        default=32,
        ge=1,
        le=2_000,
        description="Most planned runs one tick examines (deferred / skipped ones count).",
    )
    quality_cv_threshold: float = Field(
        default=0.10,
        gt=0.0,
        le=10.0,
        description=(
            "The data quality report flags a configuration whose total time "
            "varies more than this (coefficient of variation, std / mean)."
        ),
    )
    orphan_scan_s: float = Field(
        default=30.0,
        ge=0.0,
        le=3_600.0,
        description=(
            "Longest gap between two scans for orphan benchmark jobs (a job "
            "created for a run that was cancelled meanwhile); a cancel or a "
            "skipped run triggers a scan at once. 0 = scan on every tick."
        ),
    )


class SplitSettings(SectionModel):
    """``[controller.split]`` - Mode 3 task splitting (V0.10, D275). G1 uses the
    item caps; later V0.10 groups add the planner/queue knobs here."""

    max_items: int = Field(
        default=MAX_SPLIT_ITEMS,
        ge=1,
        le=MAX_SPLIT_ITEMS,
        description=(
            "Most items one split job may resolve to (split.over); more is "
            "refused at submit as invalid_input."
        ),
    )
    max_chunks: int = Field(
        default=MAX_SPLIT_CHUNKS,
        ge=1,
        le=MAX_SPLIT_CHUNKS,
        description=(
            "Most chunks one split job may have; a fixed split.chunk.items that "
            "makes more is refused at submit."
        ),
    )
    # --- V0.10 G3/G4 (D265/D275): the planner's Mode 3 knobs ---------------
    split_margin: float = Field(
        default=0.10,
        ge=0.0,
        lt=1.0,
        description=(
            "Split only when the best node set is predicted at least this "
            "fraction faster than the best single node."
        ),
    )
    target_chunk_s: float = Field(
        default=20.0,
        gt=0.0,
        le=3600.0,
        description="Auto chunk size: about this many seconds on the fastest node.",
    )
    max_overhead_frac: float = Field(
        default=0.05,
        gt=0.0,
        lt=1.0,
        description="Auto chunks grow until per-chunk overhead is at most this share.",
    )
    min_chunks_per_node: int = Field(
        default=4,
        ge=1,
        le=1000,
        description="Auto chunks shrink until there are this many per lane.",
    )
    chunk_overhead_s: float = Field(
        default=1.5,
        ge=0.0,
        le=600.0,
        description="Assumed per-chunk overhead until chunk history measures it.",
    )
    tail_frac: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="Expected tail of a set: this share of its longest chunk.",
    )
    # --- V0.10 G4 (D263/D267): lanes and chunk leases ----------------------
    visibility_timeout_s: float = Field(
        default=60.0,
        ge=5.0,
        le=86_400.0,
        description=(
            "A chunk lease expires this long after it was granted or last "
            "renewed (the lane renews every third of it); an expired chunk goes "
            "back to pending - a partitioned or hung lane cannot hold it."
        ),
    )
    wait_retry_s: float = Field(
        default=2.0,
        gt=0.0,
        le=60.0,
        description=(
            "NoWork(wait): how long an idle lane waits before asking again "
            "while every remaining chunk is leased elsewhere."
        ),
    )
    max_unverified_results: int = Field(
        default=2,
        ge=0,
        le=100,
        description=(
            "Done results of one chunk whose digest the lane's upload grant "
            "never stored, tolerated per (chunk, lane) without counting (a "
            "controller restart forgets grant history); the next one is a "
            "counted, retryable chunk failure (chunk_unverified)."
        ),
    )
    max_failed_nodes: int = Field(
        default=2,
        ge=1,
        le=64,
        description=(
            "A chunk that failed on this many different nodes fails the job "
            "(D267c); retry.max_attempts bounds the tries of one chunk too."
        ),
    )
    # --- V0.10 G6 (D266): speculative copies of straggling chunks -----------
    speculative_max_frac: float = Field(
        default=0.10,
        ge=0.0,
        le=1.0,
        description=(
            "At most this share of a split job's chunks ever get a speculative "
            "copy (counted in chunks, not copies; 0 = never). Fixed when the "
            "job's chunk queue is created; split.speculative: false turns it "
            "off per job."
        ),
    )
    speculation_factor: float = Field(
        default=1.5,
        ge=1.0,
        le=100.0,
        description=(
            "An idle lane copies a straggling chunk only when the chunk's "
            "predicted remaining time on its node exceeds this many times the "
            "idle node's expected time for it."
        ),
    )
    # --- V0.10 G5 fix r1 (B1): a combine refused resource_busy ---------------
    combine_busy_backoff_s: float = Field(
        default=1.0,
        ge=0.0,
        le=60.0,
        description=(
            "A combine the node refused resource_busy (still at its attempt "
            "limit, e.g. the job's own lanes ending) is placed again after this "
            "fixed wait; the refusal is not counted against retry.max_attempts "
            "and does not steer the combine away from that node."
        ),
    )
    combine_busy_max: int = Field(
        default=100,
        ge=0,
        le=10_000,
        description=(
            "Free resource_busy refusals of one job's combine; later ones count "
            "against retry.max_attempts like any retryable failure (a job has "
            "no deadline of its own that would end an endless refusal)."
        ),
    )
    # --- V0.10 G7 (D270): progress per chunk / node ------------------------
    progress_event_hz: float = Field(
        default=2.0,
        ge=0.1,
        le=20.0,
        description=(
            "Most split.progress events per second per split job (changes in "
            "between are coalesced into the next one; the last state is always "
            "sent)."
        ),
    )
    progress_resync_s: float = Field(
        default=5.0,
        ge=0.5,
        le=300.0,
        description=(
            "Every this many seconds the progress of every running split job is "
            "re-read even without a chunk message (catches lease expiry and any "
            "change that sent no notice); an unchanged job publishes nothing."
        ),
    )
    progress_rate_window: int = Field(
        default=8,
        ge=2,
        le=64,
        description=(
            "A lane's items/s is measured over its last this-many committed "
            "chunks (the ETA divides the remaining items by the live lanes' sum)."
        ),
    )
    progress_max_jobs: int = Field(
        default=1_000,
        ge=1,
        le=100_000,
        description=(
            "Most split jobs whose last published progress is kept in memory "
            "(running jobs; least recently changed dropped first - a dropped job "
            "is simply re-read on its next change)."
        ),
    )
    # --- V0.10 G8 (D261/D271/D285): chunk records and caches ----------------
    chunk_stderr_tail_bytes: int = Field(
        default=16_384,
        ge=0,
        le=65_536,
        description=(
            "Most bytes of a failed chunk try's stderr tail kept with its chunk "
            "record (the agent sends at most [agent.split] "
            "chunk_stderr_tail_bytes, already redacted); 0 = keep none."
        ),
    )
    chunk_stderr_ok_bytes: int = Field(
        default=2_048,
        ge=0,
        le=65_536,
        description=(
            "Most bytes of a successful chunk try's stderr tail kept (a job of "
            "10 000 chunks keeps at most 10 000 x this); 0 = keep none."
        ),
    )
    speculation_cache_s: float = Field(
        default=5.0,
        ge=0.0,
        le=60.0,
        description=(
            "An idle lane's speculation check reuses a split job's per-node "
            "chunk times for up to this long while no chunk of the job "
            "committed (a commit always invalidates them); 0 = read every time."
        ),
    )
    speculation_cache_jobs: int = Field(
        default=256,
        ge=1,
        le=100_000,
        description="Most split jobs whose per-node chunk times are cached.",
    )


class DbMode(StrEnum):
    """``[controller.db] mode`` (V0.17 G2, D390)."""

    MANAGED = "managed"
    """The controller runs its own bundled PostgreSQL 16 as a child process
    (:mod:`meshforge.controller.db_managed`): data under ``root``, loopback +
    unix socket only, generated passwords. The default of a packaged install."""
    EXTERNAL = "external"
    """Connect to a PostgreSQL someone else runs, with ``[database]`` (the dev
    Compose setup, power users). The default of a developer checkout."""


def _default_db_mode() -> DbMode:
    """Managed in a packaged (system) install, external otherwise (D390). The
    install mode is the unit's explicit ``MESHFORGE_INSTALL_MODE`` (Q11)."""
    if install_mode() is InstallMode.SYSTEM:
        return DbMode.MANAGED
    return DbMode.EXTERNAL


DEFAULT_MANAGED_DB_PORT = 47150
"""Q22: the managed PostgreSQL's loopback port - never 5432, so a system
PostgreSQL (or the dev Compose one) on the same machine never clashes."""


class DbSettings(SectionModel):
    """``[controller.db]`` - where the controller's database comes from (D390).

    In ``managed`` mode ``[database]`` keeps only its pool and timeout settings;
    the connection itself (host, port, users, passwords) is the managed
    cluster's, and setting ``[database] url`` or ``password`` there is refused
    (it would silently be ignored).
    """

    mode: DbMode = Field(
        default_factory=_default_db_mode,
        description=(
            "managed: run the bundled PostgreSQL (packaged default); external: "
            "connect with [database] (developer default)."
        ),
    )
    port: int = Field(
        default=DEFAULT_MANAGED_DB_PORT,
        ge=1,
        le=65535,
        description="Managed mode: the PostgreSQL TCP port on 127.0.0.1.",
    )
    root: Path | None = Field(
        default=None,
        description=(
            "Managed mode: the cluster's folder (data, passwords, logs, backups). "
            "Default <data_dir>/db (/var/lib/meshforge/db for the package)."
        ),
    )
    socket_dir: Path | None = Field(
        default=None,
        description=(
            "Managed mode: the unix socket folder (0700). Default <root>/run; the "
            "socket path must stay under 107 bytes."
        ),
    )
    bin_dir: Path | None = Field(
        default=None,
        description=(
            "Managed mode: PostgreSQL's bin folder. Default: pgsql/bin of the "
            "controller bundle this process runs from (a developer checkout must "
            "set it, e.g. to a tree made by scripts/build_bundle.py)."
        ),
    )
    start_timeout_s: float = Field(
        default=60.0,
        gt=0,
        le=3600,
        description="Managed mode: how long PostgreSQL may take to accept connections.",
    )
    backups_keep: int = Field(
        default=3,
        ge=1,
        le=100,
        description=(
            "Managed mode (D393): pre-upgrade backups kept in <root>/backups "
            "(pre-<version>.dump); older ones are deleted."
        ),
    )
    health_timeout_s: float = Field(
        default=60.0,
        gt=0,
        le=3600,
        description=(
            "Managed mode (D393): after an upgrade's migrations, the controller "
            "must be serving within this, else the pre-upgrade backup is "
            "restored and it exits non-zero."
        ),
    )

    @field_validator("root", "socket_dir", "bin_dir", mode="before")
    @classmethod
    def _dir_not_empty(cls, value: object) -> object:
        return _non_empty_path(value)

    @field_validator("socket_dir", "bin_dir", "root")
    @classmethod
    def _absolute(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError(f"must be an absolute path, got {value}")
        return value


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
    network: NetworkSettings = Field(default_factory=NetworkSettings)
    api: ApiSettings = Field(default_factory=ApiSettings)
    tls: TlsSettings = Field(default_factory=TlsSettings)
    enroll: EnrollSettings = Field(default_factory=EnrollSettings)
    revocation: RevocationSettings = Field(default_factory=RevocationSettings)
    renewal: RenewalSettings = Field(default_factory=RenewalSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    events: EventsSettings = Field(default_factory=EventsSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    setup: SetupSettings = Field(default_factory=SetupSettings)
    telemetry: TelemetrySettings = Field(default_factory=TelemetrySettings)
    bench: BenchSettings = Field(default_factory=BenchSettings)
    split: SplitSettings = Field(default_factory=SplitSettings)
    db: DbSettings = Field(default_factory=DbSettings)

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
        parse_bind(self.enroll_bind, allow_any=self.allow_any_bind)
        return self

    @property
    def enroll_bind(self) -> str:
        """``[controller.enroll] bind`` or ``<grpc_bind host>:47101`` (D150)."""
        if self.enroll.bind is not None:
            return self.enroll.bind
        host = parse_bind(self.grpc_bind, allow_any=True).host
        host = f"[{host}]" if ":" in host else host
        return f"{host}:{DEFAULT_ENROLL_PORT}"

    def accepts_protocol_version(self, version: int) -> bool:
        lo, hi = self.accepted_protocol_versions
        return lo <= version <= hi


_CONNECTION_FIELDS: Final = frozenset(
    {"url", "host", "port", "name", "user", "password"}
)
"""``[database]`` fields that managed mode replaces (V0.17 G2)."""


class ControllerSettings(MeshForgeSettings):
    """Everything the controller process needs (``[controller]`` + ``[database]``)."""

    controller: ControllerSection = Field(default_factory=ControllerSection)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)

    @model_validator(mode="after")
    def _insecure_dev_is_loopback_only(self) -> ControllerSettings:
        """D139: ``[security] mode = "insecure-dev"`` serves plaintext, so it is
        refused unless every listener (and the advertised URL) is loopback."""
        cfg = self.controller
        if not self.security.insecure:
            url = cfg.api.advertise_url
            if url is not None and not url.startswith("https://"):
                raise ValueError(
                    "[controller.api] advertise_url must be https:// in [security] "
                    f"mode = 'mtls' (the blob API is HTTPS), got {url}"
                )
            return self
        if cfg.allow_any_bind:
            raise ValueError(
                "[security] mode = 'insecure-dev' cannot be combined with "
                "allow_any_bind (plaintext must stay on this machine)"
            )
        for name, bind in (
            ("grpc_bind", cfg.grpc_bind),
            ("http_bind", cfg.http_bind),
            ("enroll.bind", cfg.enroll_bind),
        ):
            host = parse_bind(bind, allow_any=True).host
            if not is_loopback_host(host):
                raise ValueError(
                    f"[security] mode = 'insecure-dev' needs loopback-only listeners; "
                    f"[controller] {name} is {bind} - use 127.0.0.1, or mode = 'mtls'"
                )
        url = cfg.api.advertise_url
        if url is not None:
            host = url.split("://", 1)[-1].rsplit(":", 1)[0]
            if not is_loopback_host(host):
                raise ValueError(
                    "[security] mode = 'insecure-dev' needs a loopback "
                    f"[controller.api] advertise_url, got {url}"
                )
        return self

    @model_validator(mode="after")
    def _managed_db_owns_the_connection(self) -> ControllerSettings:
        """V0.17 G2: in managed mode the connection is the managed cluster's, so
        a configured ``[database]`` connection would be silently ignored -
        refused instead (pool and timeout settings stay usable)."""
        if self.controller.db.mode is not DbMode.MANAGED:
            return self
        given = sorted(_CONNECTION_FIELDS & self.database.model_fields_set)
        if given:
            raise ValueError(
                "[controller.db] mode = 'managed' runs its own PostgreSQL, so "
                f"[database] {', '.join(given)} would be ignored; remove them "
                "(or MESHFORGE_DATABASE__...), or use mode = 'external'"
            )
        return self

    @property
    def managed_db(self) -> bool:
        """True when the controller runs its own PostgreSQL (D390)."""
        return self.controller.db.mode is DbMode.MANAGED

    @property
    def db_root(self) -> Path:
        """The managed cluster's root (``[controller.db] root`` or
        ``<data_dir>/db``)."""
        return self.controller.db.root or (self.data_dir / DB_DIR_NAME)

    def check_database_password(self) -> None:
        """D145 (d): a controller outside ``insecure-dev`` refuses to start with
        the published development database password (anyone who has read the
        repository knows it). Called at start-up, not as a validator, so tools
        and tests that only *read* settings are unaffected. Managed mode
        (V0.17 G2) generates its passwords: nothing to check."""
        if self.managed_db:
            return
        if self.security.insecure or not self.database.uses_dev_password():
            return
        raise ConfigError(
            "the database password is the published development default "
            "'meshforge-dev'; a controller in [security] mode = 'mtls' refuses to "
            "start with it. Give the database a strong password (deploy/.env "
            "MESHFORGE_DEV_DB_PASSWORD before its first start, or ALTER ROLE "
            "meshforge PASSWORD '...') and the controller the same one "
            "(MESHFORGE_DATABASE__PASSWORD or [database] password; "
            "docs/OPERATIONS.md 0a)",
            details={"reason": "dev_db_password"},
        )

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
    def secrets_key_path(self) -> Path:
        """The job-secrets master key (``<data_dir>/controller/secrets.key``,
        V0.7 G6 D144): 32 random bytes, created on first use; losing it loses
        every stored secret (back it up with ``ca/``)."""
        return self.data_dir / CONTROLLER_DIR_NAME / SECRETS_KEY_FILE_NAME

    @property
    def ca_dir(self) -> Path:
        """The fabric CA (``<data_dir>/controller/ca``, V0.7 D134)."""
        return self.data_dir / CONTROLLER_DIR_NAME / CA_DIR_NAME

    @property
    def tls_dir(self) -> Path:
        """The controller's server key + cert (``<data_dir>/controller/tls``)."""
        return self.data_dir / CONTROLLER_DIR_NAME / TLS_DIR_NAME

    @property
    def server_cert_hosts(self) -> list[str]:
        """SAN hosts of the controller's server certificate (``[controller.tls]``)."""
        cfg = self.controller
        return server_cert_hosts(
            [cfg.grpc_bind, cfg.http_bind, cfg.enroll_bind],
            advertise_url=cfg.api.advertise_url,
            extra=cfg.tls.extra_sans,
        )

    @property
    def setup_token_path(self) -> Path:
        """V0.17 G2 (D392): the one-time setup token
        (``<data_dir>/controller/setup-token``)."""
        return self.data_dir / CONTROLLER_DIR_NAME / SETUP_TOKEN_NAME

    @property
    def setup_bind(self) -> str:
        """Where the setup page listens (``[controller.setup] bind`` or
        127.0.0.1 with the http_bind port)."""
        configured = self.controller.setup.bind
        if configured is not None:
            return configured
        port = parse_bind(self.controller.http_bind, allow_any=True).port
        return f"127.0.0.1:{port or DEFAULT_HTTP_PORT}"

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
