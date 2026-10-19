"""V0.9 G5c item 0.6 (D252.2): the runner's caches hold only live campaigns.

``_budgets`` / ``_cursors`` keep the campaigns the runner drives this tick;
``_last_progress`` the ones still reported. Fakes (the SQL side is
``tests/component/controller/test_v09_g5c_progress_campaigns.py``).
"""

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
    "budget: {max_runs: 10}\n"
)


@dataclasses.dataclass
class _Camp:
    id: str
    spec_yaml: str = SPEC
    max_concurrent_per_node: int = 1
    cooldown_s: float = 0.0
    max_infra_retries: int = 2
    elapsed_s: float = 0.0
    created_by: str | None = None


@dataclasses.dataclass
class _Rec:
    id: str
    state: str = "active"
    started_at: object = "t"
    finished_reason: str | None = None
    counts: dict[str, int] = dataclasses.field(default_factory=lambda: {"planned": 1})
    max_concurrent_per_node: int = 1
    created_by: str | None = None


class _Control:
    def __init__(self) -> None:
        self.campaigns: list[_Camp] = []

    async def runner_campaigns(self) -> list[_Camp]:
        return self.campaigns

    async def user_busy_nodes(self) -> set[str]:
        return set()

    async def planned_nodes(self, _cid: str) -> set[str]:
        return {"n"}

    async def node_slots(self, _cid: str, _nodes: Any) -> tuple[dict[str, int], dict]:
        return {}, {}

    async def stop_for_budget(self, _cid: str, _d: str) -> int:
        return 0

    async def status(self, _cid: str, _counts: Any, *, per_node: int = 1) -> Any:
        return dataclasses.make_dataclass(
            "S", ["done_percent", "eta_s", "infra_failure_rate"]
        )(0.0, None, None)


class _Store:
    def __init__(self) -> None:
        self.records: list[_Rec] = []
        self.asked_also: list[set[str]] = []

    async def sync(self, _cid: str | None = None) -> int:
        return 0

    async def get_campaign(self, cid: str) -> _Rec:
        return _Rec(cid)

    async def progress_campaigns(self, also: Any = ()) -> list[_Rec]:
        self.asked_also.append(set(also))
        return self.records


class _Submitter:
    async def submit_next(self, _r: Any, _c: int, **_kw: Any) -> SubmitBatch:
        return SubmitBatch(cursor=7)  # as if the examine cap was hit


def _runner() -> tuple[BenchRunner, _Control, _Store, list[dict[str, Any]]]:
    control, store, events = _Control(), _Store(), []
    runner = BenchRunner(
        store=store,  # type: ignore[arg-type]
        control=control,  # type: ignore[arg-type]
        submitter=_Submitter(),  # type: ignore[arg-type]
        settings=RunnerSettings(),
        publish=events.append,
    )
    return runner, control, store, events


async def test_budget_and_cursor_caches_drop_campaigns_no_longer_driven() -> None:
    runner, control, _store, _ = _runner()
    control.campaigns = [_Camp("c1"), _Camp("c2")]
    await runner.tick()
    assert set(runner._budgets) == {"c1", "c2"}
    assert set(runner._cursors) == {"c1", "c2"}
    control.campaigns = [_Camp("c2")]  # c1 paused / done / cancelled
    await runner.tick()
    assert set(runner._budgets) == {"c2"} and set(runner._cursors) == {"c2"}
    control.campaigns = []
    await runner.tick()
    assert runner._budgets == {} and runner._cursors == {}


async def test_progress_asks_for_open_campaigns_plus_the_ones_it_reported() -> None:
    runner, _control, store, events = _runner()
    store.records = [_Rec("c1")]
    await runner.tick()
    assert store.asked_also[-1] == set()
    assert [e["campaign_id"] for e in events] == ["c1"]
    store.records = [_Rec("c1", state="done", finished_reason="complete")]
    runner._last_progress["c1"] -= 5.0  # a second later
    await runner.tick()
    assert store.asked_also[-1] == {"c1"}
    assert events[-1]["state"] == "done"
    assert runner._last_progress == {}  # one final event, then forgotten


async def test_a_campaign_that_vanished_is_forgotten() -> None:
    runner, _control, store, _events = _runner()
    store.records = [_Rec("c1")]
    await runner.tick()
    assert "c1" in runner._last_progress
    store.records = []  # deleted (or the database lost it)
    await runner.tick()
    assert runner._last_progress == {}
