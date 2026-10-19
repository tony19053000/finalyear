"""V0.9 G5 lane B (0.9.7, D219/D246): the pure parts of the runner and the guard.

* :func:`meshforge.controller.bench.outcome.settle` - done / failed / requeued
  (infra: ``tries + 1`` up to ``max_infra_retries``; ``owner_preempted``:
  ``tries`` unchanged), stopped campaigns skip instead of requeueing;
* the job keys of run generations (``bench:<run>[:<g>]``), fillers / warm-ups
  per generation, and that they stay benchmark keys;
* the owner policy's ``pause_benchmarks`` (default false, proto round trip,
  refuses only benchmark asks) and the planner's live ``policy_bench_paused``
  (benchmark jobs wait, a user job is never refused for it);
* the runner's per-node budget (user job / owner guard / cool-down / full)
  and the per-tick cap, with fakes;
* ``compute bench status`` text.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.cli.bench_runner_commands import status_lines
from meshforge.common import owner_policy as op
from meshforge.common.bench_conditions import parse_condition
from meshforge.common.errors import FailureType
from meshforge.common.ids import new_bench_run_id
from meshforge.controller.bench.keys import (
    filler_key,
    pre_job_role,
    run_id_of_key,
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
from meshforge.controller.bench.runner import BenchRunner, RunnerSettings
from meshforge.controller.bench.submit import SubmitBatch, _pre_jobs, owner_guarded
from meshforge.controller.jobs.queue_reads import is_benchmark_key
from meshforge.controller.planner.feasibility import bench_guard, live_rejection
from meshforge.controller.planner.types import (
    STATIC_CODES,
    NodeSnapshot,
    PlanRequest,
    RejectCode,
)
from meshforge.controller.telemetry.record import (
    WORKLOAD_FAILURES as RECORD_WORKLOAD_FAILURES,
)

pytestmark = pytest.mark.unit

RUN = str(new_bench_run_id())


# ------------------------------------------------------------ settle


def test_workload_failures_are_the_record_class_set() -> None:
    assert WORKLOAD_FAILURES == RECORD_WORKLOAD_FAILURES


def test_success_is_done() -> None:
    out = settle("succeeded", None, tries=1, max_infra_retries=2)
    assert (out.state, out.tries, out.requeued) == ("done", 1, False)


@pytest.mark.parametrize("tries", [0, 1])
def test_infra_failure_is_requeued_with_a_try_spent(tries: int) -> None:
    out = settle("failed", "node_lost", tries=tries, max_infra_retries=2)
    assert out.state == "planned" and out.tries == tries + 1 and out.requeued
    assert out.last_failure == "node_lost"


def test_infra_failure_past_the_retries_fails() -> None:
    out = settle("failed", "transfer_error", tries=2, max_infra_retries=2)
    assert (out.state, out.tries) == ("failed", 2)


def test_zero_retries_fails_at_once() -> None:
    assert settle("failed", "timeout", tries=0, max_infra_retries=0).state == "failed"


@pytest.mark.parametrize("tries", [0, 2, 5])
def test_owner_preempted_is_requeued_without_spending_a_try(tries: int) -> None:
    out = settle("failed", "owner_preempted", tries=tries, max_infra_retries=2)
    assert out.state == "planned" and out.tries == tries


@pytest.mark.parametrize(
    "failure",
    [
        "user_error",
        "oom",
        "deadline_exceeded",
        "config_error",
        "cancelled",
        "no_feasible_plan",
        "unauthorized",
        "invalid_input",
    ],
)
def test_never_retried(failure: str) -> None:
    out = settle("failed", failure, tries=0, max_infra_retries=2)
    assert out.state == "failed" and not out.requeued


def test_a_stopped_campaign_skips_what_it_would_requeue() -> None:
    out = settle(
        "failed", "owner_preempted", tries=0, max_infra_retries=2, stopped=True
    )
    assert out.state == "skipped"
    out = settle("failed", "node_lost", tries=0, max_infra_retries=2, stopped=True)
    assert out.state == "skipped"
    assert (
        settle("succeeded", None, tries=0, max_infra_retries=2, stopped=True).state
        == "done"
    )


def test_infra_rate_excludes_owner_preempted() -> None:
    assert not counts_as_infra("owner_preempted")
    assert counts_as_infra("node_lost")
    assert not counts_as_infra("user_error")
    assert FailureType.OWNER_PREEMPTED not in INFRA_FAILURES


# ------------------------------------------------------------ keys


def test_generation_keys() -> None:
    assert run_key(RUN) == f"bench:{RUN}"  # G3/G4 keys unchanged
    assert run_key(RUN, 3) == f"bench:{RUN}:3"
    assert run_of_key(run_key(RUN)) == (RUN, 0)
    assert run_of_key(run_key(RUN, 3)) == (RUN, 3)
    assert run_id_of_key(run_key(RUN, 3)) == RUN
    for bad in (
        f"bench:{RUN}:03",
        f"bench:{RUN}:x",
        f"bench:{RUN}:-1",
        f"bench:{RUN}:",
    ):
        assert run_of_key(bad) is None
    assert run_of_key(filler_key(RUN, 2)) is None
    assert run_of_key(warmup_key(RUN, 2)) is None
    assert pre_job_role(warmup_key(RUN, 2)) == ("warmup", RUN)
    assert pre_job_role(warmup_key(RUN)) == ("warmup", RUN)
    assert pre_job_role(run_key(RUN, 2)) is None
    for key in (run_key(RUN, 4), filler_key(RUN, 9), warmup_key(RUN, 4)):
        assert is_benchmark_key(key)


def test_a_requeued_run_gets_new_fillers_and_a_new_warmup() -> None:
    queue = parse_condition("queue:2")
    assert _pre_jobs(RUN, queue, 0) == [filler_key(RUN, 1), filler_key(RUN, 2)]
    assert _pre_jobs(RUN, queue, 1) == [filler_key(RUN, 3), filler_key(RUN, 4)]
    warm = parse_condition("cache:warm")
    assert _pre_jobs(RUN, warm, 0) == [warmup_key(RUN)]
    assert _pre_jobs(RUN, warm, 2) == [warmup_key(RUN, 2)]


# ------------------------------------------------------------ policy + planner


def test_pause_benchmarks_defaults_false_and_round_trips() -> None:
    assert op.OwnerPolicy().pause_benchmarks is False
    assert op.from_proto(op.to_proto(op.OPEN_POLICY)).pause_benchmarks is False
    paused = dataclasses.replace(op.OPEN_POLICY, pause_benchmarks=True)
    assert op.from_proto(op.to_proto(paused)).pause_benchmarks is True
    assert paused.as_dict()["pause_benchmarks"] is True


def test_pause_refuses_only_benchmark_asks() -> None:
    paused = dataclasses.replace(op.OPEN_POLICY, pause_benchmarks=True)
    assert op.refusal(paused, op.JobAsk(owner_name="a")) is None  # a user job
    refused = op.refusal(paused, op.JobAsk(owner_name="a", benchmark=True))
    assert refused is not None and refused.code is op.PolicyCode.BENCH_PAUSED
    assert op.refusal(op.OPEN_POLICY, op.JobAsk(owner_name="a", benchmark=True)) is None


def _node(**kw: Any) -> NodeSnapshot:
    base: dict[str, Any] = {
        "state": "online",
        "connected": True,
        "capabilities": frozenset({"exec", "exec-subprocess"}),
        "cpu_slots": 2,
        "policy": op.OPEN_POLICY,
    }
    base.update(kw)
    return NodeSnapshot(node_id="n1", **base)


def _request(**kw: Any) -> PlanRequest:
    base: dict[str, Any] = {
        "job_id": "job_01J0000000000000000000000",
        "attempt_no": 1,
        "gpu": "none",
        "vram_bytes": None,
        "ram_bytes": None,
        "cpus": None,
        "base": "python-cpu",
        "env_key": "e" * 64,
    }
    base.update(kw)
    return PlanRequest(**base)


@pytest.mark.security
@pytest.mark.parametrize(
    "node_kw",
    [
        {"policy": dataclasses.replace(op.OPEN_POLICY, pause_benchmarks=True)},
        {"owner_active": True},
    ],
)
def test_the_guard_is_a_live_rejection_for_bench_jobs_only(
    node_kw: dict[str, Any],
) -> None:
    node = _node(**node_kw)
    bench = live_rejection(
        _request(benchmark=True), node, max_attempts_per_node=8, local=False
    )
    assert bench is not None and bench.code is RejectCode.BENCH_PAUSED
    assert not bench.static and RejectCode.BENCH_PAUSED not in STATIC_CODES
    user = live_rejection(_request(), node, max_attempts_per_node=8, local=False)
    assert user is None  # checklist 30: a user job is never held back
    assert bench_guard(_node()) is None


def test_owner_guarded_reads_the_live_session() -> None:
    class Live:
        def __init__(self, policy: Any, active: bool) -> None:
            self.policy, self.owner_active = policy, active

    assert owner_guarded(None) is None
    assert owner_guarded(Live(op.OPEN_POLICY, False)) is None
    assert owner_guarded(Live(op.OPEN_POLICY, True)) is not None
    paused = dataclasses.replace(op.OPEN_POLICY, pause_benchmarks=True)
    assert owner_guarded(Live(paused, False)) is not None


# ------------------------------------------------------------ runner budget


@dataclasses.dataclass
class _Campaign:
    id: str = "camp_x"
    spec_yaml: str = (
        "name: x\nseed: 1\nfamilies: [{family: cpu.primes}]\nrepetitions: 3\n"
        "budget: {max_runs: 10}\n"
    )
    max_concurrent_per_node: int = 2
    cooldown_s: float = 5.0
    max_infra_retries: int = 2
    elapsed_s: float = 0.0
    created_by: str | None = None


class _Control:
    def __init__(self) -> None:
        self.campaigns = [_Campaign()]
        self.nodes = {"a", "b", "c", "d", "e"}
        self.load = {"a": 0, "b": 0, "c": 1, "d": 2, "e": 0}
        self.since = {"b": 1.0}
        self.busy = {"e"}
        self.budget_stops: list[str] = []

    async def runner_campaigns(self) -> list[_Campaign]:
        return self.campaigns

    async def user_busy_nodes(self) -> set[str]:
        return self.busy

    async def planned_nodes(self, _cid: str) -> set[str]:
        return self.nodes

    async def node_bench_load(self, _nodes: Any) -> dict[str, int]:
        return self.load

    async def seconds_since_last_end(self, _cid: str, _nodes: Any) -> dict[str, float]:
        return self.since

    async def stop_for_budget(self, cid: str, _detail: str) -> int:
        self.budget_stops.append(cid)
        return 0


class _Record:
    id = "camp_x"
    state = "active"
    created_by = None


class _Store:
    def __init__(self) -> None:
        self.syncs = 0

    async def sync(self, _cid: str | None = None) -> int:
        self.syncs += 1
        return 0

    async def get_campaign(self, _cid: str) -> _Record:
        return _Record()

    async def list_campaigns(self, limit: int = 50) -> list[Any]:
        return []


class _Submitter:
    def __init__(self) -> None:
        self.calls: list[tuple[int, dict[str, int], int]] = []

    async def submit_next(
        self, _record: Any, count: int, *, node_budget: Any, max_examined: int
    ) -> list[Any]:
        self.calls.append((count, dict(node_budget), max_examined))
        return SubmitBatch()


@dataclasses.dataclass
class _LiveFake:
    policy: Any = None
    owner_active: bool = False


@pytest.mark.asyncio
async def test_tick_budget_skips_user_cooldown_full_and_guarded_nodes() -> None:
    control, store, submitter = _Control(), _Store(), _Submitter()
    guarded = {"a": _LiveFake(owner_active=True)}

    class Sessions:
        def get(self, node_id: str) -> Any:
            return guarded.get(node_id)

    runner = BenchRunner(
        store=store,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        submitter=submitter,  # type: ignore[arg-type]
        settings=RunnerSettings(max_submit_per_tick=4, max_examine_per_tick=16),
        sessions=Sessions(),
    )
    report = await runner.tick()
    assert report.excluded == {
        "a": "owner_guard",
        "b": "cooldown",
        "d": "full",
        "e": "user_job",
    }
    # only c is left: 2 per node - 1 running = 1 free slot
    assert submitter.calls == [(1, {"c": 1}, 16)]
    assert store.syncs == 1


@pytest.mark.asyncio
async def test_tick_count_is_capped_at_max_submit_per_tick() -> None:
    control, store, submitter = _Control(), _Store(), _Submitter()
    control.nodes = {f"n{i}" for i in range(10)}
    control.load, control.since, control.busy = {}, {}, set()
    runner = BenchRunner(
        store=store,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        submitter=submitter,  # type: ignore[arg-type]
        settings=RunnerSettings(max_submit_per_tick=4),
    )
    await runner.tick()
    count, budget, _ = submitter.calls[0]
    assert count == 4  # 10 nodes x 2 free slots = 20, capped (D245/N4)
    assert sum(budget.values()) == 20


@pytest.mark.asyncio
async def test_budget_hours_stop_submitting() -> None:
    control, store, submitter = _Control(), _Store(), _Submitter()
    control.campaigns = [
        _Campaign(
            spec_yaml=(
                "name: x\nseed: 1\nfamilies: [{family: cpu.primes}]\nrepetitions: 3\n"
                "budget: {max_runs: 10, max_hours: 0.5}\n"
            ),
            elapsed_s=1801.0,
        )
    ]
    runner = BenchRunner(
        store=store,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        submitter=submitter,  # type: ignore[arg-type]
        settings=RunnerSettings(),
    )
    report = await runner.tick()
    assert report.budget_stopped == ["camp_x"] and control.budget_stops == ["camp_x"]
    assert submitter.calls == []


@pytest.mark.asyncio
async def test_a_crashing_tick_is_supervised_and_degrades_after_five() -> None:
    sleeps: list[float] = []

    class Boom(_Store):
        async def sync(self, _cid: str | None = None) -> int:
            raise RuntimeError("db gone")

    async def sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) >= 6:
            raise _Done

    class _Done(BaseException):
        pass

    runner = BenchRunner(
        store=Boom(),  # type: ignore[arg-type]
        control=_Control(),  # type: ignore[arg-type]
        submitter=_Submitter(),  # type: ignore[arg-type]
        settings=RunnerSettings(tick_s=0.01),
        sleep=sleep,
    )
    with pytest.raises(_Done):
        await runner.run()
    assert runner.crashes == 6 and runner.degraded
    assert sleeps == sorted(sleeps)  # backing off


# ------------------------------------------------------------ status text


def test_status_lines() -> None:
    lines = status_lines(
        {
            "campaign_id": "camp_x",
            "state": "done",
            "finished_reason": "budget",
            "runner_owned": True,
            "counts": {
                "planned": 0,
                "submitted": 0,
                "done": 8,
                "failed": 1,
                "skipped": 1,
            },
            "done_percent": 100.0,
            "eta_s": 0.0,
            "median_run_s": 12.0,
            "infra_failure_rate": 0.1,
            "infra_failures": 1,
            "records": 10,
            "retries": 1,
            "preempted": 2,
            "max_concurrent_per_node": 1,
            "cooldown_s": 5.0,
            "max_infra_retries": 2,
        }
    )
    assert lines[0] == "camp_x: done (budget), driven by runner"
    assert (
        "10.0 %" in lines[3] and "2 stopped by the owner guard, not counted" in lines[3]
    )


# ------------------------------------------------------------ fix r1: cursor (D249)


@dataclasses.dataclass
class _Run:
    id: str
    seq: int
    node_id: str = "n1"


class _PagedStore:
    def __init__(self, runs: list[_Run]) -> None:
        self.rows = runs

    async def runs(
        self,
        _cid: str,
        *,
        state: str,
        after_seq: int,
        limit: int,
        exclude_nodes: Any = (),
        only_nodes: Any = None,
    ) -> list[_Run]:
        out = [
            r for r in self.rows if r.seq > after_seq and r.node_id not in exclude_nodes
        ]
        if only_nodes is not None:
            out = [r for r in out if r.node_id in only_nodes]
        return out[:limit]


def _submitter(store: _PagedStore, defer_until: int) -> Any:
    from meshforge.controller.bench.submit import BenchSubmitter, RunOutcome

    class Fake(BenchSubmitter):
        def __init__(self) -> None:
            self._store = store  # type: ignore[assignment]
            self._wake = None
            self.cancel_job = None

        async def _owner_name(self, _user: str | None) -> str | None:
            return None

        async def submit_run(self, _c: Any, run: Any, *, owner_name: Any) -> Any:
            if run.seq <= defer_until:  # a per-run owner limit refuses it
                return RunOutcome(run.id, run.seq, "deferred", detail="policy_ram")
            store.rows = [r for r in store.rows if r.seq != run.seq]
            return RunOutcome(run.id, run.seq, "submitted", job_id=f"job{run.seq}")

    return Fake()


@pytest.mark.asyncio
async def test_more_per_run_deferrals_than_the_cap_no_longer_stall_the_runner() -> None:
    store = _PagedStore([_Run(f"r{i}", i) for i in range(1, 51)])
    sub = _submitter(store, defer_until=40)  # 40 head runs defer, the cap is 32
    record = _Record()
    first = await sub.submit_next(record, 4, max_examined=32)
    assert [o.outcome for o in first.outcomes] == ["deferred"] * 32
    assert first.cursor == 32
    second = await sub.submit_next(record, 4, max_examined=32, start_after=first.cursor)
    assert [o.seq for o in second.outcomes if o.outcome == "submitted"] == [
        41,
        42,
        43,
        44,
    ]
    assert second.cursor == 0  # count reached: the head again next time
    # without the cursor every call re-examines the same 32 deferrals
    again = await sub.submit_next(record, 4, max_examined=32)
    assert not [o for o in again.outcomes if o.outcome == "submitted"]


@pytest.mark.asyncio
async def test_the_cursor_wraps_to_the_head() -> None:
    store = _PagedStore([_Run(f"r{i}", i) for i in range(1, 11)])
    sub = _submitter(store, defer_until=0)
    out = await sub.submit_next(_Record(), 3, max_examined=32, start_after=8)
    assert [o.seq for o in out.outcomes] == [
        9,
        10,
        1,
    ]  # after the cursor, then the head


@pytest.mark.asyncio
async def test_the_runner_carries_the_cursor_across_ticks() -> None:
    seen: list[int] = []

    class Sub:
        async def submit_next(self, _r: Any, count: int, **kw: Any) -> Any:
            seen.append(int(kw.get("start_after", 0)))
            return SubmitBatch(cursor=32 if len(seen) == 1 else 0)

    control = _Control()
    control.nodes, control.load, control.since, control.busy = {"c"}, {}, {}, set()
    runner = BenchRunner(
        store=_Store(),  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        submitter=Sub(),  # type: ignore[arg-type]
        settings=RunnerSettings(),
    )
    for _ in range(3):
        await runner.tick()
    assert seen == [0, 32, 0]


@pytest.mark.asyncio
async def test_a_failed_link_stops_the_batch() -> None:
    """D250 D-1: once a link fails the page is stale (a cancel landed) - no
    further run of it is tried."""
    from meshforge.controller.bench.submit import RunOutcome

    store = _PagedStore([_Run(f"r{i}", i) for i in range(1, 11)])
    sub = _submitter(store, defer_until=0)
    tried: list[int] = []

    async def already(_c: Any, run: Any, *, owner_name: Any) -> Any:
        tried.append(run.seq)
        return RunOutcome(run.id, run.seq, "already", job_id=f"job{run.seq}")

    sub.submit_run = already
    out = await sub.submit_next(_Record(), 4, max_examined=32)
    assert tried == [1] and out.stale and out.cursor == 0
