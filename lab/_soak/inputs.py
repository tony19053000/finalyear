"""Everything the D410 analyzer reads, loaded from one run folder.

Layout of ``lab/reports/raw/<day>-V1.0-soak/`` (all written by the kit):

==============================  ==============================================
``plan.json``                   ``soak.py plan``: seed, t0, windows, targets
``chaos-schedule.json``         ``soak.py plan``: the seeded chaos actions
``samples.<label>.jsonl``       ``soak.py run`` on each laptop (copied here)
``ledger.jsonl``                ``soak_load.py run``: the submit log
``jobs-final.json``             ``soak_load.py collect``: the controller's view
``chaos-log*.jsonl``            ``chaos.py run`` + ``chaos_win.ps1`` per laptop
``fabric-soak.checkpoint.json``  ``fabric_check.py soak --checkpoint ...``
``notes.json`` (optional)       outside events a person attributes
==============================  ==============================================

``notes.json``: ``{"events": [{"from": ISO, "to": ISO, "host": "L3" | "*",
"reason": "power cut"}]}`` - a laptop that slept, a Windows update, a router
reboot: what the team saw that our tools could not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from _fabric import checkpoint
from _fabric.soak import KIND as FABRIC_SOAK_KIND
from _soak import jsonl
from _soak.chaos import LoggedAction, logged_actions
from _soak.ledger import LedgerState, reduce
from _soak.load import JobSpec, job_specs
from _soak.plan import (
    COLLECT_FILE,
    LEDGER_FILE,
    NOTES_FILE,
    RunPlan,
    load_plan,
    read_json,
)
from _soak.stats import parse_ts

FABRIC_SOAK_FILE = "fabric-soak.checkpoint.json"


@dataclass(frozen=True, slots=True)
class Note:
    start: datetime
    end: datetime
    host: str
    reason: str

    def covers(self, moment: datetime, host: str | None) -> bool:
        return self.start <= moment <= self.end and (
            self.host == "*" or host is None or self.host == host
        )


@dataclass(slots=True)
class SoakInputs:
    plan: RunPlan
    specs: list[JobSpec]
    ledger: LedgerState
    samples: dict[str, list[dict[str, Any]]]
    """Sampler label -> its records (header, ticks, events)."""
    actions: list[LoggedAction]
    final: dict[str, Any] | None
    """``jobs-final.json`` or ``None`` when ``collect`` was not run."""
    fabric: dict[str, Any] | None
    """The ``fabric_check soak`` state, or ``None`` when it is missing."""
    notes: list[Note] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def load_inputs(run_dir: Path) -> SoakInputs:
    plan = load_plan(run_dir)
    problems: list[str] = []
    samples: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(run_dir.glob("samples.*.jsonl")):
        loaded = jsonl.load(path)
        header = next((r for r in loaded.records if r.get("type") == "header"), None)
        label = str(header.get("label")) if header else path.name
        samples.setdefault(label, []).extend(loaded.records)
        if loaded.torn_tail:
            problems.append(
                f"{path.name}: a torn line was skipped (crash while writing)"
            )
    ledger_loaded = jsonl.load(run_dir / LEDGER_FILE)
    chaos_records: list[dict[str, Any]] = []
    for path in sorted(run_dir.glob("chaos-log*.jsonl")):
        chaos_records.extend(jsonl.load(path).records)
    final = (
        read_json(run_dir / COLLECT_FILE) if (run_dir / COLLECT_FILE).exists() else None
    )
    fabric: dict[str, Any] | None = None
    fabric_path = run_dir / FABRIC_SOAK_FILE
    if fabric_path.exists():
        try:
            fabric = checkpoint.load(fabric_path, FABRIC_SOAK_KIND)
        except checkpoint.CheckpointError as exc:
            problems.append(str(exc))
    notes: list[Note] = []
    if (run_dir / NOTES_FILE).exists():
        for row in read_json(run_dir / NOTES_FILE).get("events") or []:
            start, end = parse_ts(row.get("from")), parse_ts(row.get("to"))
            if start is None or end is None:
                problems.append(f"{NOTES_FILE}: bad event {row!r}")
                continue
            notes.append(
                Note(
                    start,
                    end,
                    str(row.get("host") or "*"),
                    str(row.get("reason") or ""),
                )
            )
    return SoakInputs(
        plan=plan,
        specs=job_specs(plan),
        ledger=reduce(ledger_loaded.records),
        samples=samples,
        actions=logged_actions(chaos_records),
        final=final,
        fabric=fabric,
        notes=notes,
        problems=problems,
    )
