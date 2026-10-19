"""Benchmark labels: the best configuration per group, or an honest "no" (V0.9 G5, D218).

A campaign measures every **group** (same workload family + parameters +
condition, ``group_id``) under several **configurations** (strategy + node +
origin, ``config_id``), each repeated. The label of a group says which
configuration was cheapest - **only when the measurements can tell** (D218: a
noisy "best" teaches the ML planner noise). Labels are **derived on demand**
from the run records (D246): no table, no migration, :data:`LABELS_VERSION` 1.

This module is **pure** (no I/O): :func:`build_labels` turns a campaign's
planned runs (:class:`PlannedRunState`, from ``bench_run``) and its label rows
(:func:`.labels_store.label_rows` - the one row filter) into
:class:`GroupLabel` s. The same inputs give the same labels, in the same order
(groups by ``group_id``, configurations by ``config_id``; D222).

**Costs per configuration** (:data:`METRICS`), each summarised by
:func:`meshforge.bench.stats.summarize` (n, median, mean, std, CV, the median's
distribution-free CI, MAD outlier count):

* ``total_ms`` - the record's sum of measured phases (compute excluded);
* ``compute_per_iter_ns`` - ``compute_ns / compute_iterations`` (one kernel
  iteration, D225.3); null when either is null;
* ``bytes`` - ``bytes_in + bytes_out``; null when either is null.

**Label** (decided on ``total_ms``, in this order):

1. ``pending`` - a run of the group is not finished yet (``planned`` /
   ``submitted``), or finished but its run record is not written yet;
2. ``insufficient`` - a configuration has fewer than :data:`MIN_REPETITIONS`
   measured repetitions (a planned configuration with no record has 0), or
   no configuration of the group can run at all;
3. ``best`` - the configuration with the lowest median ``total_ms`` (ties
   broken by ``config_id``) whose CI upper bound is **below every other
   configuration's CI lower bound**; ``margin_pct`` = how much higher the
   runner-up's median is, in % of the best's (null when the group has one
   configuration: it is the best by default, nothing to compare);
4. ``tie`` - otherwise; ``tied_config_ids`` = the lowest-median configuration
   and every configuration whose CI reaches into its CI.

A configuration whose every run was stored ``skipped`` as **unsupported** (G4
``on_unsupported: skip``: that node cannot apply the condition) is reported
with ``state = "unsupported"`` and left out of the comparison - it was never
going to run, so it cannot make the group ``insufficient`` forever. Any other
skip (node gone, campaign cancelled) is a planned configuration without data
-> ``insufficient``.

**Planner signal** (D237: preference only). Each configuration passes through
the planner's preferred node of its records (``planner_node_id``, the most
frequent; ties -> the smallest id); the group's preference is the most
frequent over all its kept records, and ``planner_agrees`` = (best node ==
that preference) for a ``best`` label - a free regret signal for V0.14.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Literal

from meshforge.bench.stats import Summary, summarize

__all__ = [
    "LABELS_VERSION",
    "LABEL_KINDS",
    "METRICS",
    "MIN_REPETITIONS",
    "UNSUPPORTED_DETAIL_PREFIX",
    "CampaignLabels",
    "ConfigCosts",
    "GroupLabel",
    "PlannedRunState",
    "build_labels",
    "latest_per_repetition",
    "metric_value",
]

LABELS_VERSION: Final = 1
"""Version of the labels' shape (JSON and Parquet); a fingerprint test holds it."""
MIN_REPETITIONS: Final = 3
"""Fewest measured repetitions a configuration needs before a group is labelled
(D216: campaigns repeat >= 3 times)."""
METRICS: Final = ("total_ms", "compute_per_iter_ns", "bytes")
LABEL_KINDS: Final = ("best", "tie", "insufficient", "pending")
UNSUPPORTED_DETAIL_PREFIX: Final = "unsupported: "
"""``bench_run.detail`` of a run stored skipped because its node cannot apply
the condition (``controller.bench.store.create_campaign``)."""
_OPEN_RUN_STATES: Final = frozenset({"planned", "submitted"})
_ENDED_RUN_STATES: Final = frozenset({"done", "failed"})

type Label = Literal["best", "tie", "insufficient", "pending"]
type ConfigState = Literal["measured", "unsupported"]


@dataclass(frozen=True, slots=True)
class PlannedRunState:
    """One ``bench_run`` row as the labels need it."""

    group_id: str
    config_id: str
    repetition: int
    family: str
    params: Mapping[str, Any]
    condition: str
    strategy: str
    node_id: str
    origin_node_id: str
    state: str
    detail: str | None = None
    recorded: bool = False
    """The run's job has its record: an ``ok`` one for a ``done`` run, any one
    for a ``failed`` run (the recorder writes within seconds of the end)."""

    @property
    def unsupported(self) -> bool:
        return self.state == "skipped" and (self.detail or "").startswith(
            UNSUPPORTED_DETAIL_PREFIX
        )

    @property
    def open(self) -> bool:
        """Not finished, or finished and its record not written yet."""
        if self.state in _OPEN_RUN_STATES:
            return True
        return self.state in _ENDED_RUN_STATES and not self.recorded


