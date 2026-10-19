"""The data quality report of a benchmark campaign (V0.9 G5c, roadmap 0.9.8, D220).

**Pure** (no I/O, no clock): the controller streams the campaign's run
records into a :class:`QualityInput` (one :meth:`QualityInput.add` per
record), then :func:`build_quality` turns them, the campaign's planned runs
and the shared failure counts into a :class:`CampaignQuality`. The same
inputs give the same report, in the same order (configurations by
``(group_id, config_id)``; D222). ``quality_version`` :data:`QUALITY_VERSION`.

**What it reads.** *Every* record of the campaign - unlike the labels it must
see failures - but each record is classified once:

* ``bench_role`` set (queue fillers, cache warm-ups, D244): counted in
  ``bench_role_records`` and **nothing else** - scaffolding never enters a
  statistic (a filler's huge ``total_ms`` moves no median);
* ``label_row`` (the controller computes it with the labels' own SQL filter,
  :func:`meshforge.controller.telemetry.labels_store.label_filter`, as a
  column - never a second filter): an ``ok`` measurement. These, after
  :func:`~meshforge.bench.records.latest_per_repetition`, are the statistics'
  input - exactly what the labels use, so the medians agree;
* every other record lands in **exactly one** counted bucket (G5c fix r1,
  tester T-3 - the buckets sum to ``records``): ``owner_preempted`` and
  infrastructure failures (from :class:`FailureCounts`, the status query),
  ``condition_failed`` by condition, ``workload`` by family, ``rejected_ok``
  (class ``ok`` but the label filter refuses it) by reason - the
  ``workload_status`` when not ``ok`` (``check_failed``, ...;
  ``no_workload_status`` when null), else ``condition_unverified``
  (``condition_measured.verified = false``), else ``other`` (a filter clause
  this classification does not name - a test keeps it at zero) - and ``other_failures`` by class
  (``unavailable``: no CUDA / cancelled).

**Per configuration** (within its group): ``planned`` / ``done`` runs; ``n`` =
distinct repetitions with an ok record; ``total_ms`` and
``compute_per_iter_ns`` summarised by :func:`meshforge.bench.stats.summarize`
(median, CV, the median's distribution-free CI, MAD outliers); the outlier
attempt ids of ``total_ms``; ``cv_flag`` when the ``total_ms`` CV exceeds the
threshold (``[controller.bench] quality_cv_threshold``, default 0.10);
``reps_flag`` when ``n < 3`` (criterion 3); ``missing`` = per field of
:data:`REQUIRED_FIELDS`, how many of its ok records have it null.

**Campaign summary.** Runs by state; the infrastructure failure rate - **the
same** :func:`infra_failure_rate` over the same :class:`FailureCounts` as
``compute bench status`` (``owner_preempted`` excluded); pre-emptions;
condition failures by condition; workload failures by family; and the
criterion verdicts: ``crit1_infra_ok`` = the **exact** rate (a fraction, never
the rounded display value) < :data:`CRIT1_MAX_INFRA_FRACTION` (2 %),
``crit3_reps_ok`` = every measured configuration has ``n >= 3`` and a
``total_ms`` CV. Both are ``None`` (``pending``) while the campaign is open.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any, Final, Protocol

from meshforge.bench.records import (
    MIN_REPETITIONS,
    latest_per_repetition,
    metric_value,
)
from meshforge.bench.stats import Summary, mad_outliers, summarize
from meshforge.common.errors import FailureType

__all__ = [
    "CRIT1_MAX_INFRA_FRACTION",
    "CRIT1_MAX_INFRA_RATE",
    "DEFAULT_CV_THRESHOLD",
    "QUALITY_METRICS",
    "QUALITY_VERSION",
    "REJECTED_NO_STATUS",
    "REJECTED_OTHER",
    "REJECTED_UNVERIFIED",
    "REQUIRED_FIELDS",
    "ROW_FIELDS",
    "RUN_STATES",
    "CampaignQuality",
    "ConfigQuality",
    "FailureCounts",
    "PlannedRun",
    "QualityInput",
    "QualitySummary",
    "build_quality",
    "crit1_infra_ok",
    "infra_failure_fraction",
    "infra_failure_rate",
]

QUALITY_VERSION: Final = 1
DEFAULT_CV_THRESHOLD: Final = 0.10
CRIT1_MAX_INFRA_FRACTION: Final = Fraction(1, 50)
"""Criterion 1: infrastructure failures below 2 % of the counted attempts -
exact, the verdict compares fractions (G5c fix r1, tester T-1)."""
CRIT1_MAX_INFRA_RATE: Final = float(CRIT1_MAX_INFRA_FRACTION)
"""The same bound as a float, for display only."""
QUALITY_METRICS: Final = ("total_ms", "compute_per_iter_ns")
RUN_STATES: Final = ("planned", "submitted", "done", "failed", "skipped")
_TERMINAL_CAMPAIGN: Final = frozenset({"done", "cancelled"})

REQUIRED_FIELDS: Final = (
    "total_ms",
    "compute_ns",
    "compute_iterations",
    "bytes_in",
    "bytes_out",
    "queue_ms",
    "run_ms",
    "node_state",
    "node_cpu_percent",
    "planner_node_id",
)
REJECTED_UNVERIFIED: Final = "condition_unverified"
REJECTED_NO_STATUS: Final = "no_workload_status"
REJECTED_OTHER: Final = "other"
_OWNER_PREEMPTED: Final = FailureType.OWNER_PREEMPTED.value
"""The record fields the labels and V0.15 training need; the report counts
their nulls per configuration over its ok records. Phase timings = ``queue_ms``
+ ``run_ms`` (every forced run queues and runs; ``stage_ms`` / ``env_ms`` /
``collect_ms`` may be null for a skipped phase). Start snapshot =
``node_state`` + ``node_cpu_percent``. ``planner_node_id`` is checked only
where the run was **unpinned** (``placement_mode = auto``): a pinned run's
shadow preference is null when no node was feasible. A unit test holds every
name to the telemetry schema's columns."""

