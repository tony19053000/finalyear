"""V0.9 G5 lane B, tester round 1 (adversarial, from the brief + D246/D249, not from the code).

Pure-logic probes:

* ``settle`` over EVERY failure type x tries: only an infrastructure failure
  spends a try and never beyond ``max_infra_retries``; a workload failure and a
  cancel are never requeued; ``owner_preempted`` is requeued for ever without a try;
* job keys: every key one run can ever use (run job / fillers / warm-ups, any
  generation) is distinct, and only the run job's own key parses as a run key;
* the runner: the per-tick submit cap is shared by all campaigns, a crash
  streak recovers, cancellation is never swallowed, ``campaign.progress`` is
  at most 1/s per campaign and one last event after the end;
* the owner guard: a failed pre-emption is retried (a bench attempt must not
  keep running while the owner is active), pause + activity combine;
* the activity rule against an independent reference model (random samples);
* netem tolerance edges; old agents' messages (no G5 fields) mean "off".
"""

from __future__ import annotations

import dataclasses
import itertools
import random
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.conditions.netem import (
    expected_params,
    netem_matches,
    parse_netem_line,
)
from meshforge.agent.owner_activity import ActivityRule, ActivityState, OwnerActivity
from meshforge.agent.owner_guard import OwnerGuard
from meshforge.agent.settings import BenchSettings
from meshforge.common import owner_policy as op
from meshforge.common.bench_conditions import Netem, parse_condition
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_bench_run_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.controller.bench.keys import (
    pre_job_role,
    run_key,
    run_of_key,
    warmup_key,
)
from meshforge.controller.bench.outcome import (
    INFRA_FAILURES,
    WORKLOAD_FAILURES,
    counts_as_infra,
    settle,
)
from meshforge.controller.bench.runner import (
    DEGRADED_AFTER,
    MAX_BACKOFF_S,
    BenchRunner,
    RunnerSettings,
)
from meshforge.controller.bench.submit import SubmitBatch, _pre_jobs, owner_guarded
from meshforge.controller.settings import BenchSettings as ControllerBench

pytestmark = pytest.mark.unit

RUN = str(new_bench_run_id())


# ------------------------------------------------------------ settle, exhaustive


@pytest.mark.parametrize("failure", sorted(FailureType, key=str))
@pytest.mark.parametrize("max_retries", [0, 1, 2, 10])
def test_settle_every_failure_type_over_every_try_count(
    failure: FailureType, max_retries: int
) -> None:
    for tries in range(max_retries + 3):
        out = settle(
            "failed", failure.value, tries=tries, max_infra_retries=max_retries
        )
        if failure is FailureType.OWNER_PREEMPTED:
            # never a try, never failed, however often it happens
            assert out.state == "planned" and out.tries == tries
        elif failure in INFRA_FAILURES and tries < max_retries:
            assert out.state == "planned" and out.tries == tries + 1
        else:
            assert out.state == "failed" and out.tries == tries and not out.requeued
        assert out.tries <= max(tries, max_retries)  # tries never exceed the cap
        # a stopped campaign never requeues anything
        stopped = settle(
            "failed",
            failure.value,
            tries=tries,
            max_infra_retries=max_retries,
            stopped=True,
        )
        assert stopped.state != "planned"


def test_workload_failures_and_cancel_are_never_infra() -> None:
    for failure in WORKLOAD_FAILURES | {FailureType.CANCELLED}:
        assert failure not in INFRA_FAILURES
        assert not counts_as_infra(failure.value)
        assert (
            settle("failed", failure.value, tries=0, max_infra_retries=10).state
            == "failed"
        )


def test_infra_and_preempted_partition_the_failure_types() -> None:
    # every type is exactly one of: retried-infra, owner_preempted, never retried
    for failure in FailureType:
        retried = failure in INFRA_FAILURES
        assert not (retried and failure is FailureType.OWNER_PREEMPTED)
    # the infra rate counts what a retry counts (and nothing a retry skips)
    for failure in FailureType:
        if failure in INFRA_FAILURES:
            assert counts_as_infra(failure.value)


