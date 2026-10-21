"""``run_record`` + ``chunk_record``: telemetry schema v3 (V0.9 G1, D211/D212,
ADR-0018; migration 0021; v2 columns ``result_schema_version``, ``condition``,
``condition_measured``: V0.9 G4, migration 0023; v3 - the Mode 3 columns and
the ``chunk_record`` table: V0.10 G8, D271, migration 0029).

One row per **terminal** attempt - every job, benchmark or not (real usage is
data too). Columns are declared in :mod:`meshforge.controller.telemetry.schema`
(the single list, with their meaning); this model must match it exactly and a
unit test checks that. Rows are written once, by the telemetry recorder's
idempotent insert (``ON CONFLICT (attempt_id) DO NOTHING``), and never updated:
the record is what was known when the attempt ended.

No foreign keys on purpose: a record outlives the operational rows it was built
from (a job deleted by a later retention policy must not delete the dataset).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = ["STDERR_TAIL_CHARS", "ChunkRecord", "RunRecord"]

_SHORT: Final = 64
"""Short words (states, classes, families, policies)."""
_CONDITION: Final = 128
"""A benchmark condition's canonical string (``netem:delay=50ms,loss=1%``);
literal copy of ``telemetry.record.CONDITION_CHARS`` (a unit test holds them
equal)."""


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


# Literal copies of telemetry.schema's tuples: the model must not import the
# telemetry package (the store stays the bottom layer); the schema test checks
# they are equal.
_FAILURE_CLASSES: Final = (
    "ok",
    "workload",
    "infrastructure",
    "condition_failed",
    "unavailable",
)
_STRATEGIES: Final = ("local", "mode1", "mode3")
_ROLES: Final = ("run", "lane", "combine")
_CHUNK_OUTCOMES: Final = (
    "committed",
    "lost",
    "failed",
    "superseded",
    "released",
    "expired",
)
_DIGEST: Final = 71
"""``sha256:`` + 64 hex."""
STDERR_TAIL_CHARS: Final = 65_536
"""Most characters of a chunk try's stored stderr tail (``[controller.split]
chunk_stderr_tail_bytes`` is at most this; literal copy in migration 0029)."""


class RunRecord(Base):
    __tablename__ = "run_record"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="schema_version"),
        CheckConstraint(
            f"failure_class IN ({_in(_FAILURE_CLASSES)})", name="failure_class"
        ),
        CheckConstraint(f"strategy IN ({_in(_STRATEGIES)})", name="strategy"),
        CheckConstraint("placement_mode IN ('pinned', 'auto')", name="placement_mode"),
        CheckConstraint(
            f"attempt_role IS NULL OR attempt_role IN ({_in(_ROLES)})",
            name="attempt_role",
        ),
        CheckConstraint(
            f"planner_strategy IS NULL OR planner_strategy IN ({_in(_STRATEGIES)})",
            name="planner_strategy",
        ),
        Index("ix_run_record_campaign", "campaign_id", "config_id", "repetition"),
        Index("ix_run_record_recorded_at", "recorded_at"),
    )

    # identity
    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    attempt_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_role: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    campaign_id: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    config_id: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    repetition: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    # placement
    node_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    submitter_node_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH), nullable=True
    )
    strategy: Mapped[str] = mapped_column(String(_SHORT), nullable=False)
    placement_mode: Mapped[str] = mapped_column(String(_SHORT), nullable=False)
    policy: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    planner_node_id: Mapped[str | None] = mapped_column(
        String(ID_MAX_LENGTH), nullable=True
    )
    planner_estimate_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    planner_strategy: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    planner_node_set: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # workload
    workload_family: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    workload_params: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    workload_seed: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    workload_status: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    workload_device: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    result_schema_version: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_n: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_repeats: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_size_mb: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_batch: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_width: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_depth: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_seq_len: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    p_dtype: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    env_base: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    gpu_request: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    runtime: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    # benchmark condition (v2)
    condition: Mapped[str | None] = mapped_column(String(_CONDITION), nullable=True)
    condition_measured: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # split job (v3)
    split_node_set: Mapped[list[str] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    split_items: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    split_chunk_items: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    split_chunks: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    split_predicted_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    split_makespan_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    split_job_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    split_combine_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    split_ideal_bound_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # start snapshot
    node_state: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    node_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    node_cpu_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    node_ram_free_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    node_ram_total_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    node_gpu_util_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    node_gpu_vram_free_bytes: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True
    )
    node_sample_age_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    node_live_attempts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    node_cpu_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    node_gpu_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    cache_inputs: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_warm_inputs: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cache_state: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    cache_detail: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    net_source: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    net_rtt_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_bw_bytes_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_samples: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # phases
    queue_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stage_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    env_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    run_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    compute_ns: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    compute_iterations: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    collect_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    total_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # bytes
    bytes_in: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bytes_in_cached: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bytes_out: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # outcome
    attempt_state: Mapped[str] = mapped_column(String(_SHORT), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    failure_type: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    failure_class: Mapped[str] = mapped_column(String(_SHORT), nullable=False)
    # clocks
    clock_offset_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    clock_offset_bound_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    clock_consistent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # ordering only
    attempt_created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    attempt_ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class ChunkRecord(Base):
    """One chunk lease's end (telemetry schema v3, V0.10 G8, D271): written by
    :class:`~meshforge.controller.split.store.ChunkStore` in the transaction
    that applied the chunk's result (checklist 35); columns declared in
    :data:`meshforge.controller.telemetry.schema.CHUNK_COLUMNS` (same order) plus
    ``stderr_tail`` (operational, not exported). No foreign keys, as
    ``run_record``: a record outlives the queue rows it came from."""

    __tablename__ = "chunk_record"
    __table_args__ = (
        CheckConstraint("schema_version >= 3", name="schema_version"),
        CheckConstraint(f"outcome IN ({_in(_CHUNK_OUTCOMES)})", name="outcome"),
        CheckConstraint(
            f"failure_class IN ({_in(_FAILURE_CLASSES)})", name="failure_class"
        ),
        CheckConstraint("chunk_index >= 0 AND lease_seq >= 1", name="lease"),
        CheckConstraint("items >= 1 AND first_item >= 0", name="items"),
        CheckConstraint(
            f"stderr_tail IS NULL OR length(stderr_tail) <= {STDERR_TAIL_CHARS}",
            name="stderr_tail",
        ),
        Index("ix_chunk_record_recorded_at", "recorded_at"),
    )

    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    job_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), primary_key=True)
    chunk_index: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lease_seq: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    lane_attempt_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    node_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    first_item: Mapped[int] = mapped_column(BigInteger, nullable=False)
    items: Mapped[int] = mapped_column(BigInteger, nullable=False)
    speculative: Mapped[bool] = mapped_column(Boolean, nullable=False)
    won: Mapped[bool] = mapped_column(Boolean, nullable=False)
    outcome: Mapped[str] = mapped_column(String(_SHORT), nullable=False)
    failure_type: Mapped[str | None] = mapped_column(String(_SHORT), nullable=True)
    failure_class: Mapped[str] = mapped_column(String(_SHORT), nullable=False)
    exit_code: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_digest: Mapped[str | None] = mapped_column(String(_DIGEST), nullable=True)
    bytes_in: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    bytes_out: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    stage_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    env_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    run_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    upload_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    leased_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recorded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    stderr_tail: Mapped[str | None] = mapped_column(Text, nullable=True)
    """The try's chunk-process stderr tail (redacted on the agent, bounded by
    ``[controller.split] chunk_stderr_tail_bytes``) - for ``GET
    /v1/jobs/{id}/chunks/{index}``; never in the dataset."""
