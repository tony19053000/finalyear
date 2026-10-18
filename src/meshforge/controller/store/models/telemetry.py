"""``run_record``: telemetry schema v2 (V0.9 G1, D211/D212, ADR-0018; migration 0021;
v2 columns ``result_schema_version``, ``condition``, ``condition_measured``:
V0.9 G4, migration 0023).

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
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = ["RunRecord"]

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
_STRATEGIES: Final = ("local", "mode1")


class RunRecord(Base):
    __tablename__ = "run_record"
    __table_args__ = (
        CheckConstraint("schema_version >= 1", name="schema_version"),
        CheckConstraint(
            f"failure_class IN ({_in(_FAILURE_CLASSES)})", name="failure_class"
        ),
        CheckConstraint(f"strategy IN ({_in(_STRATEGIES)})", name="strategy"),
        CheckConstraint("placement_mode IN ('pinned', 'auto')", name="placement_mode"),
        Index("ix_run_record_campaign", "campaign_id", "config_id", "repetition"),
        Index("ix_run_record_recorded_at", "recorded_at"),
    )

    # identity
    schema_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    attempt_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
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
