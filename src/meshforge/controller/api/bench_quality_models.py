"""Response models of ``GET /v1/bench/campaigns/{id}/quality`` (V0.9 G5c, 0.9.8, D220).

The JSON form of :class:`meshforge.bench.quality.CampaignQuality`
(``quality_version`` 1, docs/TELEMETRY.md "Quality report"): a campaign summary
with the criterion verdicts, then one entry per configuration sorted by
``(group_id, config_id)``. Field order is fixed (``ApiModel``), so two calls
over the same records are byte-identical (D222).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from meshforge.bench.quality import (
    QUALITY_VERSION,
    REQUIRED_FIELDS,
    CampaignQuality,
    ConfigQuality,
    QualitySummary,
)
from meshforge.controller.api.bench_label_models import MetricStats, stats_out
from meshforge.controller.api.models import ApiModel

__all__ = [
    "ConfigQualityOut",
    "MissingFields",
    "QualityOut",
    "QualitySummaryOut",
    "RunStateCounts",
    "quality_out",
]


class RunStateCounts(ApiModel):
    planned: int = 0
    submitted: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0


class MissingFields(ApiModel):
    """Per required field: ok records of the configuration with it null
    (``planner_node_id`` only where the run was unpinned)."""

    total_ms: int = 0
    compute_ns: int = 0
    compute_iterations: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    queue_ms: int = 0
    run_ms: int = 0
    node_state: int = 0
    node_cpu_percent: int = 0
    planner_node_id: int = 0


class ConfigQualityOut(ApiModel):
    group_id: str
    config_id: str
    family: str
    condition: str
    strategy: str
    node_id: str
    state: Literal["measured", "unsupported"]
    planned: int
    """Runs planned for this configuration."""
    done: int
    n: int
    """Distinct repetitions with an ok record (latest attempt each)."""
    total_ms: MetricStats | None
    compute_per_iter_ns: MetricStats | None
    outlier_attempt_ids: tuple[str, ...]
    """Attempts whose total_ms is more than 3.5 scaled MADs from the median."""
    cv_flag: bool
    """total_ms CV above the campaign report's threshold."""
    reps_flag: bool
    """Fewer than 3 measured repetitions (criterion 3)."""
    flagged: bool
    records_checked: int
    missing: MissingFields


class QualitySummaryOut(ApiModel):
    campaign_state: str
    pending: bool
    """The campaign is still open: the verdicts are null."""
    runs_by_state: RunStateCounts
    records: int
    """Run records (fillers and warm-ups excluded)."""
    bench_role_records: dict[str, int]
    """Filler / warm-up records, by role - never in any statistic."""
    infra_failures: int
    owner_preempted: int
    infra_failure_rate: float | None
    """Same number as `compute bench status` (owner_preempted excluded)."""
    condition_failures: dict[str, int]
    """condition_failed records by condition."""
    workload_failures: dict[str, int]
    """Workload failures by family."""
    crit1_infra_ok: bool | None
    """Infrastructure failure rate below 2 %; null while pending."""
    crit3_reps_ok: bool | None
    """Every measured configuration has >= 3 repetitions and a CV; null while
    pending."""
    configs: int
    flagged_configs: int
    cv_threshold: float


class QualityOut(ApiModel):
    campaign_id: str
    quality_version: int
    summary: QualitySummaryOut
    configs: tuple[ConfigQualityOut, ...]


def _missing(counts: Mapping[str, int]) -> MissingFields:
    return MissingFields(**{name: int(counts.get(name, 0)) for name in REQUIRED_FIELDS})


def _config(config: ConfigQuality) -> ConfigQualityOut:
    return ConfigQualityOut(
        group_id=config.group_id,
        config_id=config.config_id,
        family=config.family,
        condition=config.condition,
        strategy=config.strategy,
        node_id=config.node_id,
        state="unsupported" if config.state == "unsupported" else "measured",
        planned=config.planned,
        done=config.done,
        n=config.n,
        total_ms=stats_out(config.total_ms),
        compute_per_iter_ns=stats_out(config.compute_per_iter_ns),
        outlier_attempt_ids=config.outlier_attempt_ids,
        cv_flag=config.cv_flag,
        reps_flag=config.reps_flag,
        flagged=config.flagged,
        records_checked=config.records_checked,
        missing=_missing(config.missing),
    )


def _summary(summary: QualitySummary) -> QualitySummaryOut:
    return QualitySummaryOut(
        campaign_state=summary.campaign_state,
        pending=summary.pending,
        runs_by_state=RunStateCounts(**summary.runs_by_state),
        records=summary.records,
        bench_role_records=dict(summary.bench_role_records),
        infra_failures=summary.infra_failures,
        owner_preempted=summary.owner_preempted,
        infra_failure_rate=summary.infra_failure_rate,
        condition_failures=dict(summary.condition_failures),
        workload_failures=dict(summary.workload_failures),
        crit1_infra_ok=summary.crit1_infra_ok,
        crit3_reps_ok=summary.crit3_reps_ok,
        configs=summary.configs,
        flagged_configs=summary.flagged_configs,
        cv_threshold=summary.cv_threshold,
    )


def quality_out(report: CampaignQuality) -> QualityOut:
    return QualityOut(
        campaign_id=report.campaign_id,
        quality_version=QUALITY_VERSION,
        summary=_summary(report.summary),
        configs=tuple(_config(c) for c in report.configs),
    )
