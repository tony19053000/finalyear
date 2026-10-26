"""Chaos on real laptops (D411): catalogue, seeded schedule, commands, action log.

**Catalogue** (:data:`CATALOGUE`) - D118's fault list carried over to installed
services: kill -9 an agent; kill -9 the controller (the service restarts it);
stop PostgreSQL under the controller; reboot a laptop; Wi-Fi/Ethernet down for
30 s and for 5 min; netem 200 ms / 5 % loss for 10 min (Linux only); fill the
agent's disk to 95 %; kill a running job's process tree; cancel an in-flight
Mode 3 job; revoke + re-join a node (once); reinstall the controller package
(once). Each kind says which laptops it applies to, which processes it is
*expected* to restart, whether it is fabric-wide (anything on the controller
laptop is), and how long after it ends its effects may still show (``grace_s``)
- the analyzer's attribution windows come from here, not from the log.

**Schedule** (:func:`build_schedule`): sequential, never two faults at once
(clean attribution), in the plan's chaos window; gaps drawn uniformly from
``chaos_gap_min`` (default 20-45 min: >= 1 action per 45 min, D411). Every kind
that applies to some laptop is placed once first (in seeded order), then kinds
are drawn by weight; ``once`` kinds never repeat.

**Commands.** Linux laptops are driven over SSH from the controller laptop
(``executor: ssh``) or locally (``local``) with the templates in
:data:`LINUX_COMMANDS` (overridable per target, ``{var}`` placeholders). Long
effects run detached under ``systemd-run`` so they survive the SSH session the
fault itself cuts. Windows laptops (``executor: windows``) run their own rows
with ``lab/chaos_win.ps1`` from the same schedule file; ``chaos.py`` logs
those rows as ``delegated``. ``harness`` hands each action to the dry run's
cluster harness through request files.

**Action log** (``chaos-log*.jsonl``): a ``start`` line before, an ``end`` line
after each action (outcome ``done`` / ``failed`` / ``skipped`` / ``missed`` /
``interrupted`` / ``delegated``), so a crash of the driver is visible and a
re-run resumes after the last finished action.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from _soak.plan import RunPlan, Target
from _soak.stats import parse_ts

SCHEDULE_SCHEMA: Final = "meshforge.chaos-schedule/1"
LOG_SCHEMA: Final = "meshforge.chaos-log/1"
FINAL_OUTCOMES: Final = frozenset(
    {"done", "failed", "skipped", "missed", "interrupted", "delegated"}
)
APPLIED_OUTCOMES: Final = frozenset({"done", "interrupted"})
"""Outcomes after which the fault may really have happened (attribution)."""


@dataclass(frozen=True, slots=True)
class Kind:
    name: str
    weight: float
    duration_s: float
    """How long the fault lasts (0 = instant); scaled by ``duration_scale``."""
    grace_s: float
    """After the fault ends, how long its effects may still show."""
    restarts: tuple[str, ...]
    """Processes (roles) on the target laptop this fault is expected to restart."""
    fails_jobs: bool
    """Job attempts on the target laptop may fail / be lost by it."""
    needs_agent: bool = False
    needs_controller: bool = False
    linux_only: bool = False
    once: bool = False
    max_count: int | None = None
    fabric_wide_on_controller: bool = True
    """On the controller laptop the fault hits every node (all go OFFLINE)."""
    params: tuple[tuple[str, str], ...] = ()


CATALOGUE: Final[dict[str, Kind]] = {
    k.name: k
    for k in (
        Kind("kill_agent", 3, 0, 300, ("agent",), True, needs_agent=True),
        Kind(
            "kill_controller", 1, 0, 300, ("controller", "postgres"), True,
            needs_controller=True,
        ),
        Kind(
            "restart_postgres", 1, 0, 300, ("postgres", "controller"), True,
            needs_controller=True,
        ),
        Kind(
            "reboot", 1, 240, 900, ("controller", "agent", "postgres"), True,
            max_count=3,
        ),
        Kind("net_down_short", 2, 30, 180, (), True),
        Kind("net_down_long", 1, 300, 300, (), True),
        Kind(
            "netem", 1, 600, 180, (), True, linux_only=True,
            params=(("delay_ms", "200"), ("loss_pct", "5")),
        ),
        Kind(
            "disk_fill", 1, 600, 180, (), True, needs_agent=True,
            params=(("fill_pct", "95"),),
        ),
        Kind("kill_job_tree", 2, 0, 120, (), True, needs_agent=True),
        Kind("cancel_mode3", 1, 0, 60, (), False, needs_controller=True,
             fabric_wide_on_controller=False),
        Kind("revoke_rejoin", 1, 600, 300, ("agent",), True, needs_agent=True, once=True),
        Kind(
            "upgrade_reinstall", 1, 120, 300, ("controller", "postgres"), True,
            needs_controller=True, once=True,
        ),
    )
}  # fmt: skip

MIN_DURATION_S: Final = 10.0

# Templates for Linux laptops with the .deb services (D386/D390). {id},
# {duration_s}, {iface}, {delay_ms}, {loss_pct}, {fill_pct} and every key of
# the target's "vars" are substituted. Each must exit within COMMAND_TIMEOUT_S;
# long effects detach with systemd-run (they outlive the SSH session they cut).
_RUN: Final = "sudo -n systemd-run --collect --quiet --unit=mf-chaos-{id} sh -c"
LINUX_COMMANDS: Final[dict[str, str]] = {
    "kill_agent": "sudo -n systemctl kill --signal=SIGKILL --kill-whom=main meshforge-agent",
    "kill_controller": (
        "sudo -n systemctl kill --signal=SIGKILL --kill-whom=main meshforge-controller"
    ),
    # The managed PostgreSQL (D390): stop it under the running controller. The
    # controller then stops with FAILURE and systemd restarts both (D398).
    "restart_postgres": (
        "sudo -n -u meshforge {pg_bin}/pg_ctl -D {pg_data} stop -m fast -w -t 60"
    ),
    "reboot": "sudo -n systemd-run --on-active=2 --collect --quiet systemctl reboot",
    "net_down_short": (
        _RUN + " 'nmcli networking off; sleep {duration_s}; nmcli networking on'"
    ),
    "net_down_long": (
        _RUN + " 'nmcli networking off; sleep {duration_s}; nmcli networking on'"
    ),
    "netem": (
        _RUN + " 'tc qdisc replace dev {iface} root netem delay {delay_ms}ms loss "
        "{loss_pct}%; sleep {duration_s}; tc qdisc del dev {iface} root'"
    ),
    "disk_fill": (
        _RUN
        + " 'f={fill_file}; d=$(dirname $f); s=$(df --output=size -B1 $d | tail -1);"
        " a=$(df --output=avail -B1 $d | tail -1); n=$((a - s * (100 - {fill_pct}) / 100));"
        " [ $n -gt 0 ] && fallocate -l $n $f; sleep {duration_s}; rm -f $f'"
    ),
    # Every process of the agent service's cgroup except the agent's own leaf
    # (DelegateSubgroup=agent): the running jobs' trees.
    "kill_job_tree": (
        "sudo -n sh -c 'n=0; for f in $(find {agent_cgroup} -mindepth 2 -name "
        'cgroup.procs ! -path "*/agent/*"); do for p in $(cat $f); do kill -9 $p '
        "&& n=$((n+1)); done; done; echo killed=$n'"
    ),
    "upgrade_reinstall": ("sudo -n apt-get install --reinstall -y {controller_deb}"),
}
LINUX_VARS: Final[dict[str, str]] = {
    "pg_bin": "/opt/meshforge/controller/current/pgsql/bin",
    "pg_data": "/var/lib/meshforge/db/data",
    "fill_file": "/var/lib/meshforge/mf-chaos-fill.bin",
    "agent_cgroup": "/sys/fs/cgroup/system.slice/meshforge-agent.service",
    "controller_deb": "",
}
API_KINDS: Final = frozenset({"cancel_mode3"})
"""Done by chaos.py through the controller's REST API, whatever the executor."""
MANUAL_BY_DEFAULT: Final = frozenset({"revoke_rejoin"})
"""Need a person (a join code on the laptop) unless the target gives a command."""


