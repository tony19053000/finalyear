"""The D410 soak pass rules, with attribution - pure (inputs -> verdict dict).

Every rule returns ``{"id", "rule", "verdict", "summary", "details"}`` with
verdict ``PASS`` / ``FAIL`` / ``INCONCLUSIVE`` (never a vacuous PASS: a rule
without the data it needs is INCONCLUSIVE). The run verdict is FAIL if any
rule fails, else INCONCLUSIVE if any is, else PASS.

**Attribution.** A chaos action (from the action logs, outcome ``done`` or
``interrupted``) explains an event when the event is inside the action's
window - ``[start - 30 s skew, effect end + grace_s of its kind]`` - and on the
action's laptop, or anywhere when the action hit the controller laptop and its
kind is fabric-wide. A person's ``notes.json`` entry explains events in its
interval (power cut, sleep, router). What nothing explains counts.

Rules (D410 numbering):

1. ``lost_jobs`` - every ledger job is in the controller DB and terminal;
   meant-to-succeed jobs succeeded with exactly one committed attempt and an
   output, or failed with a recorded cause that a chaos action / note explains
   (a retryable failure with attempts left is never fine); meant-to-fail jobs
   failed; cancels are planned or chaos's; no job ended > 10 min after its
   deadline; no seq got two job ids.
2. ``crashes`` - every process restart / disappearance seen by a sampler is
   explained by an action whose kind restarts that role on that laptop.
3. ``memory`` - per process: quiet-2 median RSS < quiet-1 median + 10 % and
   the least-squares slope over the run (after warm-up) < 5 MB/h.
4. ``handles`` - per process: FDs (Linux) / handles (Windows) and threads,
   quiet-2 median <= quiet-1 median + 5 % (at least +1: integer counters).

   **Restarts** (rules 3-4): chaos restarts processes, and a restarted process
   starts small - comparing quiet 1 of the old one with quiet 2 of the new one
   would hide any leak. So the series is cut into **lifetimes** (by ``(pid,
   create_time)``), each minus its own warm-up; the slope is the pooled
   within-lifetime fit (each lifetime its own level), and when no single
   lifetime spans both quiet windows the growth compares the first and last
   third of the longest lifetime instead (``growth_basis`` in the details).
5. ``db_growth`` - DB bytes per job of the 2nd half of the load within 20 %
   of the 1st half (8 MiB noise floor); in the idle tail no table's live rows
   grow (time-driven telemetry tables reported, not judged); WAL <= 1.5 x
   ``max_wal_size`` + 64 MiB.
6. ``disk`` - every capped folder (agent cache) stays under its cap; every
   ``log*`` folder grows < 5 MB/h over the last quiet window (rotation works).
7. ``heartbeat`` - every OFFLINE transition and controller outage in the
   ``fabric_check soak`` record is explained.

Plus ``chaos`` (D411): at least one applied action per 45 min of the chaos
window, and ``coverage``: every sampler observed >= 90 % of the run.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from _sched.analysis import RETRYABLE
from _soak.chaos import CATALOGUE, LoggedAction
from _soak.inputs import SoakInputs
from _soak.load import MAX_ATTEMPTS, JobSpec
from _soak.stats import (
    Window,
    median,
    parse_ts,
    pct_change,
    pooled_slope_per_hour,
    slope_per_hour,
)

PASS: Final = "PASS"  # noqa: S105 - a verdict word
FAIL: Final = "FAIL"
INCONCLUSIVE: Final = "INCONCLUSIVE"
VERDICT_SCHEMA: Final = "meshforge.soak-verdict/1"

LATE_AFTER_DEADLINE_S: Final = 600.0
RSS_GROWTH_PCT: Final = 10.0
RSS_SLOPE_MB_H: Final = 5.0
COUNTER_GROWTH_PCT: Final = 5.0
DB_RATIO_TOL: Final = 0.20
DB_NOISE_BYTES: Final = 8 * 1024 * 1024
DB_MIN_JOBS_PER_HALF: Final = 20
WAL_FACTOR: Final = 1.5
WAL_MARGIN_BYTES: Final = 64 * 1024 * 1024
LOG_SLOPE_MB_H: Final = 5.0
CHAOS_EVERY_MIN: Final = 45.0
COVERAGE_MIN: Final = 0.90
MIN_WINDOW_SAMPLES: Final = 3
TIME_DRIVEN_TABLES: Final = frozenset(
    {"resource_snapshot", "network_link", "audit_event"}
)
"""Telemetry written on a clock, not per job (retention by days, D-0.3.11)."""
IDLE_SETTLE_S: Final = 60.0
MIB: Final = 1024 * 1024
TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})


def _verdict_of(parts: Iterable[str]) -> str:
    seen = set(parts)
    if FAIL in seen:
        return FAIL
    if INCONCLUSIVE in seen or not seen:
        return INCONCLUSIVE
    return PASS


def _rule(
    rule_id: str, rule: str, verdict: str, summary: str, details: Any
) -> dict[str, Any]:
    return {
        "id": rule_id,
        "rule": rule,
        "verdict": verdict,
        "summary": summary,
        "details": details,
    }


# ------------------------------------------------------------ attribution
class Attributor:
    """Which action or note explains an event at ``moment`` on laptop ``host``."""

    def __init__(self, inputs: SoakInputs) -> None:
        self.plan = inputs.plan
        self.applied = [a for a in inputs.actions if a.applied]
        self.notes = inputs.notes
        self.controller_host = inputs.plan.controller().label
        self._node_host: dict[str, str] = {}
        for t in inputs.plan.targets:
            for node_id in t.node_ids:
                self._node_host[node_id] = t.label
        by_hostname = {
            t.hostname.casefold(): t.label for t in inputs.plan.targets if t.hostname
        }
        for node in (inputs.final or {}).get("nodes") or []:
            node_id = str(node.get("node_id"))
            host = by_hostname.get(str(node.get("hostname") or "").casefold())
            if node_id not in self._node_host and host:
                self._node_host[node_id] = host

    def host_of_node(self, node_id: str | None) -> str | None:
        return self._node_host.get(node_id or "")

    def explain(
        self,
        moment: datetime,
        host: str | None,
        *,
        role: str | None = None,
        fails_jobs: bool = False,
        offline: bool = False,
    ) -> str | None:
        """The id of the action (or ``note:<reason>``) that explains it."""
        for action in self.applied:
            kind = CATALOGUE.get(action.kind)
            if kind is None:
                continue
            start, end = action.window()
            if not start <= moment <= end:
                continue
            same_host = host is not None and action.target == host
            wide = (
                action.target == self.controller_host and kind.fabric_wide_on_controller
            )
            if role is not None:
                if same_host and role in kind.restarts:
                    return action.id
                continue
            if fails_jobs and kind.fails_jobs and (same_host or wide):
                return action.id
            if offline and (same_host or wide) and action.kind != "cancel_mode3":
                return action.id
        for note in self.notes:
            if note.covers(moment, host):
                return f"note:{note.reason}"
        return None


# ------------------------------------------------------------ rule 1: jobs
def _terminal_at(view: Mapping[str, Any]) -> datetime | None:
    moments = [
        parse_ts(t.get("at"))
        for t in view.get("transitions") or []
        if t.get("to_state") in TERMINAL
    ]
    found = [m for m in moments if m is not None]
    return max(found) if found else None


def _committed(view: Mapping[str, Any]) -> int:
    roles = ("combine",) if "split" in (view.get("manifest") or {}) else ("run",)
    return sum(
        1
        for a in view.get("attempts") or []
        if a.get("state") == "done" and (a.get("role") or "run") in roles
    )


def _job_failure_explained(
    view: Mapping[str, Any], att: Attributor, end: datetime | None
) -> str | None:
    for attempt in view.get("attempts") or []:
        if attempt.get("state") not in ("failed", "lost", "fenced"):
            continue
        moment = parse_ts(attempt.get("ended_at")) or end
        if moment is None:
            continue
        why = att.explain(
            moment, att.host_of_node(attempt.get("node_id")), fails_jobs=True
        )
        if why:
            return why
    if end is not None:
        return att.explain(end, None, fails_jobs=True)
    return None


def rule_lost_jobs(inputs: SoakInputs, att: Attributor) -> dict[str, Any]:
    ledger = inputs.ledger
    final = inputs.final
    specs = {s.seq: s for s in inputs.specs}
    lost: list[dict[str, Any]] = []
    late: list[dict[str, Any]] = []
    explained: list[dict[str, Any]] = []
    outcomes: Counter[str] = Counter()
    chaos_cancels = {
        a.job_id for a in inputs.actions if a.kind == "cancel_mode3" and a.job_id
    }
    if final is None:
        return _rule(
            "lost_jobs", "D410-1", INCONCLUSIVE,
            "no jobs-final.json - run `soak_load.py collect` first", {},
        )  # fmt: skip
    views: Mapping[str, Any] = final.get("jobs") or {}
    outputs: Mapping[str, Any] = final.get("outputs") or {}
    for seq, entry in sorted(ledger.entries.items()):
        spec = specs.get(seq)
        if entry.job_id is None:
            outcomes["not_submitted" if entry.final_failure else "unresolved"] += 1
            continue
        view = views.get(entry.job_id)
        job = {"seq": seq, "job_id": entry.job_id, "kind": entry.kind}
        if not isinstance(view, Mapping):
            lost.append({**job, "reason": "not_in_controller"})
            continue
        state = str(view.get("state"))
        outcomes[state] += 1
        if state not in TERMINAL:
            lost.append({**job, "reason": "not_terminal", "state": state})
            continue
        end = _terminal_at(view)
        submitted = parse_ts(entry.submitted_at)
        if spec is not None and end is not None and submitted is not None:
            over = (end - submitted).total_seconds() - spec.deadline_s
            if over > LATE_AFTER_DEADLINE_S:
                late.append({**job, "over_deadline_s": round(over, 1)})
        reason = _job_problem(
            spec, entry.cancel_status, chaos_cancels, view, outputs, att, end
        )
        if reason is None:
            continue
        if reason.startswith("explained:"):
            explained.append({**job, "by": reason.removeprefix("explained:"),
                              "failure_type": view.get("failure_type")})  # fmt: skip
        else:
            lost.append({**job, "reason": reason, "state": state,
                         "failure_type": view.get("failure_type")})  # fmt: skip
    unresolved = ledger.unresolved()
    details = {
        "jobs": len(ledger.job_ids()),
        "planned_jobs": len(inputs.specs),
        "states": dict(sorted(outcomes.items())),
        "lost": lost,
        "late": late,
        "failed_explained": explained,
        "duplicate_submissions": ledger.problems,
        "unresolved_submissions": unresolved,
        "outputs_checked": sum(1 for o in outputs.values() if o.get("checked")),
    }
    if lost or late or ledger.problems:
        verdict = FAIL
    elif not ledger.job_ids() or unresolved:
        verdict = INCONCLUSIVE
    else:
        verdict = PASS
    summary = (
        f"{len(ledger.job_ids())} jobs: {len(lost)} lost, {len(late)} late, "
        f"{len(explained)} failed with an explained cause"
    )
    return _rule("lost_jobs", "D410-1", verdict, summary, details)


def _job_problem(
    spec: JobSpec | None,
    cancel_status: int | None,
    chaos_cancels: set[str],
    view: Mapping[str, Any],
    outputs: Mapping[str, Any],
    att: Attributor,
    end: datetime | None,
) -> str | None:
    """``None`` = fine; ``explained:<why>`` = failed but explained; else the
    reason it counts as lost."""
    state = view.get("state")
    job_id = view.get("job_id")
    if (
        spec is not None
        and spec.cancel_after_s is not None
        and cancel_status is not None
    ):
        return None  # a planned cancel: any terminal state (the cancel races the end)
    if state == "cancelled":
        return None if job_id in chaos_cancels else "unexplained_cancel"
    if state == "succeeded":
        if spec is not None and spec.exit_code != 0:
            return "wrong_outcome"
        if _committed(view) != 1:
            return "duplicate_or_missing_commit"
        if not view.get("output_digest"):
            return "no_output"
        check = outputs.get(str(job_id)) or {}
        if check.get("checked") and not check.get("ok"):
            return "bad_output"
        return None
    # failed
    failure = view.get("failure_type")
    if spec is not None and spec.exit_code != 0 and failure == "user_error":
        return None
    if not failure:
        return "failed_without_cause"
    attempts = [
        a for a in view.get("attempts") or [] if (a.get("role") or "run") == "run"
    ]
    if (
        failure in RETRYABLE
        and len(attempts) < MAX_ATTEMPTS
        and "split" not in (view.get("manifest") or {})
    ):
        return "retry_not_done"
    why = _job_failure_explained(view, att, end)
    return f"explained:{why}" if why else "unexplained_failure"


# ------------------------------------------------------------ rule 2: crashes
def rule_crashes(inputs: SoakInputs, att: Attributor) -> dict[str, Any]:
    crashes: list[dict[str, Any]] = []
    planned: list[dict[str, Any]] = []
    missing_open: dict[tuple[str, str], dict[str, Any]] = {}
    observed = 0
    for label, records in sorted(inputs.samples.items()):
        for r in records:
            if r.get("type") != "event":
                continue
            kind = r.get("kind")
            key = (label, str(r.get("target")))
            if kind == "process_missing":
                missing_open[key] = r
                continue
            if kind == "process_found":
                missing_open.pop(key, None)
                continue
            if kind != "process_restarted":
                continue
            observed += 1
            first_seen = missing_open.pop(key, None)
            moment = parse_ts((first_seen or r).get("at"))
            row = {
                "sampler": label,
                "target": r.get("target"),
                "role": r.get("role"),
                "host": r.get("host"),
                "at": (first_seen or r).get("at"),
                "old_pid": r.get("old_pid"),
                "pid": r.get("pid"),
            }
            why = (
                att.explain(moment, str(r.get("host")), role=str(r.get("role")))
                if moment
                else None
            )
            (planned if why else crashes).append({**row, "by": why} if why else row)
    never_back = [
        {"sampler": k[0], "target": k[1], "since": v.get("at"), "role": v.get("role")}
        for k, v in sorted(missing_open.items())
    ]
    if not inputs.samples:
        return _rule("crashes", "D410-2", INCONCLUSIVE, "no sampler files", {})
    verdict = FAIL if crashes or never_back else PASS
    return _rule(
        "crashes",
        "D410-2",
        verdict,
        f"{observed} restarts seen: {len(planned)} caused by chaos, {len(crashes)} "
        f"unexplained; {len(never_back)} processes never came back",
        {"unexplained": crashes, "never_returned": never_back, "explained": planned},
    )


# ------------------------------------------------------- rules 3 + 4: series
def _series(
    inputs: SoakInputs,
) -> dict[str, dict[str, Any]]:
    """``label/target`` -> {role, host, os, points: [(offset_s, proc dict)]}."""
    out: dict[str, dict[str, Any]] = {}
    for label, records in sorted(inputs.samples.items()):
        header = next((r for r in records if r.get("type") == "header"), {})
        targets = {
            p["name"]: p for p in header.get("procs") or [] if isinstance(p, dict)
        }
        for r in records:
            if r.get("type") != "tick":
                continue
            moment = parse_ts(r.get("at"))
            if moment is None:
                continue
            offset = inputs.plan.offset_s(moment)
            for name, proc in (r.get("procs") or {}).items():
                if not isinstance(proc, dict):
                    continue
                key = f"{label}/{name}"
                meta = targets.get(name, {})
                series = out.setdefault(
                    key,
                    {"role": meta.get("role"), "host": meta.get("host"),
                     "os": header.get("os"), "points": []},
                )  # fmt: skip
                series["points"].append((offset, proc))
    return out


def _in(
    points: Sequence[tuple[float, dict[str, Any]]], window: Window, metric: str
) -> list[float]:
    return [
        float(p[metric])
        for t, p in points
        if window.holds(t) and p.get(metric) is not None
    ]


Points = Sequence[tuple[float, dict[str, Any]]]


def _lifetimes(points: Points) -> list[list[tuple[float, dict[str, Any]]]]:
    """The series cut where the process identity ``(pid, create_time)``
    changes: one list per process lifetime, in time order."""
    out: list[list[tuple[float, dict[str, Any]]]] = []
    last: tuple[Any, Any] | None = None
    for t, p in sorted(points, key=lambda tp: tp[0]):
        ident = (p.get("pid"), p.get("create_time"))
        if not out or ident != last:
            out.append([])
            last = ident
        out[-1].append((t, p))
    return out


def _settled(
    lives: list[list[tuple[float, dict[str, Any]]]], warmup_s: float, run_start_s: float
) -> list[list[tuple[float, dict[str, Any]]]]:
    """Each lifetime without its own warm-up: the plan's warm-up for a process
    that was already running at the start, ``warmup_s`` after its first sample
    for one (re)started during the run - a fresh process grows while it fills
    its caches, which is not a leak."""
    out: list[list[tuple[float, dict[str, Any]]]] = []
    for i, life in enumerate(lives):
        first = life[0][0]
        cut = run_start_s + warmup_s if i == 0 else first + warmup_s
        out.append([(t, p) for t, p in life if t >= cut])
    return out


def _growth_windows(
    pts: Points,
    lives: list[list[tuple[float, dict[str, Any]]]],
    q1: Window,
    q2: Window,
    metric: str,
) -> tuple[str, list[float], list[float]]:
    """Which two samples to compare for "quiet-1 -> quiet-2" growth.

    ``quiet_windows`` when one process lifetime covers both quiet windows (the
    D410 rule as written). Otherwise a restart in between would compare two
    different processes - the new one starts small, so any leak would vanish -
    and the comparison moves to the **longest settled lifetime**: its first
    third vs its last third (each at most a quiet window long). ``none`` when
    no lifetime is long enough (>= 3 x MIN_WINDOW_SAMPLES samples)."""
    a, b = _in(pts, q1, metric), _in(pts, q2, metric)
    in_q = {
        (p.get("pid"), p.get("create_time"))
        for t, p in pts
        if (q1.holds(t) or q2.holds(t)) and p.get(metric) is not None
    }
    if len(in_q) <= 1:
        return "quiet_windows", a, b
    usable = [
        [(t, float(p[metric])) for t, p in life if p.get(metric) is not None]
        for life in lives
    ]
    usable = [u for u in usable if len(u) >= 3 * MIN_WINDOW_SAMPLES]
    if not usable:
        return "none", [], []
    longest = max(usable, key=lambda u: (u[-1][0] - u[0][0], len(u)))
    span = longest[-1][0] - longest[0][0]
    length = min(span / 3.0, q1.end_s - q1.start_s)
    head = [v for t, v in longest if t < longest[0][0] + length]
    tail = [v for t, v in longest if t > longest[-1][0] - length]
    return "longest_lifetime", head, tail


def rule_memory(inputs: SoakInputs) -> dict[str, Any]:
    plan = inputs.plan
    q1, q2 = plan.quiet1(), plan.quiet2()
    fit = Window("fit", 0.0, plan.hours * 3600.0)
    warmup_s = plan.warmup_min * 60.0
    rows: list[dict[str, Any]] = []
    for key, series in sorted(_series(inputs).items()):
        pts = [(t, p) for t, p in series["points"] if p.get("rss_bytes")]
        lives = _settled(_lifetimes(pts), warmup_s, 0.0)
        slope = pooled_slope_per_hour(
            [
                [(t, float(p["rss_bytes"]) / MIB) for t, p in life if fit.holds(t)]
                for life in lives
            ]
        )
        q_a, q_b = _in(pts, q1, "rss_bytes"), _in(pts, q2, "rss_bytes")
        basis, a, b = _growth_windows(pts, lives, q1, q2, "rss_bytes")
        m1, m2 = median(a), median(b)
        growth = pct_change(m1, m2) if m1 is not None and m2 is not None else None
        if (
            len(q_a) < MIN_WINDOW_SAMPLES
            or len(q_b) < MIN_WINDOW_SAMPLES
            or slope is None
        ):
            verdict = INCONCLUSIVE
        elif (
            growth is not None and growth >= RSS_GROWTH_PCT
        ) or slope >= RSS_SLOPE_MB_H:
            verdict = FAIL
        else:
            verdict = PASS
        rows.append(
            {
                "series": key,
                "role": series["role"],
                "lifetimes": len(lives),
                "growth_basis": basis,
                "quiet1_median_mib": None if m1 is None else round(m1 / MIB, 2),
                "quiet2_median_mib": None if m2 is None else round(m2 / MIB, 2),
                "growth_pct": None if growth is None else round(growth, 2),
                "slope_mb_h": None if slope is None else round(slope, 3),
                "samples": [len(a), len(b)],
                "verdict": verdict,
            }
        )
    verdict = _verdict_of(r["verdict"] for r in rows)
    worst = max((r["growth_pct"] or 0.0 for r in rows), default=0.0)
    return _rule(
        "memory", "D410-3", verdict,
        f"{len(rows)} processes; worst quiet-to-quiet RSS growth {worst:.2f} %",
        rows,
    )  # fmt: skip


def rule_handles(inputs: SoakInputs) -> dict[str, Any]:
    plan = inputs.plan
    q1, q2 = plan.quiet1(), plan.quiet2()
    warmup_s = plan.warmup_min * 60.0
    rows: list[dict[str, Any]] = []
    for key, series in sorted(_series(inputs).items()):
        pts = series["points"]
        lives = _settled(_lifetimes(pts), warmup_s, 0.0)
        counter = "handles" if series.get("os") == "nt" else "fds"
        for metric in (counter, "threads"):
            q_a, q_b = _in(pts, q1, metric), _in(pts, q2, metric)
            basis, a, b = _growth_windows(pts, lives, q1, q2, metric)
            m1, m2 = median(a), median(b)
            if (
                len(q_a) < MIN_WINDOW_SAMPLES
                or len(q_b) < MIN_WINDOW_SAMPLES
                or m1 is None
                or m2 is None
            ):
                verdict, bound = INCONCLUSIVE, None
            else:
                bound = m1 + max(m1 * COUNTER_GROWTH_PCT / 100.0, 1.0)
                verdict = PASS if m2 <= bound else FAIL
            rows.append(
                {
                    "series": key,
                    "metric": metric,
                    "lifetimes": len(lives),
                    "growth_basis": basis,
                    "quiet1_median": m1,
                    "quiet2_median": m2,
                    "bound": None if bound is None else round(bound, 2),
                    "samples": [len(a), len(b)],
                    "verdict": verdict,
                    **(
                        {}
                        if q_a or q_b
                        else {"note": "not measured (needs root / Administrator?)"}
                    ),
                }
            )
    return _rule(
        "handles", "D410-4", _verdict_of(r["verdict"] for r in rows),
        f"{len(rows)} counters (FDs on Linux, handles on Windows, threads)", rows,
    )  # fmt: skip


# ------------------------------------------------------------ rule 5: DB
def _db_points(inputs: SoakInputs) -> list[tuple[float, dict[str, Any]]]:
    out: list[tuple[float, dict[str, Any]]] = []
    for records in inputs.samples.values():
        for r in records:
            db = r.get("db") if r.get("type") == "tick" else None
            moment = parse_ts(r.get("at"))
            if isinstance(db, dict) and db.get("ok") and moment is not None:
                out.append((inputs.plan.offset_s(moment), db))
    return sorted(out, key=lambda p: p[0])


def _nearest(
    points: Sequence[tuple[float, dict[str, Any]]], t: float
) -> tuple[float, dict[str, Any]] | None:
    return min(points, key=lambda p: abs(p[0] - t)) if points else None


def rule_db(inputs: SoakInputs) -> dict[str, Any]:
    plan = inputs.plan
    points = _db_points(inputs)
    if len(points) < 3:
        return _rule("db_growth", "D410-5", INCONCLUSIVE,
                     "fewer than 3 database samples (is --db-url set on the controller laptop?)",
                     {"samples": len(points)})  # fmt: skip
    load_end = plan.hours * 3600.0
    in_load = [p for p in points if p[0] <= load_end]
    parts: list[str] = []
    details: dict[str, Any] = {"samples": len(points)}
    # (a) bytes per job, first half vs second half of the load
    first, mid, last = (
        in_load[0] if in_load else None,
        _nearest(in_load, load_end / 2),
        in_load[-1] if in_load else None,
    )
    ratio_part = INCONCLUSIVE
    if (
        first
        and mid
        and last
        and all(p[1].get("jobs") is not None for p in (first, mid, last))
    ):
        j1 = mid[1]["jobs"] - first[1]["jobs"]
        j2 = last[1]["jobs"] - mid[1]["jobs"]
        b1 = mid[1]["db_bytes"] - first[1]["db_bytes"]
        b2 = last[1]["db_bytes"] - mid[1]["db_bytes"]
        ratio = {"jobs": [j1, j2], "bytes": [b1, b2]}
        if j1 >= DB_MIN_JOBS_PER_HALF and j2 >= DB_MIN_JOBS_PER_HALF:
            expected = b1 / j1 * j2
            tolerance = max(abs(expected) * DB_RATIO_TOL, DB_NOISE_BYTES)
            ratio.update(
                bytes_per_job=[round(b1 / j1), round(b2 / j2)],
                expected_2nd_half_bytes=round(expected),
                tolerance_bytes=round(tolerance),
            )
            ratio_part = PASS if abs(b2 - expected) <= tolerance else FAIL
        details["bytes_per_job"] = {**ratio, "verdict": ratio_part}
    else:
        details["bytes_per_job"] = {"verdict": INCONCLUSIVE, "note": "no job counts"}
    parts.append(ratio_part)
    # (b) idle tail: nothing grows without jobs
    parts.append(_idle_tail(inputs, points, details))
    # (c) WAL bounded
    wal = [(p[1].get("wal_bytes"), p[1].get("max_wal_bytes")) for p in points]
    measured = [(w, m) for w, m in wal if w is not None and m is not None]
    if measured:
        worst = max(w for w, _ in measured)
        bound = max(m for _, m in measured) * WAL_FACTOR + WAL_MARGIN_BYTES
        wal_part = PASS if worst <= bound else FAIL
        details["wal"] = {
            "max_bytes": worst,
            "bound_bytes": round(bound),
            "verdict": wal_part,
        }
    else:
        wal_part = INCONCLUSIVE
        details["wal"] = {"verdict": INCONCLUSIVE,
                          "note": "WAL size not readable (connect as meshforge_admin)"}  # fmt: skip
    parts.append(wal_part)
    db_bytes = [p[1]["db_bytes"] for p in points]
    return _rule(
        "db_growth", "D410-5", _verdict_of(parts),
        f"database {db_bytes[0] / MIB:.1f} -> {db_bytes[-1] / MIB:.1f} MiB",
        details,
    )  # fmt: skip


def _idle_tail(
    inputs: SoakInputs,
    points: list[tuple[float, dict[str, Any]]],
    details: dict[str, Any],
) -> str:
    last_end: datetime | None = None
    for view in ((inputs.final or {}).get("jobs") or {}).values():
        if isinstance(view, Mapping):
            end = _terminal_at(view)
            if end is not None and (last_end is None or end > last_end):
                last_end = end
    if last_end is None:
        details["idle_tail"] = {
            "verdict": INCONCLUSIVE,
            "note": "no collected job end times",
        }
        return INCONCLUSIVE
    idle_from = inputs.plan.offset_s(last_end) + IDLE_SETTLE_S
    idle = [p for p in points if p[0] >= max(idle_from, inputs.plan.tail().start_s)]
    if len(idle) < 2:
        details["idle_tail"] = {
            "verdict": INCONCLUSIVE,
            "note": "fewer than 2 DB samples after the last job ended (longer tail_h?)",
        }
        return INCONCLUSIVE
    a, b = idle[0][1].get("tables") or {}, idle[-1][1].get("tables") or {}
    hours = max((idle[-1][0] - idle[0][0]) / 3600.0, 1e-9)
    grew: dict[str, int] = {}
    clocked: dict[str, float] = {}
    for name in sorted(set(a) | set(b)):
        delta = int((b.get(name) or {}).get("live", 0)) - int(
            (a.get(name) or {}).get("live", 0)
        )
        if name in TIME_DRIVEN_TABLES:
            clocked[name] = round(delta / hours, 1)
        elif delta > 0:
            grew[name] = delta
    verdict = FAIL if grew else PASS
    details["idle_tail"] = {
        "from_h": round(idle[0][0] / 3600.0, 3),
        "to_h": round(idle[-1][0] / 3600.0, 3),
        "grew_without_jobs": grew,
        "time_driven_rows_per_h": clocked,
        "verdict": verdict,
    }
    return verdict


# ------------------------------------------------------------ rule 6: disk
def rule_disk(inputs: SoakInputs) -> dict[str, Any]:
    q2 = inputs.plan.quiet2()
    rows: list[dict[str, Any]] = []
    for label, records in sorted(inputs.samples.items()):
        series: dict[str, list[tuple[float, dict[str, Any]]]] = {}
        for r in records:
            moment = parse_ts(r.get("at"))
            if r.get("type") != "tick" or moment is None:
                continue
            for name, d in (r.get("dirs") or {}).items():
                if isinstance(d, dict) and d.get("bytes") is not None:
                    series.setdefault(name, []).append(
                        (inputs.plan.offset_s(moment), d)
                    )
        for name, pts in sorted(series.items()):
            cap = pts[-1][1].get("cap_bytes")
            peak = max(int(d["bytes"]) for _, d in pts)
            row: dict[str, Any] = {
                "series": f"{label}/{name}",
                "max_bytes": peak,
                "cap_bytes": cap,
            }
            if cap is not None:
                row["verdict"] = PASS if peak <= int(cap) else FAIL
            elif name.casefold().startswith("log"):
                slope = slope_per_hour(
                    [(t, int(d["bytes"]) / MIB) for t, d in pts if q2.holds(t)]
                )
                row["quiet2_slope_mb_h"] = None if slope is None else round(slope, 3)
                row["verdict"] = (
                    INCONCLUSIVE
                    if slope is None
                    else PASS
                    if slope < LOG_SLOPE_MB_H
                    else FAIL
                )
            else:
                row["verdict"] = "INFO"
            rows.append(row)
    judged = [r["verdict"] for r in rows if r["verdict"] != "INFO"]
    return _rule(
        "disk", "D410-6", _verdict_of(judged),
        f"{len(judged)} folders judged ({sum(1 for v in judged if v == PASS)} PASS)",
        rows,
    )  # fmt: skip


# ------------------------------------------------------------ rule 7: heartbeat
def rule_heartbeat(inputs: SoakInputs, att: Attributor) -> dict[str, Any]:
    fabric = inputs.fabric
    if fabric is None:
        return _rule("heartbeat", "D410-7", INCONCLUSIVE,
                     "no fabric-soak.checkpoint.json (run fabric_check.py soak)", {})  # fmt: skip
    host_by_node = {n: att.host_of_node(n) for n in (fabric.get("nodes") or {})}
    unexplained: list[dict[str, Any]] = []
    explained: list[dict[str, Any]] = []
    for tr in fabric.get("transitions") or []:
        if tr.get("new") != "offline" or tr.get("field") not in ("state", "appeared"):
            continue
        moment = parse_ts(tr.get("at"))
        host = host_by_node.get(str(tr.get("node_id")))
        why = att.explain(moment, host, offline=True) if moment else None
        row = {"at": tr.get("at"), "node_id": tr.get("node_id"),
               "hostname": tr.get("hostname"), "host": host}  # fmt: skip
        (explained if why else unexplained).append({**row, "by": why} if why else row)
    outages: list[dict[str, Any]] = []
    controller = inputs.plan.controller().label
    for outage in fabric.get("outages") or []:
        moment = parse_ts(outage.get("from"))
        why = att.explain(moment, controller, offline=True) if moment else None
        if why is None:
            outages.append(outage)
    verdict = FAIL if unexplained or outages else PASS
    return _rule(
        "heartbeat", "D410-7", verdict,
        f"{len(explained) + len(unexplained)} OFFLINE events, {len(unexplained)} unexplained; "
        f"{len(outages)} unexplained controller outages",
        {"unexplained": unexplained, "unexplained_outages": outages, "explained": explained,
         "observed_h": round(float(fabric.get("elapsed_s") or 0) / 3600.0, 3)},
    )  # fmt: skip


# ------------------------------------------------------ chaos + coverage
def rule_chaos(inputs: SoakInputs) -> dict[str, Any]:
    window = inputs.plan.chaos_window()
    needed = max(1, math.ceil((window.end_s - window.start_s) / 60.0 / CHAOS_EVERY_MIN))
    applied = [a for a in inputs.actions if a.applied]
    by_kind = Counter(a.kind for a in applied)
    by_outcome = Counter(a.outcome for a in inputs.actions)
    verdict = PASS if len(applied) >= needed else FAIL
    return _rule(
        "chaos", "D411", verdict,
        f"{len(applied)} chaos actions applied (>= {needed} needed)",
        {"needed": needed, "applied": len(applied), "by_kind": dict(sorted(by_kind.items())),
         "by_outcome": dict(sorted(by_outcome.items())),
         "actions": [_action_row(a) for a in inputs.actions]},
    )  # fmt: skip


def _action_row(a: LoggedAction) -> dict[str, Any]:
    return {
        "id": a.id,
        "kind": a.kind,
        "target": a.target,
        "started_at": a.started_at.isoformat(),
        "effect_until": a.effect_until.isoformat(),
        "outcome": a.outcome,
        "job_id": a.job_id,
        "detail": a.detail,
    }


def rule_coverage(inputs: SoakInputs) -> dict[str, Any]:
    plan = inputs.plan
    total = (plan.end - plan.t0).total_seconds()
    rows: list[dict[str, Any]] = []
    for label, records in sorted(inputs.samples.items()):
        header = next((r for r in records if r.get("type") == "header"), {})
        interval = float(header.get("interval_s") or 60.0)
        times = sorted(
            plan.offset_s(m)
            for r in records
            if r.get("type") == "tick" and (m := parse_ts(r.get("at"))) is not None
        )
        covered = 0.0
        biggest = 0.0
        for a, b in itertools.pairwise(times):
            gap = b - a
            biggest = max(biggest, gap)
            covered += min(gap, 3 * interval)
        share = covered / total if total > 0 else 0.0
        rows.append(
            {
                "sampler": label,
                "ticks": len(times),
                "covered_share": round(share, 4),
                "largest_gap_s": round(biggest, 1),
                "verdict": PASS if share >= COVERAGE_MIN else INCONCLUSIVE,
            }
        )
    return _rule(
        "coverage", "D410", _verdict_of(r["verdict"] for r in rows),
        f"{len(rows)} samplers", rows,
    )  # fmt: skip


# ------------------------------------------------------------ the verdict
def evaluate(
    inputs: SoakInputs, *, generated_at: datetime, meta: Mapping[str, Any]
) -> dict[str, Any]:
    att = Attributor(inputs)
    rules = [
        rule_lost_jobs(inputs, att),
        rule_crashes(inputs, att),
        rule_memory(inputs),
        rule_handles(inputs),
        rule_db(inputs),
        rule_disk(inputs),
        rule_heartbeat(inputs, att),
        rule_chaos(inputs),
        rule_coverage(inputs),
    ]
    plan = inputs.plan
    return {
        "schema": VERDICT_SCHEMA,
        "kind": "soak",
        "criterion": "V1.0-c2",
        "verdict": _verdict_of(r["verdict"] for r in rules),
        "generated_at": generated_at.isoformat(),
        "run": {
            **plan.as_dict(),
            "end": plan.end.isoformat(),
            "compressed": plan.hours < 72,
        },
        "rules": rules,
        "problems": inputs.problems,
        **dict(meta),
    }
