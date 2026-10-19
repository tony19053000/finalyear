"""V0.9 G5 lane B, tester round 2 (delta): pure-logic probes of the fix round.

* ``tc qdisc show`` output variants (rate / limit / slot / jitter / ecn / units);
* the owner guard's failed pre-emption wired through the real activity monitor
  (no storm; but is it retried at all?);
* the submit cursor: wrap-around fairness (no run skipped for ever), reset,
  per-campaign isolation, old callers of ``SubmitOutcomes``.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.agent.conditions.netem import (
    expected_params,
    netem_matches,
    parse_netem_line,
)
from meshforge.agent.owner_activity import ActivityRule, OwnerActivity
from meshforge.agent.owner_guard import OwnerGuard
from meshforge.agent.settings import BenchSettings
from meshforge.common import owner_policy as op
from meshforge.common.bench_conditions import Netem, parse_condition
from meshforge.controller.bench.runner import BenchRunner, RunnerSettings
from meshforge.controller.bench.submit import (
    BenchSubmitter,
    RunOutcome,
    SubmitOutcomes,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ netem output variants


def _expect(spec: str) -> dict[str, float | None]:
    netem = parse_condition(spec)
    assert isinstance(netem, Netem)
    return expected_params(netem)


ROOT = "qdisc netem 8001: root refcnt 2 "


@pytest.mark.parametrize(
    ("spec", "line", "ours"),
    [
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100ms", True),
        (
            "netem:delay=100ms",
            "qdisc netem 8001: dev eth0 root refcnt 2 limit 1000 delay 100ms",
            True,
        ),
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100.0ms", True),
        (
            "netem:delay=100ms",
            ROOT + "limit 5000 delay 100ms",
            True,
        ),  # limit is not ours to compare
        ("netem:delay=500ms", ROOT + "limit 1000 delay 0.5s", True),
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100ms 20ms", False),  # jitter
        (
            "netem:delay=100ms",
            ROOT + "limit 1000 delay 100ms 20ms 25%",
            False,
        ),  # correlation
        (
            "netem:delay=100ms",
            ROOT + "limit 1000 delay 100ms distribution normal",
            False,
        ),
        ("netem:loss=1%", ROOT + "limit 1000 loss 1%", True),
        ("netem:loss=1%", ROOT + "limit 1000 loss 1% 25%", False),
        ("netem:loss=1%", ROOT + "limit 1000 loss 1% ecn", False),  # ecn: never ours
        ("netem:loss=1%", ROOT + "limit 1000 loss random 1%", False),
        ("netem:loss=1%", ROOT + "limit 1000 loss state 1% 2%", False),
        ("netem:rate=10mbit", ROOT + "limit 1000 rate 10Mbit", True),
        ("netem:rate=10mbit", ROOT + "limit 1000 rate 10000Kbit", True),
        ("netem:rate=10mbit", ROOT + "limit 1000 rate 1250000bps", True),
        ("netem:rate=10mbit", ROOT + "limit 1000 rate 10Mbit packetoverhead 4", False),
        ("netem:rate=10mbit", ROOT + "limit 1000 rate 10Mbit cellsize 48", False),
        ("netem:rate=10mbit", ROOT + "limit 1000 rate 20Mbit", False),
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100ms slot 10ms 20ms", False),
        (
            "netem:delay=100ms",
            ROOT + "limit 1000 delay 100ms slot 10ms 20ms packets 5",
            False,
        ),
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100ms reorder 25% 50%", False),
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100ms corrupt 1%", False),
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100ms duplicate 1%", False),
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100ms gap 5", False),
        ("netem:delay=100ms", ROOT + "limit 1000 delay 100ms seed 123", True),
        ("netem:delay=100ms", "qdisc fq_codel 0: root refcnt 2 limit 10240p", False),
        ("netem:delay=100ms", "", False),
        ("netem:delay=100ms", ROOT, False),  # a bare netem: no delay is not 100 ms
    ],
)
def test_tc_output_variants(spec: str, line: str, ours: bool) -> None:
    assert netem_matches(line, _expect(spec)) is ours, line


def test_parse_is_total_over_word_soup() -> None:
    import itertools

    words = [
        "netem",
        "root",
        "delay",
        "loss",
        "rate",
        "10ms",
        "1%",
        "limit",
        "%",
        "ecn",
    ]
    for n in range(1, 5):
        for combo in itertools.product(words, repeat=n):
            parse_netem_line(" ".join(combo))  # never raises


# ------------------------------------------------------------ owner guard through the monitor


class _Pol:
    def __init__(self) -> None:
        self.policy = dataclasses.replace(op.OPEN_POLICY)


class _Busy:
    def __init__(self) -> None:
        self.value: float | None = 100.0

    def sample(self) -> float | None:
        return self.value


async def _wired(preempt: Any) -> tuple[OwnerActivity, OwnerGuard, _Busy]:
    cpu = _Busy()
    clock = [0.0]
    activity = OwnerActivity(
        BenchSettings(sustain_s=0.0, clear_after_s=3600.0),
        idle=None,
        cpu=cpu,  # type: ignore[arg-type]
        clock=lambda: clock[0],
    )
    guard = OwnerGuard(policy=_Pol(), activity=activity, preempt=preempt)  # type: ignore[arg-type]
    activity.on_change = guard.on_activity
    return activity, guard, cpu


@pytest.mark.asyncio
async def test_a_persistently_failing_preemption_does_not_storm() -> None:
    calls: list[str] = []

    async def preempt(reason: str) -> int:
        calls.append(reason)
        raise OSError("cannot stop")

    activity, _guard, _cpu = await _wired(preempt)
    with pytest.raises(OSError, match="cannot stop"):
        await activity.step()  # the flip; run() would log it and carry on
    for _ in range(200):  # steady state: the owner stays active
        await activity.step()
    assert len(calls) <= 2, f"{len(calls)} pre-emption attempts in 200 steady polls"


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="T2-1: a pre-emption that raised on the flip is retried only at the "
    "next flip / policy-file change, not at the next poll, while the owner stays active",
)
async def test_a_failed_preemption_is_retried_while_the_owner_stays_active() -> None:
    calls: list[str] = []

    async def preempt(reason: str) -> int:
        calls.append(reason)
        if len(calls) == 1:
            raise OSError("cannot stop")
        return 1

    activity, _guard, _cpu = await _wired(preempt)
    with pytest.raises(OSError, match="cannot stop"):
        await activity.step()
    for _ in range(5):
        await activity.step()
    assert len(calls) >= 2, "the running benchmark attempt keeps running"


# ------------------------------------------------------------ cursor / SubmitOutcomes


@dataclasses.dataclass
class _Run:
    id: str
    seq: int
    node_id: str = "n1"


class _Rec:
    id = "camp_x"
    created_by = None


class _Paged:
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


def _sub(store: _Paged, refuse: set[int]) -> Any:
    class Fake(BenchSubmitter):
        def __init__(self) -> None:
            self._store = store  # type: ignore[assignment]
            self._wake = None
            self.cancel_job = None
            self.tried: list[int] = []

        async def _owner_name(self, _u: str | None) -> str | None:
            return None

        async def submit_run(self, _c: Any, run: Any, *, owner_name: Any) -> Any:
            self.tried.append(run.seq)
            if run.seq in refuse:
                return RunOutcome(run.id, run.seq, "deferred", detail="policy_ram")
            store.rows = [r for r in store.rows if r.seq != run.seq]
            return RunOutcome(run.id, run.seq, "submitted", job_id=f"j{run.seq}")

    return Fake()


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", [1, 2, 3, 5, 7])
async def test_no_run_is_skipped_for_ever_under_a_cursor(cap: int) -> None:
    """Every run the policy lets through is submitted within a bounded number of
    ticks however small the examine cap and however the refusals are spread."""
    n = 23
    refuse = {3, 4, 5, 9, 10, 11, 12, 20}
    store = _Paged([_Run(f"r{i}", i) for i in range(1, n + 1)])
    sub = _sub(store, refuse)
    cursor = 0
    for _tick in range(30 * n):
        kw = {"start_after": cursor} if cursor else {}
        out = await sub.submit_next(_Rec(), 1, max_examined=cap, **kw)
        cursor = out.cursor
        if {r.seq for r in store.rows} <= refuse:
            break
    assert {r.seq for r in store.rows} == refuse, "a submittable run starved"


@pytest.mark.asyncio
async def test_cursor_past_the_end_and_beyond_wraps_cleanly() -> None:
    store = _Paged([_Run(f"r{i}", i) for i in range(1, 6)])
    sub = _sub(store, set())
    for cursor in (5, 6, 999):
        out = await sub.submit_next(_Rec(), 2, max_examined=32, start_after=cursor)
        assert [o.seq for o in out if o.outcome == "submitted"], cursor
        assert out.cursor == 0 and not out.stale


@pytest.mark.asyncio
async def test_all_refused_returns_cursor_zero_not_a_dead_cursor() -> None:
    store = _Paged([_Run(f"r{i}", i) for i in range(1, 6)])
    sub = _sub(store, {1, 2, 3, 4, 5})
    out = await sub.submit_next(_Rec(), 2, max_examined=32)
    assert out.cursor == 0  # exhausted, not capped: start at the head next time
    capped = await sub.submit_next(_Rec(), 2, max_examined=3)
    assert capped.cursor == 3


@pytest.mark.asyncio
async def test_a_cursor_pass_never_examines_a_run_twice() -> None:
    store = _Paged([_Run(f"r{i}", i) for i in range(1, 11)])
    sub = _sub(store, set(range(1, 11)))
    await sub.submit_next(_Rec(), 3, max_examined=32, start_after=4)
    assert sorted(sub.tried) == list(range(1, 11)) and len(sub.tried) == 10


def test_submit_outcomes_is_a_plain_list_for_older_callers() -> None:
    out = SubmitOutcomes([RunOutcome("r", 1, "submitted", job_id="j")])
    assert isinstance(out, list) and len(out) == 1 and out[0].job_id == "j"
    assert out == [RunOutcome("r", 1, "submitted", job_id="j")]
    assert [o.outcome for o in out[:1]] == ["submitted"]
    assert out.cursor == 0 and out.stale is False
    assert json.dumps([o.outcome for o in out]) == '["submitted"]'
    assert SubmitOutcomes([]) == []


class _Control:
    def __init__(self, campaigns: list[Any]) -> None:
        self.campaigns = campaigns

    async def runner_campaigns(self) -> list[Any]:
        return self.campaigns

    async def user_busy_nodes(self) -> set[str]:
        return set()

    async def planned_nodes(self, _cid: str) -> set[str]:
        return {"n1"}

    async def node_bench_load(self, _n: Any) -> dict[str, int]:
        return {}

    async def seconds_since_last_end(self, _c: str, _n: Any) -> dict[str, float]:
        return {}

    async def stop_for_budget(self, _c: str, _d: str) -> int:
        return 0


@dataclasses.dataclass
class _Camp:
    id: str
    spec_yaml: str = (
        "name: x\nseed: 1\nfamilies: [{family: cpu.primes}]\n"
        "repetitions: 3\nbudget: {max_runs: 10}\n"
    )
    max_concurrent_per_node: int = 1
    cooldown_s: float = 0.0
    max_infra_retries: int = 1
    elapsed_s: float = 0.0
    created_by: str | None = None


class _Store:
    async def sync(self, _c: str | None = None) -> int:
        return 0

    async def get_campaign(self, cid: str) -> Any:
        rec = type("R", (), {})()
        rec.id, rec.state, rec.created_by = cid, "active", None
        return rec

    async def list_campaigns(self, limit: int = 50) -> list[Any]:
        return []


def _runner(camps: list[Any], sub: Any) -> BenchRunner:
    return BenchRunner(
        store=_Store(),  # type: ignore[arg-type]
        control=_Control(camps),  # type: ignore[arg-type]
        submitter=sub,
        settings=RunnerSettings(max_submit_per_tick=4),
    )


@pytest.mark.asyncio
async def test_the_cursor_is_per_campaign_and_an_old_list_submitter_works() -> None:
    seen: list[tuple[str, int]] = []
    plan = {"camp_a": [32, 0], "camp_b": [0, 0]}

    class Sub:
        async def submit_next(self, rec: Any, count: int, **kw: Any) -> Any:
            seen.append((rec.id, int(kw.get("start_after", 0))))
            nxt = plan[rec.id].pop(0) if plan[rec.id] else 0
            return SubmitOutcomes([], cursor=nxt)

    runner = _runner([_Camp("camp_a"), _Camp("camp_b")], Sub())
    await runner.tick()
    await runner.tick()
    # camp_a's cursor never leaks into camp_b
    assert seen == [("camp_a", 0), ("camp_b", 0), ("camp_a", 32), ("camp_b", 0)]

    class Old:  # an older submitter returning a plain list
        async def submit_next(self, rec: Any, count: int, **kw: Any) -> list[Any]:
            return []

    old = _runner([_Camp("camp_a")], Old())
    await old.tick()
    await old.tick()


@pytest.mark.asyncio
async def test_the_cursor_resets_when_the_campaign_leaves_and_returns() -> None:
    seen: list[int] = []

    class Sub:
        async def submit_next(self, _r: Any, count: int, **kw: Any) -> Any:
            seen.append(int(kw.get("start_after", 0)))
            return SubmitOutcomes([], cursor=0)  # not capped: the head next time

    camps = [_Camp("camp_a")]
    runner = _runner(camps, Sub())
    runner._cursors["camp_a"] = 17
    await runner.tick()
    await runner.tick()
    assert seen == [17, 0]


def test_activity_rule_still_exported_for_the_guard() -> None:
    assert ActivityRule(BenchSettings()).active is False