@dataclass(frozen=True, slots=True)
class Action:
    id: str
    at: str
    offset_s: float
    kind: str
    target: str
    executor: str
    duration_s: float
    params: dict[str, str]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> Action:
        return cls(
            id=str(doc["id"]),
            at=str(doc["at"]),
            offset_s=float(doc["offset_s"]),
            kind=str(doc["kind"]),
            target=str(doc["target"]),
            executor=str(doc["executor"]),
            duration_s=float(doc["duration_s"]),
            params={str(k): str(v) for k, v in (doc.get("params") or {}).items()},
        )


def applies(kind: Kind, target: Target) -> bool:
    if kind.needs_agent and "agent" not in target.roles:
        return False
    if kind.needs_controller and "controller" not in target.roles:
        return False
    return not (kind.linux_only and target.os != "linux")


def scaled_duration(kind: Kind, scale: float) -> float:
    if kind.duration_s <= 0:
        return 0.0
    return round(max(MIN_DURATION_S, kind.duration_s * scale), 1)


def build_schedule(
    plan: RunPlan, *, kinds: Iterable[str] | None = None
) -> list[Action]:
    """The run's chaos actions (module docstring), deterministic in the plan."""
    rng = random.Random(f"soak-chaos-{plan.seed}")  # noqa: S311 - a reproducible schedule
    allowed = [CATALOGUE[k] for k in (kinds or CATALOGUE)]
    usable = [k for k in allowed if any(applies(k, t) for t in plan.targets)]
    must = [k.name for k in usable]
    rng.shuffle(must)
    counts: dict[str, int] = {}
    window = plan.chaos_window()
    low, high = plan.chaos_gap_min
    t = window.start_s + rng.uniform(low, high) * 60.0
    out: list[Action] = []
    while t < window.end_s:
        if must:
            name = must.pop(0)
        else:
            pool = [
                k
                for k in usable
                if not (k.once and counts.get(k.name))
                and not (
                    k.max_count is not None and counts.get(k.name, 0) >= k.max_count
                )
            ]
            if not pool:
                break
            name = rng.choices(
                [k.name for k in pool], weights=[k.weight for k in pool]
            )[0]
        kind = CATALOGUE[name]
        target = rng.choice([x for x in plan.targets if applies(kind, x)])
        duration = scaled_duration(kind, plan.duration_scale)
        if t + duration > window.end_s:
            break
        counts[name] = counts.get(name, 0) + 1
        executor = target.executor
        if name in API_KINDS:
            executor = "api"
        elif (
            name in MANUAL_BY_DEFAULT
            and executor != "harness"
            and name not in target.commands
        ):
            executor = "manual"
        out.append(
            Action(
                id=f"A{len(out) + 1:03d}",
                at=(plan.t0 + timedelta(seconds=t)).isoformat(),
                offset_s=round(t, 1),
                kind=name,
                target=target.label,
                executor=executor,
                duration_s=duration,
                params=dict(kind.params),
            )
        )
        t += duration + rng.uniform(low, high) * 60.0
    return out


