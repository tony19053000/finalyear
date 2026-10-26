"""V1.0 G1 (roadmap 1.0.2, D409/D411): the run plan, the job mix, the chaos
schedule and the chaos driver (fake commands, fake time)."""

from __future__ import annotations

import itertools
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import soak_world as w

import chaos as chaos_cli
from _soak import chaos, jsonl
from _soak.ledger import reduce
from _soak.load import CANCEL_PCT, MIX, job_specs, mix_summary
from _soak.plan import PlanError, RunPlan, Target
from _soak.stats import parse_ts

pytestmark = pytest.mark.unit


def _lab_plan(**kw: Any) -> RunPlan:
    """The 72 h lab plan on four laptops."""
    targets = (
        Target("L1", "linux", ("controller", "agent"), "local", iface="wlp2s0"),
        Target("L2", "linux", ("agent",), "ssh", ssh="lab@l2", iface="wlp3s0"),
        Target("L3", "windows", ("agent",), "windows", iface="Wi-Fi"),
        Target("L4", "windows", ("agent",), "windows", iface="Wi-Fi"),
    )
    values: dict[str, Any] = {"hours": 72.0, "quiet_h": 6.0, "tail_h": 0.5, "warmup_min": 30.0,
                              "chaos_gap_min": (20.0, 45.0), "duration_scale": 1.0,
                              "targets": targets}  # fmt: skip
    values.update(kw)
    return w.plan(**values)


# ------------------------------------------------------------------ plan
def test_windows_and_round_trip() -> None:
    plan = _lab_plan()
    assert plan.quiet1().start_s == 1800.0
    assert plan.quiet1().end_s == 6 * 3600.0
    assert plan.quiet2().start_s == 66 * 3600.0
    assert plan.chaos_window().end_s == 66 * 3600.0
    assert plan.end == plan.t0 + timedelta(hours=72.5)
    assert RunPlan.from_dict(json.loads(json.dumps(plan.as_dict()))) == plan


@pytest.mark.parametrize(
    ("kw", "message"),
    [
        ({"quiet_h": 40.0}, "room for chaos"),
        ({"warmup_min": 600.0}, "warmup"),
        ({"chaos_gap_min": (10.0, 5.0)}, "chaos gap"),
    ],
)
def test_bad_plans_are_refused(kw: dict[str, Any], message: str) -> None:
    with pytest.raises(PlanError, match=message):
        _lab_plan(**kw)


def test_exactly_one_controller_and_ssh_needs_a_host() -> None:
    with pytest.raises(PlanError, match="exactly one"):
        w.plan(targets=(Target("L2", "linux", ("agent",), "local"),))
    with pytest.raises(PlanError, match="ssh"):
        Target("L2", "linux", ("agent",), "ssh")


# ------------------------------------------------------------------ load
def test_job_mix_is_seeded_poisson_and_matches_d409() -> None:
    plan = _lab_plan()
    specs = job_specs(plan)
    assert specs == job_specs(plan)  # deterministic: a resumed generator rebuilds it
    assert job_specs(_lab_plan(seed=8)) != specs
    # ~1 job / 30 s over 72 h = 8640, Poisson
    assert 8300 < len(specs) < 9000
    gaps = [b.offset_s - a.offset_s for a, b in itertools.pairwise(specs)]
    assert sum(gaps) / len(gaps) == pytest.approx(30.0, rel=0.05)
    summary = mix_summary(specs)
    for kind, weight in MIX:
        assert summary["by_kind"][kind] / len(specs) == pytest.approx(weight, abs=0.02)
    assert summary["planned_cancels"] / len(specs) == pytest.approx(
        CANCEL_PCT, abs=0.01
    )
    assert all(s.offset_s < 72 * 3600 for s in specs)
    assert all(s.split_params for s in specs if s.kind == "m3_split")


def test_ledger_reduce_finds_resume_point_and_duplicates() -> None:
    records = [
        {"type": "header", "seed": 7},
        {"type": "intent", "seq": 0, "kind": "m1_short", "key": "k0", "at": w.at(0)},
        {"type": "submitted", "seq": 0, "job_id": "j0", "at": w.at(1)},
        {"type": "intent", "seq": 1, "kind": "bench", "key": "k1", "at": w.at(30)},
        {"type": "submit_failed", "seq": 2, "final": True, "error": "skipped_late"},
        {"type": "submitted", "seq": 0, "job_id": "jX", "at": w.at(2)},
    ]
    state = reduce(records)
    assert state.unresolved() == [1]
    assert state.entries[2].final_failure == "skipped_late"
    assert state.job_ids() == {"j0": 0}
    assert state.problems and "duplicate" in state.problems[0]