ROW_FIELDS: Final = (
    "attempt_id",
    "attempt_no",
    "attempt_created_at",
    "group_id",
    "config_id",
    "repetition",
    "failure_class",
    "failure_type",
    "workload_family",
    "condition",
    "placement_mode",
    "workload_status",
    *REQUIRED_FIELDS,
)
"""Record columns a row passed to :meth:`QualityInput.add` must carry, plus
``label_row`` (bool), ``bench_role`` (str or None) and ``condition_verified``
(``condition_measured->>'verified'``: ``"false"`` / ``"true"`` / None)."""


@dataclass(frozen=True, slots=True)
class FailureCounts:
    """The campaign's non-``bench_role`` run records: all, infrastructure
    failures (``owner_preempted`` excluded), owner pre-emptions."""

    records: int
    infra: int
    preempted: int


def infra_failure_fraction(counts: FailureCounts) -> Fraction | None:
    """Criterion 1's rate, **exact**: infrastructure failures / (records -
    pre-emptions); ``None`` with nothing counted. **The** helper - the
    campaign status and the quality report both go through it (D246), the
    displayed rate (:func:`infra_failure_rate`) and the verdict
    (:func:`crit1_infra_ok`) alike."""
    counted = counts.records - counts.preempted
    if counted <= 0:
        return None
    return Fraction(counts.infra, counted)


def infra_failure_rate(counts: FailureCounts) -> float | None:
    """:func:`infra_failure_fraction` rounded to 4 places - **for display
    only** (status, report, events); never decide on it: 1999 / 100000 =
    1.999 % shows as 0.02 yet meets criterion 1."""
    exact = infra_failure_fraction(counts)
    return None if exact is None else round(float(exact), 4)


def crit1_infra_ok(counts: FailureCounts) -> bool:
    """Criterion 1 on the exact fraction: rate <
    :data:`CRIT1_MAX_INFRA_FRACTION`; False with nothing counted (no
    evidence). ``pending`` is the caller's call."""
    exact = infra_failure_fraction(counts)
    return exact is not None and exact < CRIT1_MAX_INFRA_FRACTION


class PlannedRun(Protocol):
    """One ``bench_run`` row as the report needs it
    (``controller.telemetry.labels.PlannedRunState`` satisfies it)."""

    @property
    def group_id(self) -> str: ...
    @property
    def config_id(self) -> str: ...
    @property
    def family(self) -> str: ...
    @property
    def condition(self) -> str: ...
    @property
    def strategy(self) -> str: ...
    @property
    def node_id(self) -> str: ...
    @property
    def state(self) -> str: ...
    @property
    def unsupported(self) -> bool: ...


def _finite(value: float | int | None) -> float | int | None:
    """Belt and braces: a non-finite value (never written by the recorder)
    is treated as missing rather than crash the statistics."""
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return None
    return value