def schedule_doc(plan: RunPlan, actions: list[Action]) -> dict[str, Any]:
    return {
        "schema": SCHEDULE_SCHEMA,
        "seed": plan.seed,
        "nonce": plan.nonce,
        "t0": plan.t0.isoformat(),
        "actions": [a.as_dict() for a in actions],
        "targets": [t.as_dict() for t in plan.targets],
    }


class TemplateError(ValueError):
    """A command template names a variable nobody defined."""


def render_command(action: Action, target: Target) -> str:
    """The shell command for a Linux ``ssh`` / ``local`` action."""
    template = target.commands.get(action.kind) or LINUX_COMMANDS.get(action.kind)
    if template is None:
        raise TemplateError(f"no command for {action.kind} on {target.label}")
    values: dict[str, str] = {
        **LINUX_VARS,
        **target.vars,
        **action.params,
        "id": action.id.lower(),
        "duration_s": f"{action.duration_s:g}",
        "iface": target.iface,
    }
    try:
        command = template.format(**values)
    except (KeyError, IndexError) as exc:
        raise TemplateError(f"{action.kind}: unknown placeholder {exc}") from None
    for needed in ("iface", "controller_deb"):
        if "{" + needed + "}" in template and not values.get(needed):
            raise TemplateError(
                f"{action.kind} on {target.label} needs '{needed}' in targets.json"
            )
    return command


