"""V0.9 G4 lane B fix round 1: queue fillers / cache warm-ups are tagged in their
records so label and planner-training filters can drop them.

They are system-made background jobs of a campaign run (no campaign identity);
their record's ``condition_measured`` is ``{"bench_role", "run_id"}`` and
``condition`` stays null. Anything else (a user job, a run job, an odd key)
is untouched.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.ids import IdKind, new_id
from meshforge.controller.bench.conditions import (
    filler_key,
    pre_job_role,
    warmup_key,
)
from meshforge.controller.telemetry.conditions import BENCH_ROLES, condition_outcome
from meshforge.controller.telemetry.record import RecordSource, build_record

pytestmark = pytest.mark.unit

RUN = str(new_id(IdKind.BENCH_RUN))
T0 = dt.datetime(2026, 10, 18, 9, 0, tzinfo=dt.UTC)


def test_pre_job_role_reads_filler_and_warmup_keys() -> None:
    assert pre_job_role(filler_key(RUN, 1)) == ("filler", RUN)
    assert pre_job_role(filler_key(RUN, 12)) == ("filler", RUN)
    assert pre_job_role(warmup_key(RUN)) == ("warmup", RUN)
    assert {r for r, _ in (("filler", 0), ("warmup", 0))} == BENCH_ROLES


@pytest.mark.parametrize(
    "key",
    [
        None,
        "",
        f"bench:{RUN}",  # the run job itself
        f"bench:{RUN}:filler",
        f"bench:{RUN}:filler:x",
        f"bench:{RUN}:filler:1:2",
        f"bench:{RUN}:warmup:1",
        f"bench:{RUN}:other",
        "bench:not_a_run:filler:1",
        "bench:not_a_run:warmup",
        f"user:{RUN}:filler:1",
        f"BENCH:{RUN}:warmup",
    ],
)
def test_pre_job_role_ignores_every_other_key(key: str | None) -> None:
    assert pre_job_role(key) is None


@pytest.mark.parametrize("role", sorted(BENCH_ROLES))
def test_a_filler_or_warmup_record_is_tagged_and_never_failed(role: str) -> None:
    out = condition_outcome(
        campaign={"bench_role": role, "run_id": RUN}, workload=None, state="done"
    )
    assert out.condition is None
    assert out.measured == {"bench_role": role, "run_id": RUN}
    assert out.failed is False


@pytest.mark.parametrize(
    "identity",
    [
        {"bench_role": "other", "run_id": RUN},
        {"bench_role": True},
        {"bench_role": ["filler"]},
        {},
    ],
)
def test_an_unknown_role_is_no_tag(identity: dict[str, Any]) -> None:
    out = condition_outcome(campaign=identity, workload=None, state="done")
    assert out.condition is None and out.measured is None


def test_a_hostile_run_id_is_bounded_or_dropped() -> None:
    long = condition_outcome(
        campaign={"bench_role": "filler", "run_id": "x" * 500},
        workload=None,
        state="done",
    )
    assert long.measured is not None and len(long.measured["run_id"]) == 64
    odd = condition_outcome(
        campaign={"bench_role": "warmup", "run_id": 5}, workload=None, state="done"
    )
    assert odd.measured == {"bench_role": "warmup", "run_id": None}


def test_a_campaign_run_keeps_its_condition_even_with_a_role() -> None:
    out = condition_outcome(
        campaign={"campaign_id": "c", "condition": "none", "bench_role": "filler"},
        workload=None,
        state="done",
    )
    assert out.condition == "none"
    assert out.measured is not None and "bench_role" not in out.measured


def test_the_built_record_carries_the_tag_and_no_campaign() -> None:
    record = build_record(
        RecordSource(
            attempt_id="att_1",
            job_id="job_1",
            attempt_no=1,
            node_id="node_b",
            state="done",
            created_at=T0,
            ended_at=T0 + dt.timedelta(seconds=3),
            exit_code=0,
            placement={"node_id": "node_b"},
            submitter_node_id="node_a",
            campaign={"bench_role": "filler", "run_id": RUN},
            workload={"family": "cpu.sort", "params": {"n": 10}, "status": "ok"},
        )
    )
    assert record["campaign_id"] is None
    assert record["group_id"] is None and record["repetition"] is None
    assert record["condition"] is None
    assert record["condition_measured"] == {"bench_role": "filler", "run_id": RUN}
    assert record["failure_class"] == "ok"