@dataclass(slots=True)
class QualityInput:
    """The streamed side of the report: one :meth:`add` per run record, in
    any order. Keeps only the ok rows' fields and counters - never the
    records themselves. Every non-``bench_role`` record lands in exactly one
    bucket (module docstring); infrastructure failures and pre-emptions are
    tallied here too (``infra`` / ``preempted``) only so the invariant can be
    checked - the summary takes them from :class:`FailureCounts`."""

    ok_rows: list[dict[str, Any]] = field(default_factory=list)
    roles: Counter[str] = field(default_factory=Counter)
    records: int = 0
    measured: int = 0
    infra: int = 0
    preempted: int = 0
    condition_failures: Counter[str] = field(default_factory=Counter)
    workload_failures: Counter[str] = field(default_factory=Counter)
    rejected_ok: Counter[str] = field(default_factory=Counter)
    other_failures: Counter[str] = field(default_factory=Counter)

    def add(self, row: Mapping[str, Any]) -> None:
        role = row.get("bench_role")
        if role is not None:
            self.roles[str(role)] += 1
            return
        self.records += 1
        failure_class = str(row.get("failure_class"))
        # the same order as control.record_failure_counts: the failure type
        # owner_preempted first, whatever the class
        if row.get("failure_type") == _OWNER_PREEMPTED:
            self.preempted += 1
        elif failure_class == "infrastructure":
            self.infra += 1
        elif failure_class == "condition_failed":
            self.condition_failures[str(row.get("condition") or "none")] += 1
        elif failure_class == "workload":
            self.workload_failures[str(row.get("workload_family") or "unknown")] += 1
        elif failure_class != "ok":
            self.other_failures[failure_class] += 1
        elif row.get("label_row"):
            self.measured += 1
            self.ok_rows.append({name: row.get(name) for name in ROW_FIELDS})
        else:
            self.rejected_ok[_rejected_reason(row)] += 1


def _rejected_reason(row: Mapping[str, Any]) -> str:
    """Why the label filter refused a class-``ok`` record (module docstring)."""
    status = row.get("workload_status")
    if status is None:
        return REJECTED_NO_STATUS
    if status != "ok":
        return str(status)
    if str(row.get("condition_verified")).lower() == "false":
        return REJECTED_UNVERIFIED
    return REJECTED_OTHER  # the label filter grew a clause this does not name


@dataclass(frozen=True, slots=True)
class ConfigQuality:
    group_id: str
    config_id: str
    family: str
    condition: str
    strategy: str
    node_id: str
    state: str
    """``measured`` or ``unsupported`` (every run skipped: the node cannot
    apply the condition - never flagged, left out of criterion 3)."""
    planned: int
    done: int
    n: int
    total_ms: Summary | None
    compute_per_iter_ns: Summary | None
    outlier_attempt_ids: tuple[str, ...]
    cv_flag: bool
    reps_flag: bool
    records_checked: int
    """Ok records the missing-field check looked at (before the
    latest-per-repetition rule)."""
    missing: Mapping[str, int]
    """:data:`REQUIRED_FIELDS` -> nulls among those records (every field
    listed, in that order)."""

    @property
    def flagged(self) -> bool:
        return self.cv_flag or self.reps_flag or any(self.missing.values())


@dataclass(frozen=True, slots=True)
class QualitySummary:
    campaign_state: str
    pending: bool
    runs_by_state: Mapping[str, int]
    records: int
    bench_role_records: Mapping[str, int]
    measured_records: int
    """Ok measurements the labels use (before latest-per-repetition)."""
    infra_failures: int
    owner_preempted: int
    infra_failure_rate: float | None
    condition_failures: Mapping[str, int]
    workload_failures: Mapping[str, int]
    rejected_ok: Mapping[str, int]
    """Class ``ok`` records the label filter refuses, by reason."""
    other_failures: Mapping[str, int]
    """Records of any other class (``unavailable``), by class."""

    @property
    def accounted(self) -> int:
        """The buckets' sum - equals ``records`` when the failure counts and
        the streamed records come from one snapshot (they do)."""
        return (
            self.measured_records
            + self.infra_failures
            + self.owner_preempted
            + sum(self.condition_failures.values())
            + sum(self.workload_failures.values())
            + sum(self.rejected_ok.values())
            + sum(self.other_failures.values())
        )

    crit1_infra_ok: bool | None
    crit3_reps_ok: bool | None
    configs: int
    flagged_configs: int
    cv_threshold: float


@dataclass(frozen=True, slots=True)
class CampaignQuality:
    campaign_id: str
    summary: QualitySummary
    configs: tuple[ConfigQuality, ...]