@dataclass(frozen=True, slots=True)
class ConfigCosts:
    config_id: str
    strategy: str
    node_id: str
    origin_node_id: str
    state: ConfigState
    repetitions: int
    """Measured repetitions kept (distinct ``repetition`` values)."""
    planner_node_id: str | None
    metrics: Mapping[str, Summary | None]
    """:data:`METRICS` -> its summary (None = no value)."""


@dataclass(frozen=True, slots=True)
class GroupLabel:
    group_id: str
    family: str
    condition: str
    params: Mapping[str, Any]
    label: Label
    best_config_id: str | None
    best_strategy: str | None
    best_node_id: str | None
    margin_pct: float | None
    tied_config_ids: tuple[str, ...]
    planner_node_id: str | None
    planner_agrees: bool | None
    reason: str | None
    """Why ``insufficient`` / ``pending`` (fixed wording, ids only)."""
    configs: tuple[ConfigCosts, ...]


@dataclass(frozen=True, slots=True)
class CampaignLabels:
    campaign_id: str
    groups: tuple[GroupLabel, ...]
    rows: int
    """Label rows used (after the filter and the latest-per-repetition rule)."""
    counts: Mapping[str, int] = field(default_factory=dict)
    """Groups per label kind (every kind listed)."""


def metric_value(row: Mapping[str, Any], metric: str) -> float | int | None:
    """One cost of one record (module docstring); None = not measured."""
    if metric == "total_ms":
        return _int(row.get("total_ms"))
    if metric == "compute_per_iter_ns":
        ns, iterations = (
            _int(row.get("compute_ns")),
            _int(row.get("compute_iterations")),
        )
        if ns is None or iterations is None or iterations <= 0:
            return None
        return ns / iterations
    if metric == "bytes":
        inn, out = _int(row.get("bytes_in")), _int(row.get("bytes_out"))
        if inn is None or out is None:
            return None
        return inn + out
    raise ValueError(f"unknown metric {metric}")


def _int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _latest_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (row["attempt_created_at"], row["attempt_no"], row["attempt_id"])


