"""V0.9 G5 lane B item 3 (D219/D246): the agent's owner guard, pure parts.

* :class:`ActivityRule` - idle below the threshold or non-fabric CPU above the
  limit for ``sustain_s`` -> active; clears only after ``clear_after_s``
  quiet (hysteresis); unknown idle -> the CPU rule alone;
* :class:`LogindIdle` - the graphical sessions' idle hint through ``loginctl``
  (fake runner); no graphical session / a hint never set -> unknown;
* :class:`CpuSampler` - the fabric's process tree (the agent, its children,
  container shims) is excluded;
* :class:`OwnerGuard` - blocked by the pause switch or activity, pre-empts
  once per turn-on;
* ``meshforge-agent bench pause|resume`` writes ``pause_benchmarks`` in the
  live policy file; the live policy carries it; default false.
"""

from __future__ import annotations

import itertools
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from meshforge.agent.main import app
from meshforge.agent.owner_activity import (
    ActivityRule,
    ActivityState,
    CpuSampler,
    LogindIdle,
    OwnerActivity,
    UnknownIdle,
)
from meshforge.agent.owner_guard import OwnerGuard
from meshforge.agent.policy import LivePolicy, parse_assignments, policy_path
from meshforge.agent.settings import AgentSettings, BenchSettings, PolicySettings
from meshforge.common.exit_codes import ExitCode

pytestmark = pytest.mark.unit
cli = CliRunner()


def _rule(**kw: Any) -> ActivityRule:
    return ActivityRule(
        BenchSettings(
            idle_threshold_s=300.0,
            foreground_cpu_percent=30.0,
            sustain_s=10.0,
            clear_after_s=60.0,
            **kw,
        )
    )


# ------------------------------------------------------------ ActivityRule


def test_recent_input_is_active_at_once_and_clears_after_quiet() -> None:
    rule = _rule()
    assert rule.observe(0.0, idle_s=5.0, cpu_percent=0.0) is True
    assert rule.active and rule.reason == "idle"
    # input stops: still active (hysteresis) until 60 s after the last hit
    assert rule.observe(30.0, idle_s=301.0, cpu_percent=0.0) is False and rule.active
    assert rule.observe(59.0, idle_s=301.0, cpu_percent=0.0) is False and rule.active
    assert rule.observe(60.0, idle_s=361.0, cpu_percent=0.0) is True
    assert not rule.active and rule.reason == ""


def test_cpu_must_hold_for_sustain_s() -> None:
    rule = _rule()
    assert rule.observe(0.0, idle_s=None, cpu_percent=90.0) is False
    assert rule.observe(9.0, idle_s=None, cpu_percent=90.0) is False
    assert rule.observe(10.0, idle_s=None, cpu_percent=90.0) is True
    assert rule.reason == "cpu"


def test_a_cpu_dip_resets_the_sustain_window() -> None:
    rule = _rule()
    rule.observe(0.0, idle_s=None, cpu_percent=90.0)
    rule.observe(5.0, idle_s=None, cpu_percent=1.0)
    assert rule.observe(12.0, idle_s=None, cpu_percent=90.0) is False
    assert rule.observe(22.0, idle_s=None, cpu_percent=90.0) is True


def test_unknown_idle_is_never_guessed_cpu_rule_only() -> None:
    rule = _rule()
    for t in range(0, 200, 2):
        assert rule.observe(float(t), idle_s=None, cpu_percent=5.0) is False
    assert not rule.active


def test_idle_threshold_zero_ignores_idle() -> None:
    rule = ActivityRule(BenchSettings(idle_threshold_s=0.0))
    assert rule.observe(0.0, idle_s=0.0, cpu_percent=0.0) is False


def test_both_rules_reason() -> None:
    rule = ActivityRule(BenchSettings(sustain_s=0.0))
    rule.observe(0.0, idle_s=1.0, cpu_percent=99.0)
    assert rule.reason == "idle+cpu"


# ------------------------------------------------------------ LogindIdle


class FakeLoginctl:
    def __init__(self, sessions: dict[str, dict[str, str]]) -> None:
        self.sessions = sessions
        self.calls: list[list[str]] = []

    def __call__(self, args: list[str]) -> str | None:
        self.calls.append(args)
        if args[0] == "list-sessions":
            return "".join(f"{sid} 1000 alice seat0 tty2\n" for sid in self.sessions)
        if args[0] == "show-session":
            props = self.sessions.get(args[1])
            if props is None:
                return None
            wanted = [args[i + 1] for i, a in enumerate(args) if a == "-p"]
            return "".join(f"{k}={props[k]}\n" for k in wanted if k in props)
        return None


def _logind(sessions: dict[str, dict[str, str]], now_us: float) -> LogindIdle:
    return LogindIdle(FakeLoginctl(sessions), monotonic_us=lambda: now_us)


