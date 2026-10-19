"""``bench_campaign`` and ``bench_run``: benchmark campaigns (V0.9 G3, D216/D219; migration 0022).

A **campaign** is a validated spec (YAML kept verbatim, plus the digest of its
canonical form) expanded **once**, at creation, against the node list of that
moment (:func:`meshforge.bench.campaign.expand`): one ``bench_run`` row per
planned run, numbered ``seq`` 1..n in execution order. The rows are the
campaign's whole state - G3 submits the next ``k`` planned runs on request; G5's
resumable runner drives the same columns - so a controller restart loses
nothing.

Run states: ``planned`` (not submitted) -> ``submitted`` (a job exists,
``job_id`` set) -> ``done`` / ``failed`` (its job ended); ``skipped`` = the run
was refused at submission (its node gone, not accepted) and will not run -
``detail`` says why. ``job_id`` is unique: the benchmark identity of a job *is*
this link (D232; no column on ``job``), so a job belongs to at most one run.
It has no foreign key on purpose, like ``run_record``: the campaign's history
outlives job rows a retention policy may delete.

V0.9 G5 (migration 0024): a campaign whose ``started_at`` is set is **owned by
the runner** (:mod:`meshforge.controller.bench.runner`); it may be ``paused``;
its throttle is ``max_concurrent_per_node`` / ``cooldown_s``; an infrastructure
failure puts the run back to ``planned`` with ``tries + 1`` while ``tries <
max_infra_retries``, an owner pre-emption with ``tries`` unchanged - both bump
``generation`` (the job key suffix ``bench:<run_id>:<generation>``, so one
generation is submitted once). The old job keeps its run identity by that key.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from meshforge.controller.store.base import ID_MAX_LENGTH, Base

__all__ = [
    "CAMPAIGN_STATES",
    "FINISHED_REASONS",
    "MAX_LAST_FAILURE_CHARS",
    "MAX_RUN_DETAIL_CHARS",
    "RUN_STATES",
    "RUN_STRATEGIES",
    "BenchCampaign",
    "BenchRun",
]

CAMPAIGN_STATES: Final = ("active", "paused", "done", "cancelled")
FINISHED_REASONS: Final = ("complete", "budget", "cancelled")
MAX_LAST_FAILURE_CHARS: Final = 128
RUN_STATES: Final = ("planned", "submitted", "done", "failed", "skipped")
RUN_STRATEGIES: Final = ("local", "mode1")
"""Literal copy of ``meshforge.bench.campaign.Strategy`` (the store imports
nothing above it; a unit test holds them equal)."""
MAX_RUN_DETAIL_CHARS: Final = 512
MAX_SPEC_CHARS: Final = 256 * 1024


def _in(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


class BenchCampaign(Base):
    __tablename__ = "bench_campaign"
    __table_args__ = (
        CheckConstraint(f"state IN ({_in(CAMPAIGN_STATES)})", name="state"),
        CheckConstraint(f"length(spec_yaml) <= {MAX_SPEC_CHARS}", name="spec_yaml"),
        CheckConstraint("run_count >= 1", name="run_count"),
        CheckConstraint(
            "max_concurrent_per_node BETWEEN 1 AND 64", name="max_concurrent_per_node"
        ),
        CheckConstraint("cooldown_s >= 0 AND cooldown_s <= 86400", name="cooldown_s"),
        CheckConstraint("max_infra_retries BETWEEN 0 AND 10", name="max_infra_retries"),
        CheckConstraint(
            f"finished_reason IS NULL OR finished_reason IN ({_in(FINISHED_REASONS)})",
            name="finished_reason",
        ),
        Index("ix_bench_campaign_created_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    spec_yaml: Mapped[str] = mapped_column(Text, nullable=False)
    spec_digest: Mapped[str] = mapped_column(String(71), nullable=False)
    """``sha256:`` of the spec's canonical JSON (comments/key order excluded)."""
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    run_count: Mapped[int] = mapped_column(Integer, nullable=False)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    """The expansion's counts: groups, configs, dropped, duplicates,
    estimated_hours, nodes (the node ids it was expanded against)."""
    created_by: Mapped[str | None] = mapped_column(String(ID_MAX_LENGTH), nullable=True)
    """The creating user (owner of every job it submits); None = legacy admin."""
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # V0.9 G5 (migration 0024): the runner's controls
    max_concurrent_per_node: Mapped[int] = mapped_column(
        Integer, server_default=text("1"), nullable=False
    )
    cooldown_s: Mapped[float] = mapped_column(
        Float, server_default=text("5"), nullable=False
    )
    max_infra_retries: Mapped[int] = mapped_column(
        Integer, server_default=text("2"), nullable=False
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    """Set by ``start`` (or ``resume``): the runner owns the campaign."""
    finished_reason: Mapped[str | None] = mapped_column(String(16), nullable=True)


class BenchRun(Base):
    __tablename__ = "bench_run"
    __table_args__ = (
        CheckConstraint(f"state IN ({_in(RUN_STATES)})", name="state"),
        CheckConstraint(f"strategy IN ({_in(RUN_STRATEGIES)})", name="strategy"),
        CheckConstraint("seq >= 1", name="seq"),
        CheckConstraint("repetition >= 1", name="repetition"),
        CheckConstraint(
            "(state = 'planned' AND job_id IS NULL) OR state = 'skipped' OR "
            "(state IN ('submitted', 'done', 'failed') AND job_id IS NOT NULL)",
            name="job_link",
        ),
        CheckConstraint(
            f"detail IS NULL OR length(detail) <= {MAX_RUN_DETAIL_CHARS}",
            name="detail",
        ),
        CheckConstraint("tries >= 0", name="tries"),
        CheckConstraint("generation >= 0", name="generation"),
        UniqueConstraint("campaign_id", "seq"),
        UniqueConstraint("job_id"),
        Index("ix_bench_run_campaign_state_seq", "campaign_id", "state", "seq"),
    )

    id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), primary_key=True)
    campaign_id: Mapped[str] = mapped_column(
        String(ID_MAX_LENGTH),
        ForeignKey("bench_campaign.id", ondelete="CASCADE"),
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    group_id: Mapped[str] = mapped_column(String(64), nullable=False)
    config_id: Mapped[str] = mapped_column(String(64), nullable=False)
    repetition: Mapped[int] = mapped_column(Integer, nullable=False)
    family: Mapped[str] = mapped_column(String(64), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    """The workload's seed (same for every run of the group)."""
    conditions: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    strategy: Mapped[str] = mapped_column(String(16), nullable=False)
    node_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    origin_node_id: Mapped[str] = mapped_column(String(ID_MAX_LENGTH), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    job_id: Mapped[str | None] = mapped_column(String(ID_MAX_LENGTH), nullable=True)
    detail: Mapped[str | None] = mapped_column(
        String(MAX_RUN_DETAIL_CHARS), nullable=True
    )
    """Why a run was skipped / failed (bounded, no user values)."""
    submitted_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    tries: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    """Infrastructure retries spent (V0.9 G5)."""
    generation: Mapped[int] = mapped_column(
        Integer, server_default=text("0"), nullable=False
    )
    """Times the run was requeued (infra retry or owner pre-emption)."""
    last_failure: Mapped[str | None] = mapped_column(
        String(MAX_LAST_FAILURE_CHARS), nullable=True
    )