# -------------------------------------------------------------- schedule
def test_lab_schedule_covers_every_kind_at_least_every_45_min() -> None:
    plan = _lab_plan()
    actions = chaos.build_schedule(plan)
    assert actions == chaos.build_schedule(plan)
    kinds = {a.kind for a in actions}
    assert kinds == set(chaos.CATALOGUE)
    window = plan.chaos_window()
    starts = [window.start_s] + [a.offset_s for a in actions]
    ends = [a.offset_s + a.duration_s for a in actions]
    # sequential: never two faults at once
    assert all(
        e <= n for e, n in zip(ends, [a.offset_s for a in actions[1:]], strict=False)
    )
    # D411: >= 1 action per 45 min (from each fault's end to the next start)
    gaps = [b - a for a, b in zip([window.start_s, *ends], starts[1:], strict=False)]
    assert max(gaps) <= 45 * 60
    assert all(window.start_s <= a.offset_s and a.offset_s + a.duration_s <= window.end_s
               for a in actions)  # fmt: skip
    for once in ("revoke_rejoin", "upgrade_reinstall"):
        assert sum(a.kind == once for a in actions) == 1
    assert sum(a.kind == "reboot" for a in actions) <= 3
    by_target = {t.label: t for t in plan.targets}
    for a in actions:
        target = by_target[a.target]
        assert chaos.applies(chaos.CATALOGUE[a.kind], target), a
        if a.kind == "netem":
            assert target.os == "linux"
        if a.kind == "cancel_mode3":
            assert a.executor == "api"
        if a.kind == "revoke_rejoin":
            assert a.executor in ("manual", "windows")
        if a.executor == "windows":
            assert target.os == "windows"


def test_compressed_schedule_scales_durations() -> None:
    plan = w.plan()
    actions = chaos.build_schedule(plan)
    assert len(actions) >= 6
    long = [a for a in actions if a.kind == "net_down_long"]
    assert all(a.duration_s == 60.0 for a in long)
    assert all(
        a.duration_s == 0 or a.duration_s >= chaos.MIN_DURATION_S for a in actions
    )


def test_command_templates_render_and_need_their_variables() -> None:
    plan = _lab_plan()
    l2 = plan.target("L2")
    action = chaos.Action("A007", w.at(0), 0, "netem", "L2", "ssh", 600.0, {"delay_ms": "200",
                          "loss_pct": "5"})  # fmt: skip
    command = chaos.render_command(action, l2)
    assert "tc qdisc replace dev wlp3s0 root netem delay 200ms loss 5%" in command
    assert "sleep 600" in command
    assert "--unit=mf-chaos-a007" in command
    argv = chaos.ssh_argv(l2, command)
    assert argv[0] == "ssh"
    assert "BatchMode=yes" in argv
    assert argv[-2:] == ["lab@l2", command]
    upgrade = chaos.Action(
        "A9", w.at(0), 0, "upgrade_reinstall", "L1", "local", 120.0, {}
    )
    with pytest.raises(chaos.TemplateError, match="controller_deb"):
        chaos.render_command(upgrade, plan.target("L1"))
    custom = Target(
        "L1", "linux", ("controller",), "local", vars={"controller_deb": "/x.deb"}
    )
    assert chaos.render_command(upgrade, custom).endswith("/x.deb")


# ---------------------------------------------------------------- driver
class FakeTime:
    def __init__(self, start: float = -5.0) -> None:
        self.t = start

    def now(self) -> Any:
        return w.T0 + timedelta(seconds=self.t)

    def sleep(self, s: float) -> None:
        self.t += s


