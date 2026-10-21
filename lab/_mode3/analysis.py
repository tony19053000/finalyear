"""The V0.10 lab numbers for success criteria 2 and 3 (D274) - pure.

Inputs are rows of the two telemetry tables (``compute telemetry export``
``--table runs`` / ``--table chunks``, schema v3) and the job ids the lab run
named: one **split** job (Mode 3 over a node set) and the **single-node** runs
of the same workload ("don't split": one lane, one chunk - Mode 1 / Local),
measured in the same session.

* ``r_n`` - each node's **items/s** from its single-node run(s): committed
  (``won``) chunk items over their agent-measured ``run_ms`` (several runs on
  one node are pooled). D274: "each node's steady-state items/s measured in
  the same session by single-node runs".
* **Ideal bound** (C2) = ``items / sum(r_n over the split job's nodes)`` + the
  largest ``stage_ms + env_ms`` of the split job's lanes + the split job's
  combine run time (``split_combine_ms``): the unavoidable work D274 adds.
* **Makespan** (C2) = the split job's ``split_makespan_ms`` (first lane
  RUNNING -> job end, controller clock). Ratio = makespan / bound; **judged**
  (pass at <= 1.25) only when the best single-node job time is >= 300 s;
  shorter runs are ``REPORTED`` (D274), never a pass.
* **Speedup** (C3) = best single-node ``split_job_ms`` / the split job's
  ``split_job_ms`` (both: first lane dispatched -> job end, same clock, same
  code path - D269). ``rate_ratio`` = ``sum(r_n) / max(r_n)`` - the most any
  split could gain. Pass when speedup >= 0.8 x rate_ratio, **and** on exactly
  3 nodes with rate_ratio >= 2.5 also speedup >= 2.0 (D274).

A missing input (no single-node run for a node of the set, no job row, no
committed chunk) is ``INCONCLUSIVE`` with the reason - never a pass.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

__all__ = [
    "C2_JUDGED_MIN_SINGLE_S",
    "C2_MAX_RATIO",
    "FAIL",
    "INCONCLUSIVE",
    "PASS",
    "REPORTED",
    "Mode3Numbers",
    "evaluate",
]

PASS: Final = "PASS"  # noqa: S105 - a verdict, not a password
FAIL: Final = "FAIL"
REPORTED: Final = "REPORTED"
INCONCLUSIVE: Final = "INCONCLUSIVE"
C2_MAX_RATIO: Final = 1.25
C2_JUDGED_MIN_SINGLE_S: Final = 300.0
C3_FRACTION: Final = 0.8
C3_TRIO_RATIO: Final = 2.5
C3_TRIO_SPEEDUP: Final = 2.0


@dataclass(frozen=True)
class Mode3Numbers:
    """Everything the checklist asks to paste back for C2/C3."""

    split_job_id: str
    node_set: tuple[str, ...]
    items: int | None
    rates: dict[str, float] = field(default_factory=dict)
    """node -> items/s from its single-node run(s)."""
    sum_rate: float | None = None
    rate_ratio: float | None = None
    ideal_bound_s: float | None = None
    recorded_bound_s: float | None = None
    """The record's own ``split_ideal_bound_s`` (from the split job's chunks)."""
    makespan_s: float | None = None
    makespan_ratio: float | None = None
    split_job_s: float | None = None
    best_single_s: float | None = None
    best_single_node: str | None = None
    speedup: float | None = None
    c2: str = INCONCLUSIVE
    c3: str = INCONCLUSIVE
    problems: tuple[str, ...] = ()

    def as_json(self) -> dict[str, Any]:
        out = {k: getattr(self, k) for k in self.__dataclass_fields__}
        out["node_set"] = list(self.node_set)
        out["problems"] = list(self.problems)
        return out


def _node_set(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return ()
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return tuple(value)
    return ()


def _job_row(
    runs: Iterable[Mapping[str, Any]], job_id: str
) -> Mapping[str, Any] | None:
    """The job row of a split job: its combine record that ended it ``ok``."""
    for row in runs:
        if (
            row.get("job_id") == job_id
            and row.get("attempt_role") == "combine"
            and row.get("failure_class") == "ok"
        ):
            return row
    return None


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _rates(
    chunks: Sequence[Mapping[str, Any]], single_ids: Iterable[str]
) -> dict[str, float]:
    items: dict[str, float] = defaultdict(float)
    seconds: dict[str, float] = defaultdict(float)
    wanted = set(single_ids)
    for c in chunks:
        if c.get("job_id") not in wanted or not c.get("won"):
            continue
        run_ms, n = _num(c.get("run_ms")), _num(c.get("items"))
        if run_ms is None or n is None or run_ms <= 0:
            continue
        node = str(c.get("node_id"))
        items[node] += n
        seconds[node] += run_ms / 1000.0
    return {n: items[n] / seconds[n] for n in sorted(items) if seconds[n] > 0}


def evaluate(
    runs: Sequence[Mapping[str, Any]],
    chunks: Sequence[Mapping[str, Any]],
    *,
    split_job_id: str,
    single_job_ids: Sequence[str],
) -> Mode3Numbers:
    """C2 / C3 numbers and verdicts (module docstring)."""
    problems: list[str] = []
    row = _job_row(runs, split_job_id)
    if row is None:
        return Mode3Numbers(
            split_job_id,
            (),
            None,
            problems=(f"no ok job row (combine record) for {split_job_id}",),
        )
    node_set = _node_set(row.get("split_node_set"))
    items_value = _num(row.get("split_items"))
    items = None if items_value is None else int(items_value)
    if len(node_set) < 2:
        problems.append(f"{split_job_id} is not a split over >= 2 nodes: {node_set}")
    rates = _rates(chunks, single_job_ids)
    missing = [n for n in node_set if n not in rates]
    if missing:
        problems.append(f"no single-node run measured for {missing}")
    lanes = [
        r
        for r in runs
        if r.get("job_id") == split_job_id and r.get("attempt_role") == "lane"
    ]
    starts = [
        ((_num(r.get("stage_ms")) or 0.0) + (_num(r.get("env_ms")) or 0.0)) / 1000.0
        for r in lanes
    ]
    combine_s = (_num(row.get("split_combine_ms")) or 0.0) / 1000.0
    makespan_ms = _num(row.get("split_makespan_ms"))
    job_ms = _num(row.get("split_job_ms"))
    if makespan_ms is None or job_ms is None:
        problems.append("the job row has no split_makespan_ms / split_job_ms")
    lane_node = {
        str(c.get("job_id")): str(c.get("node_id")) for c in chunks if c.get("won")
    }  # a single-node job's lane node (its combine may run elsewhere)
    singles = [
        (s / 1000.0, lane_node.get(sid, str(r.get("node_id"))))
        for sid in single_job_ids
        if (r := _job_row(runs, sid)) is not None
        and (s := _num(r.get("split_job_ms"))) is not None
    ]
    if len(singles) < len(single_job_ids):
        problems.append("a single-node job id has no ok job row with split_job_ms")
    best = min(singles) if singles else None

    used = [rates[n] for n in node_set if n in rates]
    sum_rate = sum(used) if used and not missing else None
    rate_ratio = None if sum_rate is None else sum_rate / max(used)
    bound = None
    if sum_rate and items is not None:
        bound = items / sum_rate + (max(starts) if starts else 0.0) + combine_s
    makespan_s = None if makespan_ms is None else makespan_ms / 1000.0
    ratio = None if bound is None or makespan_s is None else makespan_s / bound
    split_s = None if job_ms is None else job_ms / 1000.0
    speedup = None if best is None or not split_s else best[0] / split_s

    c2 = INCONCLUSIVE
    if ratio is not None and best is not None and not problems:
        if best[0] < C2_JUDGED_MIN_SINGLE_S:
            c2 = REPORTED
        else:
            c2 = PASS if ratio <= C2_MAX_RATIO else FAIL
    c3 = INCONCLUSIVE
    if speedup is not None and rate_ratio is not None and not problems:
        ok = speedup >= C3_FRACTION * rate_ratio
        if len(node_set) == 3 and rate_ratio >= C3_TRIO_RATIO:
            ok = ok and speedup >= C3_TRIO_SPEEDUP
        c3 = PASS if ok else FAIL
    return Mode3Numbers(
        split_job_id=split_job_id,
        node_set=node_set,
        items=items,
        rates=rates,
        sum_rate=sum_rate,
        rate_ratio=rate_ratio,
        ideal_bound_s=bound,
        recorded_bound_s=_num(row.get("split_ideal_bound_s")),
        makespan_s=makespan_s,
        makespan_ratio=ratio,
        split_job_s=split_s,
        best_single_s=None if best is None else best[0],
        best_single_node=None if best is None else best[1],
        speedup=speedup,
        c2=c2,
        c3=c3,
        problems=tuple(problems),
    )
