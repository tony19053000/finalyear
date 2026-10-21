"""V0.14 G2 (D293, D307): the dispatcher plans with the tick's calibration,
names it on the decision row and stores it in the replay.

* no calibration -> ``calibration_version`` None and no ``calibration`` key
  in the replay (as before G2);
* a calibration -> ``calibration_version`` = its version, the candidates'
  terms carry ``detail.calibration``, the replay's block holds the factors
  **cut to the job** (nodes of the fabric, the job's family), and ``plan`` on
  the replay with that block gives the record again (candidates, digest,
  reason key, chosen node) - dispatched, waiting and pinned-with-shadow;
* the calibration really moves the dispatch.
* :class:`CalibrationSource`: ``off`` never reads, ``auto`` re-reads by
  version id and loads factors only when it changed, a pin is checked at
  startup (missing / unreadable -> ``ConfigError``), a failed read keeps the
  version in use and never raises.
* settings: ``calibration`` = auto | off | a version >= 1; clamp low < 1 <
  high; k > 0; window >= 1.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.common.errors import ConfigError
from meshforge.common.ids import new_job_id
from meshforge.common.states import JobState
from meshforge.controller.calibration_store import (
    CalibrationRow,
    CalibrationSource,
    CalibrationStoreError,
    parse_mode,
)
from meshforge.controller.jobs.execution_store import Dispatch, Wait
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner import History, plan
from meshforge.controller.planner.calibration import Calibration, Factor, TermFactors
from meshforge.controller.planner.decision_json import candidates_json
from meshforge.controller.planner.replay import (
    calibration_from_replay,
    canonical_json,
    from_replay,
    shadow_from_replay,
)
from meshforge.controller.settings import JobApiSettings, PlannerSettings

pytestmark = pytest.mark.unit

CAPS = ["exec", "exec-subprocess"]
JOB = {"name": "d", "entrypoint": ["python", "m.py"]}
A, B = "node_01AAAAAAAAAAAAAAAAAAAAAAAA", "node_01BBBBBBBBBBBBBBBBBBBBBBBB"
GONE = "node_01ZZZZZZZZZZZZZZZZZZZZZZZZ"
TREE = "sha256:" + "c" * 64
FP = "f" * 64
WALL = dt.datetime(2026, 10, 21, 9, 30, 0, tzinfo=dt.UTC)


def _live() -> Any:
    return SimpleNamespace(
        state=NodeState.ONLINE,
        rtt_ewma_ms=None,
        session_id="s",
        policy=None,
        policy_slots=None,
        clock=SimpleNamespace(latest=None),
    )


class _Manager:
    def __init__(self, nodes: tuple[str, ...] = (A, B)) -> None:
        self.live = {n: _live() for n in nodes}

    def get(self, node_id: str) -> Any:
        return self.live.get(node_id)

    def get_lingering(self, _node_id: str) -> Any:
        return None


def _planner(nodes: tuple[str, ...] = (A, B)) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(nodes),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(),
        clock=lambda: 1000.0,
        wall_clock=lambda: WALL,
        started_at=0.0,
    )


def _job(node_id: str | None = None, *, bench: bool = False) -> JobRecord:
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest=JOB,
        bundle_digest=None,
        tree_id=TREE,
        placement={"mode": "offload", "node_id": node_id}
        if node_id
        else {"mode": "auto"},
        idempotency_key=f"bench:brun_{node_id}" if bench and node_id else None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=WALL,
        updated_at=WALL,
        scheduling=JobScheduling(input_bytes=0, submitter_node_id=None, fingerprint=FP),
    )


def _fabric() -> FabricRows:
    def row(node_id: str) -> NodeRow:
        return NodeRow(
            node_id=node_id,
            state="online",
            hardware={"capabilities": CAPS},
            hardware_scores=None,
            cpu_slots=2,
            gpu_slots=0,
        )

    fabric = FabricRows(nodes={A: row(A), B: row(B)})
    # A is a little faster on this workload
    fabric.history[FP] = History(
        by_node_s={A: 10.0, B: 12.0}, samples_by_node={A: 5, B: 5}
    )
    return fabric


def _calibration(version: int = 42) -> Calibration:
    return Calibration(
        version=version,
        terms={
            "compute": TermFactors(
                global_=Factor(1.1, 30, 1.2),
                node={A: Factor(1.5, 12, 1.7), GONE: Factor(3.0, 4, 3.0)},
                node_family={
                    A: {"matmul": Factor(1.6, 6, 1.8), "other": Factor(0.5, 9, 0.4)}
                },
            )
        },
        families={FP: "matmul", "e" * 64: "other"},
    )


def _replays_to(record: Any, expect_version: int | None) -> Calibration | None:
    assert record.replay is not None
    assert record.calibration_version == expect_version
    stored = json.loads(canonical_json(record.replay))  # as jsonb hands it back
    request, snapshot, now = from_replay(stored)
    cal = calibration_from_replay(stored)
    assert (None if cal is None else cal.version) == expect_version
    again = plan(request, snapshot, record.policy, now, calibration=cal)
    assert candidates_json(again) == list(record.candidates)
    assert again.snapshot_digest == record.snapshot_digest
    assert again.reason_key == record.reason_key
    assert (None if again.chosen is None else again.chosen.node_id) == (
        record.chosen_node_id
    )
    return cal


def test_uncalibrated_records_name_no_calibration() -> None:
    decision = _planner().decide(_job(), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    assert "calibration" not in (decision.record.replay or {})
    _replays_to(decision.record, None)
    assert decision.node_id == A


def test_a_calibrated_dispatch_names_the_version_and_replays_exactly() -> None:
    planner = _planner()
    planner.calibration = _calibration()
    decision = planner.decide(_job(), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    # A: 10 s x 1.6 (node + family) = 16 s > B: 12 s x 1.1 (global) = 13.2 s
    assert decision.node_id == B
    cal = _replays_to(decision.record, 42)
    assert cal is not None
    compute = cal.terms["compute"]
    assert GONE not in compute.node  # cut to the fabric's nodes
    assert compute.node_family == {A: {"matmul": Factor(1.6, 6, 1.8)}}  # job's family
    assert cal.families == {FP: "matmul"}
    rows = {c["node_id"]: c for c in decision.record.candidates}
    detail = rows[A]["estimate"]["terms"]["compute"]["detail"]["calibration"]
    assert detail == {
        "factor": 1.6,
        "n": 6,
        "key_level": "node_family",
        "version": 42,
        "raw_s": 10.0,
    }
    assert (
        rows[B]["estimate"]["terms"]["compute"]["detail"]["calibration"]["key_level"]
        == "global"
    )


def test_a_waiting_calibrated_decision_replays_too() -> None:
    planner = _planner(nodes=())  # nobody connected: wait
    planner.calibration = _calibration(7)
    decision = planner.decide(_job(), _fabric())
    assert isinstance(decision, Wait) and decision.record is not None
    _replays_to(decision.record, 7)


def test_the_shadow_plan_of_a_campaign_run_uses_the_same_calibration() -> None:
    planner = _planner()
    planner.calibration = _calibration(9)
    decision = planner.decide(_job(A, bench=True), _fabric())
    assert isinstance(decision, Dispatch) and decision.record is not None
    record = decision.record
    assert record.shadow_chosen_node_id == B  # calibrated preference
    stored = json.loads(canonical_json(record.replay or {}))
    cal = _replays_to(record, 9)
    shadow = shadow_from_replay(stored)
    assert shadow is not None
    again = plan(*shadow[:2], record.policy, shadow[2], calibration=cal)
    assert again.chosen is not None and again.chosen.node_id == B
    assert again.chosen.estimate is not None
    assert round(again.chosen.estimate.total_s, 6) == record.shadow_estimate_s


# ------------------------------------------------------------ CalibrationSource


def _row(version: int, calibration: Calibration | None) -> CalibrationRow:
    return CalibrationRow(
        version=version,
        created_at=WALL,
        status="active",
        calibration=calibration,
        fit_inputs={},
        holdout_error_new=None,
        holdout_error_active=None,
        reason=None,
    )


class _Store:
    def __init__(self) -> None:
        self.active: int | None = None
        self.rows: dict[int, CalibrationRow] = {}
        self.version_reads = 0
        self.loads = 0
        self.fail: BaseException | None = None

    async def active_version(self) -> int | None:
        self.version_reads += 1
        if self.fail is not None:
            raise self.fail
        return self.active

    async def load(self, version: int) -> Calibration | None:
        self.loads += 1
        found = self.rows.get(version)
        return None if found is None else found.calibration

    async def row(self, version: int) -> CalibrationRow | None:
        return self.rows.get(version)


def _source(store: _Store, setting: str) -> CalibrationSource:
    return CalibrationSource(store, setting)  # type: ignore[arg-type]


def test_off_never_reads() -> None:
    store = _Store()
    source = _source(store, "off")
    assert asyncio.run(source.refresh()) is None
    assert store.version_reads == 0 and store.loads == 0


def test_auto_loads_factors_only_when_the_active_version_changes() -> None:
    store = _Store()
    store.rows = {1: _row(1, _calibration(1)), 2: _row(2, _calibration(2))}
    source = _source(store, "auto")

    async def ticks() -> list[int | None]:
        seen: list[int | None] = []
        for active in (None, 1, 1, 1, 2, 2, None):
            store.active = active
            cal = await source.refresh()
            seen.append(None if cal is None else cal.version)
        return seen

    assert asyncio.run(ticks()) == [None, 1, 1, 1, 2, 2, None]
    assert store.version_reads == 7
    assert store.loads == 2  # once per new version


def test_a_failed_read_keeps_the_version_in_use_and_never_raises() -> None:
    store = _Store()
    store.rows = {1: _row(1, _calibration(1))}
    source = _source(store, "auto")

    async def go() -> list[int | None]:
        store.active = 1
        out = [(await source.refresh())]
        store.fail = CalibrationStoreError("db down")
        out.append(await source.refresh())
        store.fail = RuntimeError("a bug")
        out.append(await source.refresh())
        store.fail = None
        out.append(await source.refresh())
        return [None if c is None else c.version for c in out]

    assert asyncio.run(go()) == [1, 1, 1, 1]
    assert source.failures == 2


def test_a_pin_is_checked_at_startup_and_used_without_reads() -> None:
    store = _Store()
    with pytest.raises(ConfigError):
        asyncio.run(_source(store, "5").check())
    store.rows = {5: _row(5, None)}  # its factors cannot be read
    with pytest.raises(ConfigError):
        asyncio.run(_source(store, "5").check())
    store.rows = {5: _row(5, _calibration(5))}
    source = _source(store, "5")
    asyncio.run(source.check())
    store.active = 99  # the active version does not matter for a pin
    cal = asyncio.run(source.refresh())
    assert cal is not None and cal.version == 5
    assert store.version_reads == 0


def test_an_unreadable_active_version_is_logged_once_and_not_reread() -> None:
    """Fix round 1 NB3: a bad active version is remembered by id (planned
    uncalibrated), not loaded again every tick; a new active id is loaded."""
    store = _Store()
    store.rows = {3: _row(3, None), 4: _row(4, _calibration(4))}
    store.active = 3
    source = _source(store, "auto")

    async def ticks() -> list[int | None]:
        seen: list[int | None] = []
        for active in (3, 3, 3, 4, 3):
            store.active = active
            cal = await source.refresh()
            seen.append(None if cal is None else cal.version)
        return seen

    assert asyncio.run(ticks()) == [None, None, None, 4, None]
    # 3 once, 4 once; back to 3: still known bad, not re-read
    assert store.loads == 2


def test_a_rejected_version_cannot_be_pinned() -> None:
    """Fix round 1 R1: a rejected row is never used - pinning it is a
    ConfigError at startup; unchecked, the source plans without it."""
    store = _Store()
    rejected = _row(6, _calibration(6))
    store.rows = {6: dataclasses.replace(rejected, status="rejected")}
    with pytest.raises(ConfigError, match="rejected"):
        asyncio.run(_source(store, "6").check())
    unchecked = _source(store, "6")
    assert asyncio.run(unchecked.refresh()) is None


def test_a_superseded_version_can_still_be_pinned() -> None:
    store = _Store()
    old = _row(2, _calibration(2))
    store.rows = {2: dataclasses.replace(old, status="superseded")}
    source = _source(store, "2")
    asyncio.run(source.check())
    assert source.current is not None and source.current.version == 2


def test_parse_mode() -> None:
    assert parse_mode("auto") == ("auto", None)
    assert parse_mode("off") == ("off", None)
    assert parse_mode("12") == ("pinned", 12)


# ------------------------------------------------------------ settings


@pytest.mark.parametrize(
    ("value", "stored"),
    [("auto", "auto"), ("off", "off"), (7, "7"), ("007", "7"), (" 3 ", "3")],
)
def test_calibration_setting_accepts(value: Any, stored: str) -> None:
    assert PlannerSettings(calibration=value).calibration == stored


@pytest.mark.parametrize(
    "value", ["", "AUTO", "on", "x1", "0", 0, -1, "-1", True, 1.5, "1e3", "٣", 2**63]
)
def test_calibration_setting_refuses(value: Any) -> None:
    with pytest.raises(ValidationError):
        PlannerSettings(calibration=value)


@pytest.mark.parametrize(
    "clamp", [(1.0, 5.0), (0.2, 1.0), (0.0, 2.0), (-1.0, 2.0), (0.5, 1e9)]
)
def test_clamp_must_bracket_one(clamp: tuple[float, float]) -> None:
    with pytest.raises(ValidationError):
        PlannerSettings(calibration_clamp=clamp)


def test_other_limits() -> None:
    defaults = PlannerSettings()
    assert defaults.calibration == "auto"
    assert defaults.calibration_window_runs == 200
    assert defaults.calibration_shrink_k == 5.0
    assert defaults.calibration_clamp == (0.2, 5.0)
    assert defaults.calibration_refresh_s == 1800.0
    for bad in (
        {"calibration_shrink_k": 0.0},
        {"calibration_window_runs": 0},
        {"calibration_refresh_s": 1.0},
    ):
        with pytest.raises(ValidationError):
            PlannerSettings(**bad)