def _summaries(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Summary | None], tuple[str, ...]]:
    out: dict[str, Summary | None] = {}
    outliers: tuple[str, ...] = ()
    for metric in QUALITY_METRICS:
        pairs = [
            (v, str(r["attempt_id"]))
            for r in rows
            if (v := _finite(metric_value(r, metric))) is not None
        ]
        out[metric] = summarize([v for v, _ in pairs])
        if metric == "total_ms":
            far = set(mad_outliers([v for v, _ in pairs]))
            outliers = tuple(sorted(a for v, a in pairs if float(v) in far))
    return out, outliers


def _missing(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    counts = dict.fromkeys(REQUIRED_FIELDS, 0)
    for row in rows:
        for name in REQUIRED_FIELDS:
            if name == "planner_node_id" and row.get("placement_mode") != "auto":
                continue  # pinned: the shadow preference may be null
            if row.get(name) is None:
                counts[name] += 1
    return counts


def _config(
    key: tuple[str, str],
    runs: Sequence[PlannedRun],
    raw: Sequence[Mapping[str, Any]],
    kept: Sequence[Mapping[str, Any]],
    cv_threshold: float,
) -> ConfigQuality:
    group_id, config_id = key
    head = runs[0] if runs else None
    sample = kept[0] if kept else (raw[0] if raw else {})
    unsupported = bool(runs) and all(r.unsupported for r in runs)
    metrics, outliers = _summaries(kept)
    n = len({r["repetition"] for r in kept})
    total = metrics["total_ms"]
    measured = not unsupported
    return ConfigQuality(
        group_id=group_id,
        config_id=config_id,
        family=head.family if head else str(sample.get("workload_family") or ""),
        condition=head.condition if head else str(sample.get("condition") or "none"),
        strategy=head.strategy if head else "",
        node_id=head.node_id if head else "",
        state="unsupported" if unsupported else "measured",
        planned=len(runs),
        done=sum(1 for r in runs if r.state == "done"),
        n=n,
        total_ms=total,
        compute_per_iter_ns=metrics["compute_per_iter_ns"],
        outlier_attempt_ids=outliers,
        cv_flag=(
            measured
            and total is not None
            and total.cv is not None
            and total.cv > cv_threshold
        ),
        reps_flag=measured and n < MIN_REPETITIONS,
        records_checked=len(raw),
        missing=_missing(raw),
    )


def build_quality(
    campaign_id: str,
    *,
    campaign_state: str,
    runs: Iterable[PlannedRun],
    data: QualityInput,
    failures: FailureCounts,
    cv_threshold: float = DEFAULT_CV_THRESHOLD,
) -> CampaignQuality:
    """The report (module docstring). Pure and deterministic."""
    by_config_runs: dict[tuple[str, str], list[PlannedRun]] = {}
    run_states: Counter[str] = Counter()
    for run in runs:
        by_config_runs.setdefault((run.group_id, run.config_id), []).append(run)
        run_states[run.state] += 1
    by_config_raw: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for row in data.ok_rows:
        config = row.get("config_id")
        if isinstance(config, str):
            key = (str(row.get("group_id") or ""), config)
            by_config_raw.setdefault(key, []).append(row)
    configs = tuple(
        _config(
            key,
            by_config_runs.get(key, []),
            by_config_raw.get(key, []),
            latest_per_repetition(by_config_raw.get(key, [])),
            cv_threshold,
        )
        for key in sorted(by_config_runs.keys() | by_config_raw.keys())
    )
    pending = campaign_state not in _TERMINAL_CAMPAIGN
    rate = infra_failure_rate(failures)
    measured = [c for c in configs if c.state == "measured"]
    crit3 = bool(measured) and all(
        c.n >= MIN_REPETITIONS and c.total_ms is not None and c.total_ms.cv is not None
        for c in measured
    )
    summary = QualitySummary(
        campaign_state=campaign_state,
        pending=pending,
        runs_by_state={s: run_states.get(s, 0) for s in RUN_STATES},
        records=failures.records,
        bench_role_records=dict(sorted(data.roles.items())),
        measured_records=data.measured,
        infra_failures=failures.infra,
        owner_preempted=failures.preempted,
        infra_failure_rate=rate,
        condition_failures=dict(sorted(data.condition_failures.items())),
        workload_failures=dict(sorted(data.workload_failures.items())),
        rejected_ok=dict(sorted(data.rejected_ok.items())),
        other_failures=dict(sorted(data.other_failures.items())),
        crit1_infra_ok=None if pending else crit1_infra_ok(failures),
        crit3_reps_ok=None if pending else crit3,
        configs=len(configs),
        flagged_configs=sum(1 for c in configs if c.flagged),
        cv_threshold=cv_threshold,
    )
    return CampaignQuality(campaign_id=campaign_id, summary=summary, configs=configs)