@pytest.mark.parametrize("junk", ["", "NODE_LOST", "node_lost ", "💥", "x" * 500])
def test_settle_survives_a_garbage_failure_type(junk: str) -> None:
    out = settle("failed", junk, tries=0, max_infra_retries=2)
    assert out.state in {"planned", "failed"}
    assert out.last_failure is None or len(out.last_failure) <= 64


def test_a_cancelled_job_state_with_a_missing_type_is_still_decided() -> None:
    # defensive: never raises, bounded retries (a typeless failure is infra but capped)
    states = [
        settle("cancelled", None, tries=t, max_infra_retries=2).state for t in range(5)
    ]
    assert states[:2] == ["planned", "planned"] and set(states[2:]) == {"failed"}


# ------------------------------------------------------------ keys


@pytest.mark.parametrize("k", [1, 2, 3, 5])
def test_all_keys_a_run_can_use_are_distinct_and_only_run_keys_parse(k: int) -> None:
    seen: dict[str, str] = {}
    for gen in range(12):
        for kind, key in (("run", run_key(RUN, gen)), ("warm", warmup_key(RUN, gen))):
            assert key not in seen, (key, seen.get(key), kind)
            seen[key] = f"{kind}{gen}"
    for gen in range(12):
        for key in _pre_jobs(RUN, parse_condition(f"queue:{k}"), gen):
            assert key not in seen, (key, seen.get(key))
            seen[key] = f"filler g{gen}"
    assert sum(1 for v in seen.values() if v.startswith("filler")) == 12 * k
    for key, role in seen.items():
        parsed = run_of_key(key)
        if role.startswith("run"):
            assert parsed == (RUN, int(role[3:]))
        else:
            assert parsed is None, key
            assert pre_job_role(key) is not None


@pytest.mark.parametrize(
    "bad",
    [
        f"bench:{RUN}:0",  # generation 0 has no suffix
        f"bench:{RUN}:00",
        f"bench:{RUN}:1:2",
        f"bench:{RUN}:1234567890",  # > 9 digits
        f"bench:{RUN}: 1",
        f"bench:{RUN}:" + chr(0x661),  # arabic-indic digit: isdecimal but not ascii
        f"bench:{RUN}:1\n",
        f"BENCH:{RUN}",
        "bench:",
        "bench:bench_run_nope:1",
        "",
        None,
    ],
)
def test_run_of_key_refuses_lookalikes(bad: str | None) -> None:
    assert run_of_key(bad) is None


# ------------------------------------------------------------ runner with fakes


@dataclasses.dataclass
class Camp:
    id: str
    max_concurrent_per_node: int = 5
    cooldown_s: float = 0.0
    max_infra_retries: int = 2
    elapsed_s: float = 0.0
    created_by: str | None = None
    spec_yaml: str = (
        "name: x\nseed: 1\nfamilies: [{family: cpu.primes}]\nrepetitions: 3\n"
        "budget: {max_runs: 10}\n"
    )


@dataclasses.dataclass
class Rec:
    id: str
    state: str = "active"
    started_at: Any = 1
    finished_reason: str | None = None
    max_concurrent_per_node: int = 1
    counts: dict[str, int] = dataclasses.field(
        default_factory=lambda: {"planned": 3, "done": 1}
    )


@dataclasses.dataclass
class Out:
    outcome: str = "submitted"


class Control:
    def __init__(self, camps: list[Camp]) -> None:
        self.camps = camps

    async def runner_campaigns(self) -> list[Camp]:
        return self.camps

    async def user_busy_nodes(self) -> set[str]:
        return set()

    async def planned_nodes(self, _cid: str) -> set[str]:
        return {"n1", "n2"}

    async def node_bench_load(self, _n: Any) -> dict[str, int]:
        return {}

    async def seconds_since_last_end(self, _c: str, _n: Any) -> dict[str, float]:
        return {}

    async def node_slots(
        self, cid: str, nodes: Any
    ) -> tuple[dict[str, int], dict[str, float]]:
        # G5c item 0.0: the runner reads both in one snapshot
        return (
            await self.node_bench_load(nodes),
            await self.seconds_since_last_end(cid, nodes),
        )

    async def stop_for_budget(self, _cid: str, _d: str) -> int:
        return 0

    async def status(self, _cid: str, counts: Any, *, per_node: int = 1) -> Any:
        return dataclasses.make_dataclass(
            "S", ["done_percent", "eta_s", "infra_failure_rate"]
        )(25.0, 10.0, 0.0)