def ssh_argv(target: Target, command: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=5",
        target.ssh,
        command,
    ]


# ------------------------------------------------------------- action log
@dataclass(frozen=True, slots=True)
class LoggedAction:
    """An action as the analyzer sees it (one ``end`` line, plus its start)."""

    id: str
    kind: str
    target: str
    started_at: datetime
    effect_until: datetime
    outcome: str
    job_id: str | None = None
    detail: str = ""

    @property
    def applied(self) -> bool:
        return self.outcome in APPLIED_OUTCOMES

    def window(self) -> tuple[datetime, datetime]:
        """When this action may explain an event: from a little before it
        started (clock skew between laptops) to ``grace_s`` after it ended."""
        kind = CATALOGUE.get(self.kind)
        grace = kind.grace_s if kind else 300.0
        return (
            self.started_at - timedelta(seconds=SKEW_S),
            self.effect_until + timedelta(seconds=grace),
        )


SKEW_S: Final = 30.0
"""Clock-skew allowance between laptops (the checklist checks NTP < 2 s)."""


def finished_ids(records: Iterable[Mapping[str, Any]]) -> set[str]:
    return {
        str(r["id"])
        for r in records
        if r.get("type") == "end" and r.get("outcome") in FINAL_OUTCOMES
    }


def started_ids(records: Iterable[Mapping[str, Any]]) -> set[str]:
    return {str(r["id"]) for r in records if r.get("type") == "start"}


def logged_actions(records: Iterable[Mapping[str, Any]]) -> list[LoggedAction]:
    """Every action with an ``end`` line (the last one wins: a ``delegated``
    line from chaos.py is replaced by the Windows log's real outcome)."""
    by_id: dict[str, LoggedAction] = {}
    for r in records:
        if r.get("type") != "end":
            continue
        start = parse_ts(r.get("started_at"))
        until = parse_ts(r.get("effect_until")) or start
        if start is None or until is None:
            continue
        action = LoggedAction(
            id=str(r.get("id")),
            kind=str(r.get("kind")),
            target=str(r.get("target")),
            started_at=start,
            effect_until=until,
            outcome=str(r.get("outcome")),
            job_id=r.get("job_id") if isinstance(r.get("job_id"), str) else None,
            detail=str(r.get("detail") or ""),
        )
        old = by_id.get(action.id)
        if old is None or old.outcome == "delegated":
            by_id[action.id] = action
    return sorted(by_id.values(), key=lambda a: (a.started_at, a.id))


def end_record(
    action: Action,
    *,
    started: datetime,
    ended: datetime,
    outcome: str,
    executor: str,
    exit_code: int | None = None,
    detail: str = "",
    job_id: str | None = None,
) -> dict[str, Any]:
    effect_until = max(ended, started + timedelta(seconds=action.duration_s))
    return {
        "type": "end",
        "schema": LOG_SCHEMA,
        "id": action.id,
        "kind": action.kind,
        "target": action.target,
        "scheduled_at": action.at,
        "started_at": started.isoformat(),
        "ended_at": ended.isoformat(),
        "effect_until": effect_until.isoformat(),
        "outcome": outcome,
        "executor": executor,
        "exit_code": exit_code,
        "detail": detail[:500],
        "job_id": job_id,
    }


Runner = Callable[[list[str], float], tuple[int, str]]
"""``(argv, timeout_s) -> (exit code, combined output)``."""
