"""Synthetic soak runs for the V1.0 G1 unit tests (no fabric, no clock).

:func:`world` builds a :class:`~_soak.inputs.SoakInputs` for a 1 h plan
(quiet windows 6 min each, 12 min idle tail) on three laptops - L1 controller
(Linux), L2 agent (Linux), L3 agent (Windows) - with a flat, healthy sampler
series per process. Tests then bend one thing (a leak, a lost job, an
unexplained restart) and check the rule that must notice.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from _soak.chaos import LoggedAction
from _soak.inputs import Note, SoakInputs
from _soak.ledger import reduce
from _soak.load import JobSpec
from _soak.plan import RunPlan, Target

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
MIB = 1024 * 1024


def plan(**overrides: Any) -> RunPlan:
    values: dict[str, Any] = {
        "seed": 7,
        "t0": T0,
        "hours": 1.0,
        "quiet_h": 0.1,
        "tail_h": 0.2,
        "warmup_min": 1.0,
        "job_gap_s": 30.0,
        "chaos_gap_min": (5.0, 8.0),
        "duration_scale": 0.2,
        "targets": (
            Target(
                "L1", "linux", ("controller",), "local", hostname="ctl", node_ids=()
            ),
            Target(
                "L2",
                "linux",
                ("agent",),
                "ssh",
                hostname="lin",
                ssh="lab@l2",
                node_ids=("node_L2",),
            ),
            Target(
                "L3",
                "windows",
                ("agent",),
                "windows",
                hostname="WIN",
                node_ids=("node_L3",),
            ),
        ),
        "nonce": "abcd1234",
    }
    values.update(overrides)
    return RunPlan(**values)


def at(offset_s: float) -> str:
    return (T0 + timedelta(seconds=offset_s)).isoformat()


Shape = Callable[[float], float]
"""offset seconds -> value."""


def sampler(
    label: str,
    procs: dict[str, tuple[str, str]],
    *,
    rss: dict[str, Shape] | None = None,
    fds: dict[str, Shape] | None = None,
    threads: dict[str, Shape] | None = None,
    pids: dict[str, Shape] | None = None,
    os_name: str = "posix",
    every_s: float = 30.0,
    end_s: float = 4320.0,
    dirs: dict[str, tuple[Shape, int | None]] | None = None,
    db: Callable[[float], dict[str, Any] | None] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """A samples file: ``procs`` = name -> (role, host); flat 100 MiB RSS,
    40 FDs, 12 threads unless a shape says otherwise; ``pids``: the process
    identity over time (a new value = a restart; default one process)."""
    pids = pids or {}
    rss = rss or {}
    fds = fds or {}
    threads = threads or {}
    records: list[dict[str, Any]] = [
        {
            "type": "header",
            "label": label,
            "interval_s": every_s,
            "os": os_name,
            "procs": [
                {"name": n, "role": r, "host": h, "match": None}
                for n, (r, h) in procs.items()
            ],
            "dirs": [],
        }
    ]
    t = 0.0
    while t <= end_s:
        tick: dict[str, Any] = {
            "type": "tick",
            "at": at(t),
            "label": label,
            "procs": {},
        }
        for name in procs:
            counter = fds.get(name, lambda _t: 40.0)(t)
            pid = int(pids.get(name, lambda _t: 100.0)(t))
            tick["procs"][name] = {
                "pid": pid,
                "create_time": float(pid),
                "rss_bytes": int(rss.get(name, lambda _t: 100.0 * MIB)(t)),
                "fds": None if os_name == "nt" else int(counter),
                "handles": int(counter) if os_name == "nt" else None,
                "threads": int(threads.get(name, lambda _t: 12.0)(t)),
                "cpu_s": t / 100.0,
                "procs": 1,
            }
        if dirs:
            tick["dirs"] = {
                n: {"bytes": int(shape(t)), "cap_bytes": cap}
                for n, (shape, cap) in dirs.items()
            }
        if db is not None:
            point = db(t)
            if point is not None:
                tick["db"] = point
        records.append(tick)
        t += every_s
    records.extend(events or [])
    return records


def spec(
    seq: int, *, exit_code: int = 0, cancel: float | None = None, kind: str = "m1_short"
) -> JobSpec:
    return JobSpec(
        seq=seq,
        offset_s=60.0 * seq,
        kind=kind,
        policy="cost_model",
        priority=0,
        exit_code=exit_code,
        sleep_s=10.0,
        work_s=0.0,
        split_params=None,
        seed=1,
        cancel_after_s=cancel,
        timeout_s=600,
    )


def view(
    job_id: str,
    state: str = "succeeded",
    *,
    node: str = "node_L2",
    submitted_s: float = 0.0,
    end_s: float = 60.0,
    failure: str | None = None,
    attempts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    transitions = [
        {"seq": 1, "from_state": None, "to_state": "submitted", "at": at(submitted_s)}
    ]
    if state in ("succeeded", "failed", "cancelled"):
        transitions.append(
            {"seq": 2, "from_state": "running", "to_state": state, "at": at(end_s)}
        )
    if attempts is None:
        attempt_state = {
            "succeeded": "done",
            "failed": "failed",
            "cancelled": "failed",
        }.get(state, "running")
        attempts = [
            {
                "attempt_id": f"{job_id}-a1",
                "node_id": node,
                "state": attempt_state,
                "role": "run",
                "ended_at": at(end_s),
            }
        ]
    return {
        "job_id": job_id,
        "state": state,
        "failure_type": failure,
        "manifest": {"name": job_id},
        "transitions": transitions,
        "attempts": attempts,
        "output_digest": "sha256:" + "a" * 64 if state == "succeeded" else None,
    }


def ledger_records(jobs: list[tuple[int, str, float]]) -> list[dict[str, Any]]:
    """(seq, job_id, submitted offset s) -> intent + submitted lines."""
    out: list[dict[str, Any]] = [{"type": "header", "seed": 7}]
    for seq, job_id, t in jobs:
        out.append(
            {
                "type": "intent",
                "seq": seq,
                "kind": "m1_short",
                "key": f"k{seq}",
                "at": at(t),
            }
        )
        out.append({"type": "submitted", "seq": seq, "job_id": job_id, "at": at(t)})
    return out


def action(
    aid: str,
    kind: str,
    target: str,
    start_s: float,
    until_s: float | None = None,
    *,
    outcome: str = "done",
    job_id: str | None = None,
) -> LoggedAction:
    return LoggedAction(
        id=aid,
        kind=kind,
        target=target,
        started_at=T0 + timedelta(seconds=start_s),
        effect_until=T0
        + timedelta(seconds=until_s if until_s is not None else start_s),
        outcome=outcome,
        job_id=job_id,
    )


def healthy_samples() -> dict[str, list[dict[str, Any]]]:
    return {
        "L1": sampler(
            "L1", {"controller": ("controller", "L1"), "postgres": ("postgres", "L1")}
        ),
        "L2": sampler("L2", {"agent": ("agent", "L2")}),
        "L3": sampler("L3", {"agent": ("agent", "L3")}, os_name="nt"),
    }


def world(
    *,
    samples: dict[str, list[dict[str, Any]]] | None = None,
    jobs: list[tuple[JobSpec, dict[str, Any] | None]] | None = None,
    actions: list[LoggedAction] | None = None,
    fabric: dict[str, Any] | None = None,
    notes: list[Note] | None = None,
    collected: bool = True,
    the_plan: RunPlan | None = None,
) -> SoakInputs:
    jobs = (
        jobs
        if jobs is not None
        else [
            (spec(i), view(f"job{i}", submitted_s=60.0 * i, end_s=60.0 * i + 30))
            for i in range(5)
        ]
    )
    records = ledger_records(
        [(s.seq, (v or {}).get("job_id") or f"job{s.seq}", s.offset_s) for s, v in jobs]
    )
    final = (
        {
            "jobs": {(v or {}).get("job_id") or f"job{s.seq}": v for s, v in jobs},
            "nodes": [
                {"node_id": "node_L2", "hostname": "lin"},
                {"node_id": "node_L3", "hostname": "WIN"},
            ],
            "outputs": {},
        }
        if collected
        else None
    )
    return SoakInputs(
        plan=the_plan or plan(),
        specs=[s for s, _ in jobs],
        ledger=reduce(records),
        samples=samples if samples is not None else healthy_samples(),
        actions=actions or [],
        final=final,
        fabric=fabric
        if fabric is not None
        else {
            "nodes": {},
            "transitions": [],
            "outages": [],
            "elapsed_s": 4320.0,
        },
        notes=notes or [],
    )