class Store:
    def __init__(self, records: list[Rec]) -> None:
        self.records = records
        self.sync_error: Exception | None = None

    async def sync(self, _cid: str | None = None) -> int:
        if self.sync_error is not None:
            raise self.sync_error
        return 0

    async def get_campaign(self, cid: str) -> Rec | None:
        return next((r for r in self.records if r.id == cid), None)

    async def list_campaigns(self, limit: int = 50) -> list[Rec]:
        return self.records


class Submitter:
    def __init__(self) -> None:
        self.counts: list[int] = []

    async def submit_next(
        self, _rec: Any, count: int, *, node_budget: Any, max_examined: int
    ) -> SubmitBatch:
        self.counts.append(count)
        # fix r2 (D251 N1): the typed batch instead of a list
        return SubmitBatch(tuple(Out() for _ in range(count)))  # type: ignore[arg-type]


def _runner(
    camps: list[Camp], *, records: list[Rec] | None = None, **kw: Any
) -> tuple[BenchRunner, Submitter, Store]:
    sub = Submitter()
    store = Store(records if records is not None else [Rec(c.id) for c in camps])
    runner = BenchRunner(
        store=store,  # type: ignore[arg-type]
        control=Control(camps),  # type: ignore[arg-type]
        submitter=sub,  # type: ignore[arg-type]
        settings=kw.pop("settings", RunnerSettings(max_submit_per_tick=4)),
        **kw,
    )
    return runner, sub, store


@pytest.mark.asyncio
async def test_the_submit_cap_is_shared_by_all_campaigns_in_a_tick() -> None:
    runner, sub, _ = _runner([Camp("c1"), Camp("c2"), Camp("c3")])
    report = await runner.tick()
    assert sub.counts == [4]  # c1 takes the whole tick's budget; c2/c3 wait
    assert report.submitted == 4
    runner, sub, _ = _runner(
        [Camp("c1"), Camp("c2")], settings=RunnerSettings(max_submit_per_tick=7)
    )
    await runner.tick()
    assert sum(sub.counts) <= 7


@pytest.mark.asyncio
async def test_a_cancelled_or_paused_campaign_between_read_and_submit_is_not_submitted() -> (
    None
):
    # runner read the campaign as active; by the time of submit it was cancelled
    runner, sub, _ = _runner([Camp("c1")], records=[Rec("c1", state="cancelled")])
    await runner.tick()
    assert sub.counts == []
    runner, sub, _ = _runner([Camp("c1")], records=[Rec("c1", state="paused")])
    await runner.tick()
    assert sub.counts == []


@pytest.mark.asyncio
async def test_a_vanished_campaign_does_not_crash_the_tick() -> None:
    runner, sub, _ = _runner([Camp("c1")], records=[])
    await runner.tick()
    assert sub.counts == []


@pytest.mark.asyncio
async def test_a_garbage_spec_in_the_row_does_not_stop_the_runner() -> None:
    runner, sub, _ = _runner([Camp("c1", spec_yaml=":::not yaml{{")])
    await runner.tick()  # unreadable budget = no budget, never a crash
    assert sub.counts == [4]


@pytest.mark.asyncio
async def test_crash_streak_recovers_backs_off_capped_and_cancel_is_not_swallowed() -> (
    None
):
    import asyncio

    runner, _sub, store = _runner([Camp("c1")], settings=RunnerSettings(tick_s=0.01))
    sleeps: list[float] = []
    ticks = 0

    class Stop(BaseException):
        pass

    async def sleep(s: float) -> None:
        sleeps.append(s)

    store.sync_error = RuntimeError("db down")
    runner._sleep = sleep  # type: ignore[assignment]
    original = runner.tick

    async def counted() -> Any:
        nonlocal ticks
        ticks += 1
        if ticks == DEGRADED_AFTER + 2:
            store.sync_error = None  # the database is back
        if ticks == DEGRADED_AFTER + 4:
            raise asyncio.CancelledError
        return await original()

    runner.tick = counted  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await runner.run()
    assert max(sleeps) <= MAX_BACKOFF_S
    assert runner.crashes == 0 and not runner.degraded  # recovered
    assert len(sleeps) == DEGRADED_AFTER + 1


