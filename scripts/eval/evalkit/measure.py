"""One job's controller record -> its per-job metrics (D417).

Inputs are exactly what the REST API serves - ``GET /v1/jobs/{id}`` and
``GET /v1/jobs/{id}/decisions`` - so a measurement can be recomputed from the
controller at any time (a resumed campaign re-attaches to a job that finished
while the runner was down and measures it the same way).

All times are the **controller's** timestamps (one clock), never the
runner's: ``end_to_end_ms`` = the terminal transition's time minus the job's
``created_at`` (submit -> result available, what a user waits for);
``queue_ms`` = created -> first ``preparing`` transition (planning + waiting
for a slot); ``run_ms`` = the final whole-job attempt's ``run`` phase (the
program itself); ``overhead_ms`` = ``end_to_end_ms - run_ms``: everything the
fabric adds around the program (RQ4) - only for a job that ran whole (a split
job's lanes overlap, so it has no single ``run`` to subtract; ``None``, with
the reason in ``overhead_basis``).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from typing import Any, Final

__all__ = ["TERMINAL", "measure_job"]

TERMINAL: Final = frozenset({"succeeded", "failed", "cancelled"})


def _when(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return parsed


def _ms(start: dt.datetime | None, end: dt.datetime | None) -> int | None:
    if start is None or end is None:
        return None
    return round((end - start).total_seconds() * 1000.0)


def _finished_at(job: Mapping[str, Any]) -> dt.datetime | None:
    ends = [
        _when(t.get("at"))
        for t in job.get("transitions", ())
        if t.get("to_state") in TERMINAL
    ]
    found = [e for e in ends if e is not None]
    return max(found) if found else None


def _first(job: Mapping[str, Any], state: str) -> dt.datetime | None:
    hits = [
        _when(t.get("at"))
        for t in job.get("transitions", ())
        if t.get("to_state") == state
    ]
    found = [h for h in hits if h is not None]
    return min(found) if found else None


def _chosen(decision: Mapping[str, Any]) -> Mapping[str, Any] | None:
    rows: Sequence[Mapping[str, Any]] = decision.get("candidates", ())
    for row in rows:
        if row.get("rank") == 1:
            return row
    return None


def _dispatched(decisions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    found = [d for d in decisions if d.get("outcome") == "dispatched"]
    return None if not found else found[-1]


def _run_attempt(job: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The whole-job attempt that ended ``done`` (None for a split job)."""
    done = [
        a
        for a in job.get("attempts", ())
        if a.get("role", "run") == "run" and a.get("state") == "done"
    ]
    return done[-1] if done else None


def _phase(
    decisions: Sequence[Mapping[str, Any]], attempt_id: str, phase: str
) -> int | None:
    for decision in decisions:
        actual = decision.get("actual") or {}
        if actual.get("attempt_id") == attempt_id:
            value = (actual.get("phases_ms") or {}).get(phase)
            return int(value) if isinstance(value, int | float) else None
    return None


def _transfer_bytes(job: Mapping[str, Any]) -> int:
    return sum(
        int(t.get("bytes") or 0)
        for a in job.get("attempts", ())
        for t in a.get("transfers", ())
        if t.get("source") != "cache"
    )


def measure_job(
    job: Mapping[str, Any], decisions: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """The per-job record (module docstring); every key always present."""
    created = _when(job.get("created_at"))
    finished = _finished_at(job)
    state = str(job.get("state"))
    dispatched = _dispatched(decisions)
    chosen = None if dispatched is None else _chosen(dispatched)
    mode = None if chosen is None else chosen.get("mode")
    nodes: list[str] = []
    if chosen is not None:
        node_set = chosen.get("node_set")
        nodes = [str(n) for n in node_set] if node_set else [str(chosen.get("node_id"))]
    run = _run_attempt(job)
    run_ms = (
        None if run is None else _phase(decisions, str(run.get("attempt_id")), "run")
    )
    e2e = _ms(created, finished) if state in TERMINAL else None
    if mode == "split":
        overhead, basis = None, "split_job"
    elif run_ms is None or e2e is None:
        overhead, basis = None, "no_run_phase"
    else:
        overhead, basis = e2e - run_ms, "end_to_end_minus_run"
    return {
        "job_id": str(job.get("job_id")),
        "state": state,
        "failure_type": job.get("failure_type"),
        "created_at": job.get("created_at"),
        "finished_at": None if finished is None else finished.isoformat(),
        "end_to_end_ms": e2e,
        "queue_ms": _ms(created, _first(job, "preparing")),
        "run_ms": run_ms,
        "overhead_ms": overhead,
        "overhead_basis": basis,
        "mode": mode,
        "nodes": nodes,
        "policy": None if dispatched is None else dispatched.get("policy"),
        "calibration_version": (
            None if dispatched is None else dispatched.get("calibration_version")
        ),
        "ml_model_version": (
            None if dispatched is None else dispatched.get("ml_model_version")
        ),
        "attempts": len(job.get("attempts", ())),
        "transfer_bytes": _transfer_bytes(job),
    }