def test_logind_idle_from_the_hint() -> None:
    gui = {
        "Type": "wayland",
        "Active": "yes",
        "IdleHint": "yes",
        "IdleSinceHintMonotonic": "1000000000",
    }
    assert _logind({"2": gui}, now_us=1_400_000_000).idle_s() == pytest.approx(400.0)
    busy = {**gui, "IdleHint": "no"}
    assert _logind({"2": busy}, now_us=1_400_000_000).idle_s() == 0.0


def test_logind_without_a_graphical_session_or_hint_is_unknown() -> None:
    tty = {
        "Type": "tty",
        "Active": "yes",
        "IdleHint": "no",
        "IdleSinceHintMonotonic": "5",
    }
    assert _logind({"3": tty}, now_us=1e9).idle_s() is None
    never = {
        "Type": "x11",
        "Active": "yes",
        "IdleHint": "no",
        "IdleSinceHintMonotonic": "0",
    }
    assert _logind({"4": never}, now_us=1e9).idle_s() is None
    inactive = {**never, "Active": "no", "IdleSinceHintMonotonic": "7"}
    assert _logind({"5": inactive}, now_us=1e9).idle_s() is None
    assert _logind({}, now_us=1e9).idle_s() is None


@pytest.mark.security
def test_logind_refuses_odd_session_ids_and_reads_only_hint_properties() -> None:
    fake = FakeLoginctl({"2; rm -rf": {"Type": "x11", "Active": "yes"}})
    reader = LogindIdle(fake, monotonic_us=lambda: 1e9)
    assert reader.idle_s() is None
    assert all(c[0] == "list-sessions" for c in fake.calls)  # never shown
    fake2 = FakeLoginctl(
        {
            "2": {
                "Type": "x11",
                "Active": "yes",
                "IdleHint": "yes",
                "IdleSinceHintMonotonic": "1",
            }
        }
    )
    LogindIdle(fake2, monotonic_us=lambda: 1e9).idle_s()
    props = {c[i + 1] for c in fake2.calls for i, a in enumerate(c) if a == "-p"}
    assert props <= {"Type", "Active", "IdleHint", "IdleSinceHintMonotonic"}


# ------------------------------------------------------------ CpuSampler


def test_the_fabric_tree_and_container_shims_are_excluded() -> None:
    sampler = CpuSampler(root_pid=100)
    table = {
        100: (1, "meshforge-agent"),
        101: (100, "python"),  # a job
        102: (101, "python"),  # a burner / grandchild
        200: (1, "containerd-shim-runc-v2"),
        201: (200, "python"),  # a docker job
        300: (1, "firefox"),  # the owner
        400: (1, "vmmem"),
    }
    fabric = sampler._fabric(table)
    assert {100, 101, 102, 200, 201} <= fabric
    assert 300 not in fabric and 400 not in fabric  # no attempt running
    busy = CpuSampler(root_pid=100, attempts_running=lambda: True)
    assert 400 in busy._fabric(table)  # Docker Desktop's VM while a job runs


def test_cpu_sampler_first_sample_has_no_window() -> None:
    sampler = CpuSampler()
    assert sampler.sample() is None
    second = sampler.sample()
    assert second is None or 0.0 <= second <= 100.0


# ------------------------------------------------------------ OwnerActivity + guard


class _Idle:
    name = "logind"

    def __init__(self) -> None:
        self.value: float | None = 1000.0

    def idle_s(self) -> float | None:
        return self.value


class _Cpu:
    def sample(self) -> float | None:
        return 0.0


@pytest.mark.asyncio
async def test_activity_flip_calls_the_guard_which_preempts_once() -> None:
    clock = [0.0]
    idle = _Idle()
    activity = OwnerActivity(
        BenchSettings(clear_after_s=60.0),
        idle=idle,
        cpu=_Cpu(),  # type: ignore[arg-type]
        clock=lambda: clock[0],
    )
    preempted: list[str] = []

    async def preempt(reason: str) -> int:
        preempted.append(reason)
        return 1

    guard = OwnerGuard(policy=None, activity=activity, preempt=preempt)
    activity.on_change = guard.on_activity
    assert await activity.step() is False and guard.blocked() is None
    assert guard.heartbeat_fields() == (False, "", "logind")
    idle.value = 3.0
    assert await activity.step() is True
    assert guard.blocked() == "owner_active:idle"
    assert preempted == ["owner_active:idle"]
    assert guard.heartbeat_fields() == (True, "idle", "logind")
    clock[0] = 10.0
    await activity.step()  # still active: no second pre-emption
    assert preempted == ["owner_active:idle"]
    idle.value = 999.0
    clock[0] = 80.0
    assert await activity.step() is True and guard.blocked() is None
    assert activity.state == ActivityState(
        active=False, reason="", idle_source="logind"
    )