@pytest.mark.asyncio
async def test_progress_events_at_most_one_per_second_per_campaign() -> None:
    now = [100.0]
    events: list[dict[str, Any]] = []
    recs = [Rec("c1"), Rec("c2")]
    runner, _sub, _ = _runner(
        [], records=recs, publish=events.append, clock=lambda: now[0]
    )
    for _ in range(100):  # 10 s of 0.1 s ticks
        await runner.tick()
        now[0] += 0.1
    for cid in ("c1", "c2"):
        mine = [e for e in events if e["campaign_id"] == cid]
        assert 9 <= len(mine) <= 11, len(mine)  # <= 1/s (+ the first)
    # event payload is bounded and carries the documented keys
    assert set(events[0]) == {
        "campaign_id",
        "state",
        "finished_reason",
        "counts",
        "done_percent",
        "eta_s",
        "infra_failure_rate",
    }
    # the end of a campaign: one final event, then quiet
    recs[0].state, recs[0].finished_reason = "done", "complete"
    before = len([e for e in events if e["campaign_id"] == "c1"])
    for _ in range(30):
        now[0] += 1.0
        await runner.tick()
    final = [e for e in events if e["campaign_id"] == "c1"][before:]
    assert len(final) == 1 and final[0]["state"] == "done"


@pytest.mark.asyncio
async def test_progress_skips_campaigns_never_started() -> None:
    events: list[dict[str, Any]] = []
    runner, _sub, _ = _runner(
        [], records=[Rec("m", started_at=None)], publish=events.append
    )
    await runner.tick()
    assert events == []


def test_controller_defaults_are_the_documented_ones() -> None:
    cfg = ControllerBench()
    assert (cfg.tick_s, cfg.max_submit_per_tick, cfg.runner) == (2.0, 4, True)
    assert RunnerSettings().max_submit_per_tick == 4
    agent = BenchSettings()
    assert (agent.idle_threshold_s, agent.foreground_cpu_percent) == (300.0, 30.0)
    assert (agent.sustain_s, agent.clear_after_s, agent.activity) == (10.0, 60.0, True)


# ------------------------------------------------------------ owner guard


@pytest.mark.asyncio
async def test_a_failed_preemption_is_retried_on_the_next_evaluation() -> None:
    """Invariant: while the guard is on, a running benchmark attempt must be stopped.
    A pre-emption that raised must not be forgotten (the guard already 'was' on)."""
    calls: list[str] = []

    class BoomError(Exception):
        pass

    async def preempt(reason: str) -> int:
        calls.append(reason)
        if len(calls) == 1:
            raise BoomError
        return 1

    class P:
        def __init__(self) -> None:
            self.policy = dataclasses.replace(op.OPEN_POLICY, pause_benchmarks=True)

    guard = OwnerGuard(policy=P(), activity=None, preempt=preempt)  # type: ignore[arg-type]
    with pytest.raises(BoomError):
        await guard.refresh()
    await guard.refresh()  # the policy watcher / activity monitor evaluates again
    assert len(calls) == 2, "the failed pre-emption was never retried"