def latest_per_repetition(
    rows: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Keep one record per (group_id, config_id, repetition): the **latest**
    attempt - created last, then highest attempt number, then highest attempt
    id (deterministic whatever the input order). A config id already names its
    group (it is a digest of it); the group in the key is belt and braces.
    A retried run (D234) can leave several ``ok`` records; the latest is the
    one the campaign kept. Rows
    without a config or repetition are dropped. Sorted by (group_id,
    config_id, repetition)."""
    kept: dict[tuple[str, str, int], Mapping[str, Any]] = {}
    for row in rows:
        config, rep = row.get("config_id"), row.get("repetition")
        if not isinstance(config, str) or not isinstance(rep, int):
            continue
        key = (str(row.get("group_id") or ""), config, rep)
        current = kept.get(key)
        if current is None or _latest_key(row) > _latest_key(current):
            kept[key] = row
    return sorted(
        kept.values(),
        key=lambda r: (str(r.get("group_id") or ""), r["config_id"], r["repetition"]),
    )


def _mode(values: Iterable[str | None]) -> str | None:
    counted = Counter(v for v in values if v is not None)
    if not counted:
        return None
    top = max(counted.values())
    return min(v for v, c in counted.items() if c == top)


def _config(
    config_id: str,
    runs: Sequence[PlannedRunState],
    rows: Sequence[Mapping[str, Any]],
) -> ConfigCosts:
    head = runs[0] if runs else None
    sample = rows[0] if rows else {}
    unsupported = bool(runs) and all(r.unsupported for r in runs)
    return ConfigCosts(
        config_id=config_id,
        strategy=head.strategy if head else str(sample.get("strategy") or ""),
        node_id=head.node_id if head else str(sample.get("node_id") or ""),
        origin_node_id=(
            head.origin_node_id
            if head
            else str(sample.get("submitter_node_id") or sample.get("node_id") or "")
        ),
        state="unsupported" if unsupported else "measured",
        repetitions=len({r["repetition"] for r in rows}),
        planner_node_id=_mode(r.get("planner_node_id") for r in rows),
        metrics={
            m: summarize(
                [v for v in (metric_value(r, m) for r in rows) if v is not None]
            )
            for m in METRICS
        },
    )


def _decide(
    configs: Sequence[ConfigCosts], runs: Sequence[PlannedRunState]
) -> tuple[Label, str | None, ConfigCosts | None, float | None, tuple[str, ...]]:
    """(label, reason, best, margin_pct, tied ids)."""
    open_runs = sum(1 for r in runs if r.open)
    if open_runs:
        return (
            "pending",
            f"{open_runs} run(s) not finished or not recorded",
            None,
            None,
            (),
        )
    candidates = [c for c in configs if c.state == "measured"]
    if not candidates:
        return "insufficient", "no configuration can run this group", None, None, ()
    short = [
        c.config_id
        for c in candidates
        if (s := c.metrics["total_ms"]) is None or s.n < MIN_REPETITIONS
    ]
    if short:
        return (
            "insufficient",
            f"fewer than {MIN_REPETITIONS} measured repetitions: "
            + ", ".join(short[:5])
            + (f" (+{len(short) - 5})" if len(short) > 5 else ""),
            None,
            None,
            (),
        )
    ranked = sorted(candidates, key=lambda c: (_total(c).median, c.config_id))
    best, others = ranked[0], ranked[1:]
    top = _total(best)
    margin = None
    if others and top.median > 0:
        margin = (_total(others[0]).median - top.median) / top.median * 100.0
    if all(top.ci_high < _total(o).ci_low for o in others):
        return "best", None, best, margin, ()
    tied = sorted(
        [best.config_id]
        + [o.config_id for o in others if _total(o).ci_low <= top.ci_high]
    )
    return "tie", None, None, margin, tuple(tied)


def _total(config: ConfigCosts) -> Summary:
    summary = config.metrics["total_ms"]
    if summary is None:  # pragma: no cover - _decide checked n >= 3
        raise AssertionError("total_ms summary missing")
    return summary


def _group(
    group_id: str,
    runs: Sequence[PlannedRunState],
    rows: Sequence[Mapping[str, Any]],
) -> GroupLabel:
    config_ids = sorted({r.config_id for r in runs} | {r["config_id"] for r in rows})
    configs = tuple(
        _config(
            cid,
            [r for r in runs if r.config_id == cid],
            [r for r in rows if r["config_id"] == cid],
        )
        for cid in config_ids
    )
    label, reason, best, margin, tied = _decide(configs, runs)
    head = runs[0] if runs else None
    sample = rows[0] if rows else {}
    planner = _mode(r.get("planner_node_id") for r in rows)
    return GroupLabel(
        group_id=group_id,
        family=head.family if head else str(sample.get("workload_family") or ""),
        condition=head.condition if head else str(sample.get("condition") or "none"),
        params=dict(head.params) if head else dict(sample.get("workload_params") or {}),
        label=label,
        best_config_id=best.config_id if best else None,
        best_strategy=best.strategy if best else None,
        best_node_id=best.node_id if best else None,
        margin_pct=margin,
        tied_config_ids=tied,
        planner_node_id=planner,
        planner_agrees=(
            None if best is None or planner is None else best.node_id == planner
        ),
        reason=reason,
        configs=configs,
    )


def build_labels(
    campaign_id: str,
    runs: Iterable[PlannedRunState],
    rows: Iterable[Mapping[str, Any]],
) -> CampaignLabels:
    """Every group's label (module docstring). ``rows`` are label rows
    (already filtered); the latest-per-repetition rule is applied here too, so
    a caller cannot forget it. Pure and deterministic.

    The inputs are **not shared mutable state** (D248 N5): ``runs`` and
    ``rows`` are fresh per request (the route reads them for this call), only
    read here, and every list built below (the per-group buckets, the sort)
    is local - so running this in a worker thread (``asyncio.to_thread``)
    races nothing."""
    kept = latest_per_repetition(rows)
    by_group_runs: dict[str, list[PlannedRunState]] = {}
    for run in runs:
        by_group_runs.setdefault(run.group_id, []).append(run)
    by_group_rows: dict[str, list[Mapping[str, Any]]] = {}
    for row in kept:
        group = row.get("group_id")
        if isinstance(group, str):
            by_group_rows.setdefault(group, []).append(row)
    for group_runs in by_group_runs.values():
        group_runs.sort(key=lambda r: (r.config_id, r.repetition))
    groups = tuple(
        _group(gid, by_group_runs.get(gid, []), by_group_rows.get(gid, []))
        for gid in sorted(by_group_runs.keys() | by_group_rows.keys())
    )
    counts = {kind: sum(1 for g in groups if g.label == kind) for kind in LABEL_KINDS}
    return CampaignLabels(
        campaign_id=campaign_id,
        groups=groups,
        rows=sum(len(v) for v in by_group_rows.values()),
        counts=counts,
    )
