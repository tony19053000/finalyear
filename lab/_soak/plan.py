"""The run plan: one JSON file that defines a soak run (``soak.py plan``).

Every tool of the run reads it, so they agree on the clock and the windows:

* ``t0`` (UTC) and ``hours``: load and chaos happen in ``[t0, t0 + hours)``;
* ``quiet_h``: the first and the last ``quiet_h`` are **quiet windows** - load
  runs, chaos does not (D409). The memory and FD/handle rules compare the
  median of window 1 (after ``warmup_min``) with the median of window 2, so both
  medians are taken under the same load;
* ``tail_h``: after the load stops, the samplers keep going for ``tail_h``:
  jobs drain, then the fabric is idle - the DB rule's "no table grows without
  jobs" is checked there (D410 rule 5);
* chaos actions are scheduled in ``[t0 + quiet_h, t0 + hours - quiet_h)``.

**Compressed runs.** The 72 h lab run is ``hours=72, quiet_h=6``. A dry run on
one machine uses the same code with ``hours=1, quiet_h=5/60`` and smaller
chaos gaps / durations (``duration_scale``): the rules are evaluated on wall
time exactly the same way (MB/h is still per real hour), only the windows are
shorter, so a dry run can prove the tools and the fabric's recovery - not the
72 h leak bounds.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from _soak.stats import Window, parse_ts
from spikes._harness.atomic import atomic_write_text

PLAN_SCHEMA: Final = "meshforge.soak-plan/1"
PLAN_FILE: Final = "plan.json"
SCHEDULE_FILE: Final = "chaos-schedule.json"
LEDGER_FILE: Final = "ledger.jsonl"
COLLECT_FILE: Final = "jobs-final.json"
VERDICT_FILE: Final = "verdict.json"
NOTES_FILE: Final = "notes.json"


class PlanError(ValueError):
    """The plan file is missing, unreadable or inconsistent."""


@dataclass(frozen=True, slots=True)
class Target:
    """One laptop of the run (``targets.json`` row).

    ``executor``: how ``chaos.py`` acts on it - ``ssh`` (Linux laptop, from
    the controller laptop), ``local`` (this laptop), ``windows`` (delegated to
    ``chaos_win.ps1`` running on that laptop), ``harness`` (the dry run's
    cluster harness). ``roles``: ``controller`` and/or ``agent`` (PostgreSQL
    goes with the controller). ``node_ids`` / ``hostname``: how fabric nodes
    and job attempts are mapped to this laptop for attribution.
    """

    label: str
    os: str
    roles: tuple[str, ...]
    executor: str
    hostname: str = ""
    node_ids: tuple[str, ...] = ()
    ssh: str = ""
    iface: str = ""
    vars: dict[str, str] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.os not in ("linux", "windows"):
            raise PlanError(f"{self.label}: os must be linux or windows")
        if self.executor not in ("ssh", "local", "windows", "harness"):
            raise PlanError(f"{self.label}: unknown executor {self.executor!r}")
        if self.executor == "ssh" and not self.ssh:
            raise PlanError(f"{self.label}: executor ssh needs 'ssh': 'user@host'")
        if not set(self.roles) <= {"controller", "agent"} or not self.roles:
            raise PlanError(f"{self.label}: roles must be controller and/or agent")

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> Target:
        try:
            return cls(
                label=str(doc["label"]),
                os=str(doc["os"]),
                roles=tuple(str(r) for r in doc["roles"]),
                executor=str(doc["executor"]),
                hostname=str(doc.get("hostname") or ""),
                node_ids=tuple(str(n) for n in doc.get("node_ids") or ()),
                ssh=str(doc.get("ssh") or ""),
                iface=str(doc.get("iface") or ""),
                vars={str(k): str(v) for k, v in (doc.get("vars") or {}).items()},
                commands={
                    str(k): str(v) for k, v in (doc.get("commands") or {}).items()
                },
            )
        except (KeyError, TypeError) as exc:
            raise PlanError(f"bad target row {doc!r}: {exc}") from None

    def as_dict(self) -> dict[str, Any]:
        doc = asdict(self)
        doc["roles"] = list(self.roles)
        doc["node_ids"] = list(self.node_ids)
        return doc


@dataclass(frozen=True, slots=True)
class RunPlan:
    seed: int
    t0: datetime
    hours: float
    quiet_h: float
    tail_h: float
    warmup_min: float
    job_gap_s: float
    chaos_gap_min: tuple[float, float]
    duration_scale: float
    targets: tuple[Target, ...]
    nonce: str
    link: str = "wifi"

    def __post_init__(self) -> None:
        if self.hours <= 0 or self.quiet_h <= 0 or self.tail_h < 0:
            raise PlanError("hours and quiet_h must be > 0, tail_h >= 0")
        if 2 * self.quiet_h >= self.hours:
            raise PlanError("the two quiet windows must leave room for chaos")
        if self.warmup_min * 60 >= self.quiet_h * 3600:
            raise PlanError("warmup must be shorter than the quiet window")
        low, high = self.chaos_gap_min
        if not 0 < low <= high:
            raise PlanError("chaos gap must be 0 < low <= high minutes")
        if self.job_gap_s <= 0 or self.duration_scale <= 0:
            raise PlanError("job gap and duration scale must be positive")
        labels = [t.label for t in self.targets]
        if len(set(labels)) != len(labels):
            raise PlanError(f"target labels must be unique: {labels}")
        if sum("controller" in t.roles for t in self.targets) != 1:
            raise PlanError("exactly one target must have the controller role")

    # ------------------------------------------------------------ windows
    @property
    def end_load(self) -> datetime:
        return self.t0 + timedelta(hours=self.hours)

    @property
    def end(self) -> datetime:
        return self.t0 + timedelta(hours=self.hours + self.tail_h)

    def quiet1(self) -> Window:
        return Window("quiet1", self.warmup_min * 60.0, self.quiet_h * 3600.0)

    def quiet2(self) -> Window:
        return Window(
            "quiet2", (self.hours - self.quiet_h) * 3600.0, self.hours * 3600.0
        )

    def chaos_window(self) -> Window:
        return Window(
            "chaos", self.quiet_h * 3600.0, (self.hours - self.quiet_h) * 3600.0
        )

    def tail(self) -> Window:
        return Window("tail", self.hours * 3600.0, (self.hours + self.tail_h) * 3600.0)

    def offset_s(self, moment: datetime) -> float:
        return (moment - self.t0).total_seconds()

    def controller(self) -> Target:
        return next(t for t in self.targets if "controller" in t.roles)

    def target(self, label: str) -> Target:
        for t in self.targets:
            if t.label == label:
                return t
        raise PlanError(f"no target {label!r} in the plan")

    # ---------------------------------------------------------------- I/O
    def as_dict(self) -> dict[str, Any]:
        return {
            "schema": PLAN_SCHEMA,
            "seed": self.seed,
            "t0": self.t0.isoformat(),
            "hours": self.hours,
            "quiet_h": self.quiet_h,
            "tail_h": self.tail_h,
            "warmup_min": self.warmup_min,
            "job_gap_s": self.job_gap_s,
            "chaos_gap_min": list(self.chaos_gap_min),
            "duration_scale": self.duration_scale,
            "targets": [t.as_dict() for t in self.targets],
            "nonce": self.nonce,
            "link": self.link,
            "windows": [
                w.as_dict()
                for w in (
                    self.quiet1(),
                    self.chaos_window(),
                    self.quiet2(),
                    self.tail(),
                )
            ],
        }

    @classmethod
    def from_dict(cls, doc: dict[str, Any]) -> RunPlan:
        if doc.get("schema") != PLAN_SCHEMA:
            raise PlanError(f"not a {PLAN_SCHEMA} file")
        t0 = parse_ts(doc.get("t0"))
        if t0 is None:
            raise PlanError("plan has no valid t0")
        try:
            gap = doc["chaos_gap_min"]
            return cls(
                seed=int(doc["seed"]),
                t0=t0,
                hours=float(doc["hours"]),
                quiet_h=float(doc["quiet_h"]),
                tail_h=float(doc["tail_h"]),
                warmup_min=float(doc["warmup_min"]),
                job_gap_s=float(doc["job_gap_s"]),
                chaos_gap_min=(float(gap[0]), float(gap[1])),
                duration_scale=float(doc["duration_scale"]),
                targets=tuple(Target.from_dict(t) for t in doc["targets"]),
                nonce=str(doc["nonce"]),
                link=str(doc.get("link") or "wifi"),
            )
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise PlanError(f"bad plan: {exc}") from None


def new_nonce() -> str:
    """Makes this run's idempotency keys unique across runs of one seed."""
    return secrets.token_hex(4)


def load_targets(path: Path) -> tuple[Target, ...]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"{path}: cannot read the targets file ({exc})") from None
    rows = doc.get("targets") if isinstance(doc, dict) else doc
    if not isinstance(rows, list) or not rows:
        raise PlanError(f"{path}: expected a list of targets (or {{'targets': [...]}})")
    return tuple(Target.from_dict(r) for r in rows)


def write_json(path: Path, doc: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(doc, indent=1, sort_keys=True, allow_nan=False) + "\n"
    )


def read_json(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PlanError(f"{path}: cannot read ({exc})") from None
    if not isinstance(doc, dict):
        raise PlanError(f"{path}: not a JSON object")
    return doc


def load_plan(run_dir: Path) -> RunPlan:
    return RunPlan.from_dict(read_json(run_dir / PLAN_FILE))