@pytest.mark.asyncio
async def test_pause_and_activity_combine_and_preempt_once_per_turn_on() -> None:
    class P:
        def __init__(self) -> None:
            self.policy = dataclasses.replace(op.OPEN_POLICY)

    pol = P()
    activity = OwnerActivity(
        BenchSettings(),
        idle=None,
        cpu=type("C", (), {"sample": lambda s: 0.0})(),  # type: ignore[arg-type]
    )
    calls: list[str] = []

    async def preempt(reason: str) -> int:
        calls.append(reason)
        return 0

    guard = OwnerGuard(policy=pol, activity=activity, preempt=preempt)  # type: ignore[arg-type]
    assert await guard.refresh() == 0 and calls == []
    pol.policy = dataclasses.replace(op.OPEN_POLICY, pause_benchmarks=True)
    await guard.refresh()
    assert calls == ["paused"] and guard.blocked() == "paused"
    activity.state = ActivityState(active=True, reason="idle", idle_source="logind")
    await guard.refresh()  # paused -> paused+active: still on, no second pre-emption
    assert calls == ["paused"]
    pol.policy = dataclasses.replace(op.OPEN_POLICY)
    await guard.refresh()  # resume the switch, owner still active: still blocked
    assert guard.blocked() == "owner_active:idle" and calls == ["paused"]
    activity.state = ActivityState()
    await guard.refresh()
    assert guard.blocked() is None
    pol.policy = dataclasses.replace(op.OPEN_POLICY, pause_benchmarks=True)
    await guard.refresh()
    assert calls == ["paused", "paused"]  # a new turn-on pre-empts again


def test_controller_side_guard_tolerates_old_or_partial_sessions() -> None:
    class Old:  # an object with neither attribute (a session from before G5)
        pass

    assert owner_guarded(Old()) is None
    assert owner_guarded(None) is None

    class NoPolicy:
        policy = None
        owner_active = True

    assert owner_guarded(NoPolicy()) is not None  # activity alone still guards


def test_messages_from_an_agent_without_the_g5_fields_mean_off() -> None:
    hb = agent_pb2.Heartbeat.FromString(
        agent_pb2.Heartbeat(sequence=7).SerializeToString()
    )
    assert (hb.owner_active, hb.owner_active_reason, hb.idle_source) == (False, "", "")
    policy = op.from_proto(agent_pb2.OwnerPolicy(accept_jobs=True))
    assert policy.pause_benchmarks is False
    start = agent_pb2.StartAttempt()
    assert start.benchmark is False
    ask = op.JobAsk(owner_name="a")
    assert op.refusal(policy, ask) is None
    assert op.refusal(policy, op.JobAsk(owner_name="a", benchmark=True)) is None


# ------------------------------------------------------------ activity rule model


def _reference(
    samples: list[tuple[float, float | None, float | None]],
    *,
    idle_t: float,
    cpu_t: float,
    sustain: float,
    clear: float,
) -> list[bool]:
    """Independent model: evidence at t = idle < thr (thr>0) or CPU > limit
    continuously since >= sustain; active = evidence within the last `clear` s
    (or now)."""
    out: list[bool] = []
    since: float | None = None
    last: float | None = None
    for t, idle, cpu in samples:
        if cpu is not None:
            since = (t if since is None else since) if cpu > cpu_t else None
        ev = (idle is not None and idle_t > 0 and idle < idle_t) or (
            since is not None and t - since >= sustain
        )
        if ev:
            last = t
        out.append(last is not None and (ev or t - last < clear))
    return out


@pytest.mark.parametrize("seed", range(40))
def test_activity_rule_matches_the_reference_model(seed: int) -> None:
    rnd = random.Random(seed)  # noqa: S311
    cfg = BenchSettings(
        idle_threshold_s=rnd.choice([0.0, 30.0, 300.0]),
        foreground_cpu_percent=rnd.choice([10.0, 30.0, 80.0]),
        sustain_s=rnd.choice([0.0, 4.0, 10.0]),
        clear_after_s=rnd.choice([0.0, 20.0, 60.0]),
    )
    rule = ActivityRule(cfg)
    samples: list[tuple[float, float | None, float | None]] = []
    t = 0.0
    for _ in range(400):
        t += rnd.choice([0.5, 2.0, 2.0, 7.0])
        idle = rnd.choice([None, 1.0, 29.9, 30.0, 299.0, 300.0, 5000.0])
        cpu = rnd.choice([None, 0.0, 9.9, 10.0, 30.0, 30.1, 79.9, 95.0])
        samples.append((t, idle, cpu))
    want = _reference(
        samples,
        idle_t=cfg.idle_threshold_s,
        cpu_t=cfg.foreground_cpu_percent,
        sustain=cfg.sustain_s,
        clear=cfg.clear_after_s,
    )
    got = []
    for ts, idle, cpu in samples:
        rule.observe(ts, idle_s=idle, cpu_percent=cpu)
        got.append(rule.active)
    mism = [i for i, (a, b) in enumerate(zip(got, want, strict=True)) if a != b]
    assert not mism, (seed, mism[:3], samples[mism[0]] if mism else None)