@pytest.mark.asyncio
async def test_unknown_idle_source_is_reported() -> None:
    activity = OwnerActivity(BenchSettings(), idle=UnknownIdle(), cpu=_Cpu())  # type: ignore[arg-type]
    await activity.step()
    guard = OwnerGuard(policy=None, activity=activity)
    assert guard.heartbeat_fields() == (False, "", "unknown")


# ------------------------------------------------------------ policy + CLI


@pytest.fixture
def data_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for name in list(os.environ):
        if name.upper().startswith("MESHFORGE_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    data_dir = tmp_path / "data"
    monkeypatch.setenv("MESHFORGE_DATA_DIR", str(data_dir))
    return data_dir


def test_pause_benchmarks_is_a_live_key_default_false() -> None:
    assert PolicySettings().pause_benchmarks is False
    assert PolicySettings().to_policy().pause_benchmarks is False
    assert parse_assignments(["pause_benchmarks=yes"]) == {"pause_benchmarks": True}


def test_bench_pause_and_resume_write_the_live_policy(data_dir: Path) -> None:
    result = cli.invoke(app, ["bench", "pause"], prog_name="meshforge-agent")
    assert result.exit_code == ExitCode.OK, result.output
    settings = AgentSettings.load(data_dir=data_dir)
    live = LivePolicy(
        settings.agent.policy,
        policy_path(settings),
        gpu_count=0,
        max_concurrent_attempts=settings.agent.execution.max_concurrent_attempts,
    )
    assert live.policy.pause_benchmarks is True
    guard = OwnerGuard(policy=live, activity=None)
    assert guard.blocked() == "paused"
    result = cli.invoke(app, ["bench", "resume"], prog_name="meshforge-agent")
    assert result.exit_code == ExitCode.OK, result.output
    assert live.reload() is True
    assert live.policy.pause_benchmarks is False and guard.blocked() is None


# ------------------------------------------------------------ fix r2 (D251 B1)


@pytest.mark.asyncio
async def test_a_failing_preemption_retries_on_polls_with_backoff_capped_at_30s() -> (
    None
):
    import structlog

    clock = [0.0]
    idle = _Idle()
    idle.value = 1.0  # the owner is active from the first sample
    activity = OwnerActivity(
        BenchSettings(poll_s=2.0, clear_after_s=3600.0),
        idle=idle,
        cpu=_Cpu(),  # type: ignore[arg-type]
        clock=lambda: clock[0],
    )
    tries: list[float] = []

    async def preempt(_reason: str) -> int:
        tries.append(clock[0])
        raise OSError("cannot stop")

    guard = OwnerGuard(
        policy=None, activity=activity, preempt=preempt, clock=lambda: clock[0]
    )
    activity.on_change = guard.on_activity
    with structlog.testing.capture_logs() as logs:
        with pytest.raises(OSError):
            await activity.step()  # the flip
        for _ in range(200):  # 400 s of polls, every 2 s
            clock[0] += 2.0
            await activity.step()
    gaps = [round(b - a, 1) for a, b in itertools.pairwise(tries)]
    assert gaps[:5] == [2.0, 4.0, 8.0, 16.0, 30.0]  # one poll, then doubling
    assert all(g == 30.0 for g in gaps[4:])  # capped
    failed = [e for e in logs if e["event"] == "agent.bench.preempt_failed"]
    assert len(failed) == len(tries)  # one line per step, no storm


@pytest.mark.asyncio
async def test_a_recovered_preemption_is_not_repeated_and_resets_on_clear() -> None:
    clock = [0.0]
    idle = _Idle()
    idle.value = 1.0
    activity = OwnerActivity(
        BenchSettings(poll_s=2.0, clear_after_s=10.0),
        idle=idle,
        cpu=_Cpu(),  # type: ignore[arg-type]
        clock=lambda: clock[0],
    )
    calls: list[float] = []

    async def preempt(_reason: str) -> int:
        calls.append(clock[0])
        if len(calls) == 1:
            raise OSError("once")
        return 1

    guard = OwnerGuard(
        policy=None, activity=activity, preempt=preempt, clock=lambda: clock[0]
    )
    activity.on_change = guard.on_activity
    with pytest.raises(OSError):
        await activity.step()
    for _ in range(10):
        clock[0] += 2.0
        await activity.step()
    assert len(calls) == 2 and guard.preempted == 1  # retried once, then done
    idle.value = 9999.0
    for _ in range(10):
        clock[0] += 2.0
        await activity.step()
    assert guard.blocked() is None
    idle.value = 1.0
    clock[0] += 2.0
    await activity.step()  # on again: a new pre-emption
    assert len(calls) == 3
