"""Rendering of a :class:`~.model.ScoreSet` for ``meshforge-agent hardware score|show``.

No I/O: text lines or a JSON-safe mapping, unit-testable from fabricated sets.
Every metric is shown - an ``UNAVAILABLE``/``FAILED`` row says why, it never
disappears (the planner-facing rule of D30/D32 applies to the operator view too).
"""

from __future__ import annotations

import json
from typing import Any

from meshforge.agent.benchmarks.cache import CacheState
from meshforge.agent.benchmarks.model import Metric, MetricStatus, ScoreSet

__all__ = ["format_value", "score_lines", "scores_payload"]


def format_value(value: float) -> str:
    """Three significant digits, no exponent for the ranges scores live in."""
    if value >= 100:
        return f"{value:.0f}"
    if value >= 10:
        return f"{value:.1f}"
    return f"{value:.2f}"


def _notes(metric: Metric) -> str:
    notes: list[str] = []
    if metric.lower_bound:
        notes.append("lower bound")
    if metric.threads_effective is not None:
        notes.append(f"{metric.threads_effective} thread(s)")
    if metric.throttling_suspected:
        notes.append("throttling suspected")
    return ", ".join(notes)


def _row(metric: Metric) -> str:
    if metric.status is MetricStatus.OK and metric.mean is not None:
        stdev = metric.stdev or 0.0
        value = f"{format_value(metric.mean)} ± {format_value(stdev)} {metric.unit}"
        backend = " ".join(p for p in (metric.backend, metric.backend_version) if p)
        extra = _notes(metric)
        return f"  {metric.key:<26} {value:<24} n={metric.n:<3} {backend}" + (
            f"  ({extra})" if extra else ""
        )
    reason = metric.reason.value if metric.reason else "-"
    detail = f"  {metric.detail}" if metric.detail else ""
    return f"  {metric.key:<26} {metric.status.value}: {reason}{detail}"


def score_lines(scores: ScoreSet, *, source: str) -> list[str]:
    """``source`` is ``measured`` (this run) or ``cached (valid|stale: ...)``."""
    c = scores.conditions
    battery = {True: "yes", False: "no", None: "unknown"}[c.on_battery]
    load = (
        "unknown"
        if c.cpu_load_percent_before is None
        else (
            f"{c.cpu_load_percent_before:.0f} %"
            + (" (high)" if c.background_load_high else "")
        )
    )
    lines = [
        f"scores: {source}",
        f"  run_id: {scores.run_id}",
        f"  measured_at: {scores.measured_at.isoformat()}",
        f"  score_version: {scores.score_version}  fingerprint: {scores.fingerprint[:16]}",
        f"  conditions: on battery {battery} · cpu load before {load} · "
        f"priority {'lowered' if c.priority_lowered else 'normal'} · "
        f"{c.duration_s:.1f} s of {c.budget_s:.0f} s budget"
        + (" (budget exhausted)" if c.budget_exhausted else ""),
    ]
    lines.extend(_row(m) for m in scores.metrics)
    return lines


def scores_payload(
    scores: ScoreSet | None, *, source: str, cache: CacheState | None = None
) -> dict[str, Any]:
    """``{"source", "cache_status", "cache_reason", "scores"}`` (JSON-safe)."""
    return {
        "source": source,
        "cache_status": cache.status.value if cache is not None else None,
        "cache_reason": (cache.reason or None) if cache is not None else None,
        "scores": json.loads(scores.to_json()) if scores is not None else None,
    }