def test_activity_rule_boundaries_are_strict_and_hysteresis_exact() -> None:
    cfg = BenchSettings(
        idle_threshold_s=300.0,
        foreground_cpu_percent=30.0,
        sustain_s=0.0,
        clear_after_s=60.0,
    )
    rule = ActivityRule(cfg)
    rule.observe(0.0, idle_s=300.0, cpu_percent=30.0)  # == thresholds: not active
    assert not rule.active
    rule.observe(1.0, idle_s=299.99, cpu_percent=None)
    assert rule.active
    rule.observe(60.99, idle_s=None, cpu_percent=None)  # unknown idle, no CPU: hold
    assert rule.active
    rule.observe(61.0, idle_s=None, cpu_percent=None)  # exactly 60 s quiet: clear
    assert not rule.active


def test_nan_and_negative_samples_never_raise_or_wedge() -> None:
    rule = ActivityRule(BenchSettings())
    for i, (idle, cpu) in enumerate(
        itertools.product([float("nan"), -5.0, float("inf"), None], repeat=2)
    ):
        rule.observe(float(i), idle_s=idle, cpu_percent=cpu)
    # -5 s idle is 'just now' (< threshold) -> active, but nothing raised


# ------------------------------------------------------------ netem tolerance edges


def _expect(spec: str) -> dict[str, float | None]:
    netem = parse_condition(spec)
    assert isinstance(netem, Netem)
    return expected_params(netem)


@pytest.mark.parametrize(
    ("line", "ours"),
    [
        ("qdisc netem 8001: root limit 1000 delay 50ms", True),
        ("qdisc netem 8001: root limit 1000 delay 50.5ms", True),  # +1.0 %
        ("qdisc netem 8001: root limit 1000 delay 50.6ms", False),  # +1.2 %
        ("qdisc netem 8001: root limit 1000 delay 49.5ms", True),
        ("qdisc netem 8001: root limit 1000 delay 49.4ms", False),
        ("qdisc netem 8001: root limit 1000 delay 0.05s", True),  # other unit
        ("qdisc netem 8001: root limit 1000 delay 50000us", True),
        ("qdisc netem 8001: root limit 1000 delay 50ms loss 0.0001%", False),
        ("qdisc netem 8001: root limit 1000 delay 50ms rate 1Mbit", False),
        ("qdisc netem 8001: root limit 1000 delay 50ms 5ms", False),  # jitter
        # fix r1 T1-2 (was a strict xfail): an unknown word = not ours
        ("qdisc netem 8001: root limit 1000 delay 50ms duplicate 1%", False),
        ("qdisc netem 8001: parent 1:1 limit 1000 delay 50ms", True),  # parsed line
    ],
)
def test_netem_tolerance_edges(line: str, ours: bool) -> None:
    # (the parent case documents that the matcher alone does not check 'root';
    # delete_netem only ever feeds it root lines)
    assert netem_matches(line, _expect("netem:delay=50ms")) is ours


def test_netem_zero_means_not_set() -> None:
    expect = _expect("netem:loss=2%")
    assert netem_matches("qdisc netem 8001: root limit 1000 loss 2%", expect)
    assert not netem_matches(
        "qdisc netem 8001: root limit 1000 delay 1ms loss 2%", expect
    )


def test_netem_parse_never_raises_on_garbage() -> None:
    for line in (
        "",
        "netem",
        "netem delay",
        "netem delay %",
        "netem loss 1%% 2%%",
        "netem rate",
    ):
        parse_netem_line(line)