def _driver(
    tmp_path: Path, actions: list[chaos.Action], **kw: Any
) -> tuple[Any, list[list[str]], FakeTime]:
    calls: list[list[str]] = []
    clock = kw.pop("clock", FakeTime())

    def runner(argv: list[str], timeout_s: float) -> tuple[int, str]:
        calls.append(argv)
        return kw.get("code", 0), "ok"

    driver = chaos_cli.Driver(
        _lab_plan(), actions, tmp_path / "chaos-log.jsonl", manual="skip", late_s=600.0,
        control_dir=None, runner=runner, now=clock.now, sleep=clock.sleep,
        canceller=lambda a: ("done", 202, "cancelled j9", "j9"),
    )  # fmt: skip
    return driver, calls, clock


def _act(
    aid: str, kind: str, target: str, t: float, executor: str, duration: float = 0.0
) -> chaos.Action:
    return chaos.Action(aid, w.at(t), t, kind, target, executor, duration, {})


def test_driver_runs_logs_and_delegates(tmp_path: Path) -> None:
    actions = [
        _act("A1", "kill_agent", "L2", 10, "ssh"),
        _act("A2", "kill_agent", "L3", 20, "windows"),
        _act("A3", "cancel_mode3", "L1", 30, "api"),
        _act("A4", "revoke_rejoin", "L2", 40, "manual", 600),
        _act("A5", "kill_job_tree", "L1", 50, "local"),
    ]
    driver, calls, clock = _driver(tmp_path, actions)
    assert driver.run() == chaos_cli.EXIT_OK
    assert clock.t >= 50
    assert calls[0][0] == "ssh"
    assert calls[1][:2] == ["sh", "-c"]
    ends = [
        r
        for r in jsonl.load(tmp_path / "chaos-log.jsonl").records
        if r["type"] == "end"
    ]
    assert [(r["id"], r["outcome"]) for r in ends] == [
        ("A1", "done"), ("A2", "delegated"), ("A3", "done"), ("A4", "skipped"), ("A5", "done"),
    ]  # fmt: skip
    assert ends[2]["job_id"] == "j9"
    # the end line carries the effect window the analyzer attributes with
    assert parse_ts(ends[3]["effect_until"]) == parse_ts(
        ends[3]["started_at"]
    ) + timedelta(seconds=600)


def test_driver_resumes_marks_interrupted_and_missed(tmp_path: Path) -> None:
    actions = [
        _act("A1", "kill_agent", "L2", 10, "ssh"),
        _act("A2", "kill_agent", "L2", 20, "ssh"),
        _act("A3", "kill_agent", "L2", 30, "ssh"),
        _act("A4", "kill_agent", "L2", 4000, "ssh"),
    ]
    log = tmp_path / "chaos-log.jsonl"
    finished = chaos.end_record(actions[0], started=w.T0, ended=w.T0, outcome="done",
                                executor="ssh")  # fmt: skip
    jsonl.append(log, {"type": "start", "id": "A1"})
    jsonl.append(log, finished)
    jsonl.append(log, {"type": "start", "id": "A2"})  # the driver died here
    driver, calls, _ = _driver(tmp_path, actions, clock=FakeTime(start=3000.0))
    assert driver.run() == chaos_cli.EXIT_OK
    ends = {
        r["id"]: r["outcome"] for r in jsonl.load(log).records if r["type"] == "end"
    }
    assert ends == {"A1": "done", "A2": "interrupted", "A3": "missed", "A4": "done"}
    assert len(calls) == 1  # only A4 ran


def test_failed_command_fails_the_run_but_reboot_ssh_drop_does_not(
    tmp_path: Path,
) -> None:
    driver, _, _ = _driver(
        tmp_path, [_act("A1", "reboot", "L2", 1, "ssh", 240)], code=255
    )
    assert driver.run() == chaos_cli.EXIT_OK
    driver, _, _ = _driver(
        tmp_path / "x", [_act("A1", "kill_agent", "L2", 1, "ssh")], code=1
    )
    (tmp_path / "x").mkdir()
    assert driver.run() == chaos_cli.EXIT_ACTION_FAILED


def test_logged_actions_prefer_the_windows_outcome_over_delegated() -> None:
    a = _act("A2", "kill_agent", "L3", 20, "windows")
    delegated = chaos.end_record(
        a, started=w.T0, ended=w.T0, outcome="delegated", executor="windows"
    )
    real = chaos.end_record(
        a, started=w.T0, ended=w.T0, outcome="done", executor="windows"
    )
    for order in ([delegated, real], [real, delegated]):
        (only,) = chaos.logged_actions(order)
        assert only.outcome == "done"
        assert only.applied
