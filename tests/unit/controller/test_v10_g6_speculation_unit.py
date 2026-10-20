"""V0.10 G6 (D266/D275): the controller side of speculation without a database -
config validation, the planner-rate fallback read from a stored decision, a
node's chunk time, and the queue policy taking ``speculative_max_frac`` from
``[controller.split]`` (0 when the job says ``split.speculative: false``)."""

from __future__ import annotations

import datetime as dt
import math
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from meshforge.common.ids import new_job_id, new_node_id
from meshforge.common.manifest import SplitSpec
from meshforge.common.states import JobState
from meshforge.controller.jobs.execution_store import DispatchLanes
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.queue_reads import FabricRows, NodeRow
from meshforge.controller.jobs.store import JobRecord, SplitRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.settings import (
    JobApiSettings,
    PlannerSettings,
    SplitSettings,
)
from meshforge.controller.split.speculation import (
    HISTORY,
    PLANNER,
    SiteTime,
    SpeculationConfig,
    planner_rates,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ config


def test_settings_defaults_match_d266() -> None:
    s = SplitSettings()
    assert s.speculative_max_frac == 0.10
    assert s.speculation_factor == 1.5


@pytest.mark.parametrize(
    "values",
    [
        {"speculative_max_frac": -0.01},
        {"speculative_max_frac": 1.01},
        {"speculation_factor": 0.99},
        {"speculation_factor": 101.0},
    ],
)
def test_settings_refuse_out_of_range(values: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        SplitSettings(**values)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"factor": 0.5},
        {"factor": math.nan},
        {"factor": math.inf},
        {"overhead_s": -1.0},
        {"overhead_s": math.inf},
    ],
)
def test_config_refuses_bad_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        SpeculationConfig(**kwargs)


# ------------------------------------------------------------------ estimates


def test_site_time_scales_per_item_plus_fixed() -> None:
    t = SiteTime(per_item_s=0.5, fixed_s=1.5, source=PLANNER)
    assert t.seconds(10) == 6.5
    assert t.seconds(0) == 2.0  # at least one item
    h = SiteTime(per_item_s=0.25, fixed_s=0.0, source=HISTORY, samples=3)
    assert h.seconds(4) == 1.0


def test_planner_rates_reads_single_node_rows_only() -> None:
    rows: list[Any] = [
        {"node_id": "a", "mode": "local", "rate_items_s": 4.0},
        {"node_id": "b", "mode": "offload", "rate_items_s": 2},
        {"node_id": "s", "mode": "split", "rate_items_s": 99.0},  # a set row
        {"node_id": "c", "mode": "offload", "rate_items_s": 0},  # not a rate
        {"node_id": "d", "mode": "offload", "rate_items_s": None},  # rejected
        {"node_id": "e", "mode": "offload", "rate_items_s": True},  # not a number
        {"node_id": "f", "mode": "offload", "rate_items_s": float("inf")},
        {"node_id": 7, "mode": "offload", "rate_items_s": 1.0},
        "junk",
        {"node_id": "a", "mode": "offload", "rate_items_s": 1.0},  # first wins
    ]
    assert planner_rates(rows) == {"a": 4.0, "b": 2.0}
    assert planner_rates(None) == {}
    assert planner_rates({"a": 1}) == {}


# ------------------------------------------------------------------ dispatch


class _Manager:
    def __init__(self, node_id: str) -> None:
        self.live = {
            node_id: SimpleNamespace(
                state=NodeState.ONLINE,
                rtt_ewma_ms=None,
                session_id="s",
                policy=None,
                policy_slots=None,
                clock=SimpleNamespace(latest=None),
            )
        }

    def get(self, node_id: str) -> Any:
        return self.live.get(node_id)

    def get_lingering(self, _node_id: str) -> Any:
        return None


def _planner(node_id: str, split: SplitSettings) -> DispatchPlanner:
    return DispatchPlanner(
        manager=_Manager(node_id),  # type: ignore[arg-type]
        settings=JobApiSettings(),
        planner=PlannerSettings(),
        clock=lambda: 1000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=1000.0,
        split=split,
    )


def _job(split: dict[str, Any]) -> JobRecord:
    now = dt.datetime.now(dt.UTC)
    return JobRecord(
        id=str(new_job_id()),
        name="demo",
        state=JobState.QUEUED,
        manifest={"name": "d", "entrypoint": ["python", "m.py"], "split": split},
        bundle_digest=None,
        tree_id="sha256:" + "a" * 64,
        placement={"mode": "auto", "node_id": None},
        idempotency_key=None,
        request_hash="h" * 64,
        attempt_epoch=0,
        failure_type=None,
        failure_detail=None,
        created_at=now,
        updated_at=now,
        split=SplitRecord(spec=split, items_digest="sha256:" + "b" * 64, item_count=8),
    )


def _fabric(node_id: str) -> FabricRows:
    return FabricRows(
        nodes={
            node_id: NodeRow(
                node_id=node_id,
                state="online",
                hardware={"capabilities": ["exec", "exec-subprocess", "exec-split"]},
                hardware_scores=None,
                cpu_slots=1,
                gpu_slots=0,
            )
        }
    )


def test_queue_policy_takes_the_configured_fraction() -> None:
    node = str(new_node_id())
    split = {"over": {"glob": "data/*"}}
    got = _planner(node, SplitSettings(speculative_max_frac=0.25)).decide(
        _job(split), _fabric(node)
    )
    assert isinstance(got, DispatchLanes), got
    assert got.queue_policy.speculative_max_frac == 0.25
    # the stored decision carries the node's predicted rate (the fallback)
    assert got.record is not None
    assert node in planner_rates([dict(c) for c in got.record.candidates])


def test_queue_policy_is_zero_when_the_job_says_no() -> None:
    node = str(new_node_id())
    planner = _planner(node, SplitSettings(speculative_max_frac=0.5))
    spec = SplitSpec.model_validate({"over": {"glob": "data/*"}, "speculative": False})
    policy = planner.queue_policy(_job({"over": {"glob": "data/*"}}), spec)
    assert policy.speculative_max_frac == 0.0
