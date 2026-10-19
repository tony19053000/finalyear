"""V0.9 G5c tester r1 (item 0.6): a campaign resumed after its caches were
pruned is driven from the head with a freshly parsed budget; a max_hours budget
still stops it; the caches stay bounded over a long history."""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.controller.bench.runner import BenchRunner, RunnerSettings
from meshforge.controller.bench.submit import SubmitBatch

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

SPEC = (
    "name: x\nseed: 1\nfamilies: [{family: cpu.primes}]\nrepetitions: 3\n"
    "budget: {max_runs: 10, max_hours: 1}\n"
)


@dataclasses.dataclass
class Camp:
    id: str
    spec_yaml: str = SPEC
    max_concurrent_per_node: int = 1
    cooldown_s: float = 0.0
    max_infra_retries: int = 2
    elapsed_s: float = 0.0
    created_by: str | None = None


@dataclasses.dataclass
class Rec:
    id: str
    state: str = "active"
    started_at: object = "t"
    finished_reason: str | None = None
    counts: dict[str, int] = dataclasses.field(default_factory=dict)
    max_concurrent_per_node: int = 1
    created_by: str | None = None


class Control:
    def __init__(self) -> None:
        self.campaigns: list[Camp] = []
        self.stopped: list[str] = []

    async def runner_campaigns(self) -> list[Camp]:
        return self.campaigns

    async def user_busy_nodes(self) -> set[str]:
        return set()

    async def planned_nodes(self, _cid: str) -> set[str]:
        return {"n"}

    async def node_slots(self, _cid: str, _nodes: Any) -> tuple[dict[str, int], dict]:
        return {}, {}

    async def stop_for_budget(self, cid: str, _d: str) -> int:
        self.stopped.append(cid)
        return 0


class Store:
    async def sync(self, _cid: str | None = None) -> int:
        return 0

    async def get_campaign(self, cid: str) -> Rec:
        return Rec(cid)

    async def progress_campaigns(self, also: Any = ()) -> list[Rec]:
        return []


class Submitter:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def submit_next(self, _r: Any, _c: int, **kw: Any) -> SubmitBatch:
        self.calls.append(kw)
        return SubmitBatch(cursor=7)


def make() -> tuple[BenchRunner, Control, Submitter]:
    control, sub = Control(), Submitter()
    runner = BenchRunner(
        store=Store(),  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        submitter=sub,  # type: ignore[arg-type]
        settings=RunnerSettings(),
    )
    return runner, control, sub


async def test_resumed_campaign_restarts_at_the_head_not_a_stale_cursor() -> None:
    runner, control, sub = make()
    control.campaigns = [Camp("c1")]
    await runner.tick()
    await runner.tick()
    assert sub.calls[1].get("start_after") == 7  # cursor kept while active
    control.campaigns = []  # paused
    await runner.tick()
    control.campaigns = [Camp("c1")]  # resumed
    await runner.tick()
    assert "start_after" not in sub.calls[2]


async def test_resumed_campaign_past_its_hours_budget_is_stopped_not_submitted() -> (
    None
):
    runner, control, sub = make()
    control.campaigns = [Camp("c1")]
    await runner.tick()
    control.campaigns = []
    await runner.tick()
    control.campaigns = [Camp("c1", elapsed_s=3601.0)]
    report = await runner.tick()
    assert report.budget_stopped == ["c1"] and control.stopped == ["c1"]
    assert len(sub.calls) == 1  # nothing submitted on resume


async def test_caches_stay_bounded_over_a_long_history() -> None:
    runner, control, _sub = make()
    for i in range(300):
        control.campaigns = [Camp(f"c{i}")]
        await runner.tick()
    assert len(runner._budgets) <= 1 and len(runner._cursors) <= 1
