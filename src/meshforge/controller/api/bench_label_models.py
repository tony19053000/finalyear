"""Response models of ``GET /v1/bench/campaigns/{id}/labels`` (V0.9 G5, 0.9.6, D218).

The JSON form of :class:`meshforge.controller.telemetry.labels.CampaignLabels`
(``labels_version`` 1, docs/TELEMETRY.md "Labels"); the Parquet form is the
flat table of :mod:`meshforge.controller.telemetry.labels_export`. Both carry the
same numbers; groups are sorted by ``group_id``, configurations by ``config_id``.
"""

from __future__ import annotations

from typing import Any, Literal

from meshforge.bench.stats import Summary
from meshforge.controller.api.models import ApiModel
from meshforge.controller.telemetry.labels import (
    LABELS_VERSION,
    METRICS,
    CampaignLabels,
    ConfigCosts,
    GroupLabel,
)

__all__ = [
    "ConfigLabelOut",
    "GroupLabelOut",
    "LabelCounts",
    "LabelsOut",
    "MetricStats",
    "labels_out",
    "stats_out",
]


class MetricStats(ApiModel):
    """One cost of one configuration over its repetitions (meshforge.bench.stats)."""

    n: int
    median: float
    mean: float
    std: float | None
    cv: float | None
    """std / mean; null when n < 2 or the mean is 0."""
    ci_low: float
    ci_high: float
    ci_coverage: float
    """Achieved coverage of [ci_low, ci_high] for the median (order statistics)."""
    outliers: int
    """Values more than 3.5 scaled MADs from the median."""


class ConfigLabelOut(ApiModel):
    config_id: str
    strategy: str
    node_id: str
    origin_node_id: str
    state: Literal["measured", "unsupported"]
    repetitions: int
    planner_node_id: str | None
    total_ms: MetricStats | None
    compute_per_iter_ns: MetricStats | None
    bytes: MetricStats | None


class GroupLabelOut(ApiModel):
    group_id: str
    family: str
    condition: str
    params: dict[str, Any]
    label: Literal["best", "tie", "insufficient", "pending"]
    best_config_id: str | None
    best_strategy: str | None
    best_node_id: str | None
    margin_pct: float | None
    """How much higher the runner-up's median total_ms is, in % of the lowest."""
    tied_config_ids: tuple[str, ...]
    planner_node_id: str | None
    """The planner's preferred node for this group (D237: a preference)."""
    planner_agrees: bool | None
    reason: str | None
    configs: tuple[ConfigLabelOut, ...]


class LabelCounts(ApiModel):
    best: int = 0
    tie: int = 0
    insufficient: int = 0
    pending: int = 0


class LabelsOut(ApiModel):
    campaign_id: str
    labels_version: int
    campaign_state: str
    rows: int
    """Run records used (after the row filter and latest-per-repetition)."""
    counts: LabelCounts
    groups: tuple[GroupLabelOut, ...]


def stats_out(summary: Summary | None) -> MetricStats | None:
    """One :class:`~meshforge.bench.stats.Summary` as JSON (labels and the
    quality report, G5c)."""
    if summary is None:
        return None
    return MetricStats(
        n=summary.n,
        median=summary.median,
        mean=summary.mean,
        std=summary.std,
        cv=summary.cv,
        ci_low=summary.ci_low,
        ci_high=summary.ci_high,
        ci_coverage=summary.ci_coverage,
        outliers=summary.outliers,
    )


def _config(config: ConfigCosts) -> ConfigLabelOut:
    metrics = {m: stats_out(config.metrics.get(m)) for m in METRICS}
    return ConfigLabelOut(
        config_id=config.config_id,
        strategy=config.strategy,
        node_id=config.node_id,
        origin_node_id=config.origin_node_id,
        state=config.state,
        repetitions=config.repetitions,
        planner_node_id=config.planner_node_id,
        total_ms=metrics["total_ms"],
        compute_per_iter_ns=metrics["compute_per_iter_ns"],
        bytes=metrics["bytes"],
    )


def _group(group: GroupLabel) -> GroupLabelOut:
    return GroupLabelOut(
        group_id=group.group_id,
        family=group.family,
        condition=group.condition,
        params=dict(group.params),
        label=group.label,
        best_config_id=group.best_config_id,
        best_strategy=group.best_strategy,
        best_node_id=group.best_node_id,
        margin_pct=group.margin_pct,
        tied_config_ids=group.tied_config_ids,
        planner_node_id=group.planner_node_id,
        planner_agrees=group.planner_agrees,
        reason=group.reason,
        configs=tuple(_config(c) for c in group.configs),
    )


def labels_out(labels: CampaignLabels, *, campaign_state: str) -> LabelsOut:
    return LabelsOut(
        campaign_id=labels.campaign_id,
        labels_version=LABELS_VERSION,
        campaign_state=campaign_state,
        rows=labels.rows,
        counts=LabelCounts(**labels.counts),
        groups=tuple(_group(g) for g in labels.groups),
    )
