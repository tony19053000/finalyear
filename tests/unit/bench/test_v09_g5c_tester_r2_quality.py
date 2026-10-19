"""V0.9 G5c tester r2: hostile records for the one-bucket invariant (D257 T-3)
and the exact criterion-1 fraction (T-1). Derived from the rule "every
non-bench_role record is in exactly one summary bucket", not from `add`."""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

import pytest

from meshforge.bench.quality import (
    ROW_FIELDS,
    FailureCounts,
    QualityInput,
    build_quality,
    crit1_infra_ok,
    infra_failure_rate,
)

pytestmark = pytest.mark.unit

_CLASSES: list[Any] = [
    "ok",
    "workload",
    "infrastructure",
    "condition_failed",
    "unavailable",
    "",
    "OK",
    "Infrastructure",
    "something_new",
    "x" * 500,
    "ünï-💥",
    None,
    7,
]
_TYPES: list[Any] = [None, "owner_preempted", "node_lost", "", "OWNER_PREEMPTED", 5]
_STATUS: list[Any] = [None, "ok", "check_failed", "", "weird", "ünï"]
_VERIFIED: list[Any] = [None, "true", "false", "FALSE", True, False, "", 0]
_FAMILY: list[Any] = [None, "", "cpu.sort", "ünï"]
_COND: list[Any] = [None, "", "none", "thermal"]
_ROLES: list[Any] = [None, None, None, None, "filler", "warmup", "", "odd"]


def _hostile(rng: random.Random, i: int) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(ROW_FIELDS)
    row.update(
        attempt_id=f"a{i}",
        attempt_no=1,
        attempt_created_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        group_id="g",
        config_id="c",
        repetition=rng.randint(1, 2),
        failure_class=rng.choice(_CLASSES),
        failure_type=rng.choice(_TYPES),
        workload_family=rng.choice(_FAMILY),
        condition=rng.choice(_COND),
        workload_status=rng.choice(_STATUS),
        condition_verified=rng.choice(_VERIFIED),
        bench_role=rng.choice(_ROLES),
        total_ms=rng.choice([None, 1, 5]),
        label_row=rng.choice([True, False]),
    )
    return row


@pytest.mark.parametrize("seed", range(40))
def test_tester_r2_hostile_records_each_in_exactly_one_bucket(seed: int) -> None:
    rng = random.Random(seed * 7919)  # noqa: S311
    rows = [_hostile(rng, i) for i in range(rng.randint(0, 250))]
    data = QualityInput()
    for r in rows:
        data.add(r)  # must never raise
    mine = [r for r in rows if r["bench_role"] is None]
    s = build_quality(
        "c",
        campaign_state="done",
        runs=[],
        data=data,
        failures=FailureCounts(
            records=len(mine),
            infra=sum(
                1
                for r in mine
                if r["failure_class"] == "infrastructure"
                and r["failure_type"] != "owner_preempted"
            ),
            preempted=sum(1 for r in mine if r["failure_type"] == "owner_preempted"),
        ),
    ).summary
    assert s.accounted == s.records == len(mine)
    # the precedence is fixed: owner_preempted wins over every class
    assert s.owner_preempted == sum(
        1 for r in mine if r["failure_type"] == "owner_preempted"
    )
    # no bucket is negative / no key is empty-collapsed away
    for bucket in (s.rejected_ok, s.other_failures, s.workload_failures):
        assert all(v > 0 for v in bucket.values())
    # fillers / odd roles are outside the buckets
    assert sum(s.bench_role_records.values()) == len(rows) - len(mine)


def test_tester_r2_owner_preempted_with_any_class_is_counted_once() -> None:
    for cls in ["ok", "workload", "infrastructure", "condition_failed", "zzz", None]:
        data = QualityInput()
        data.add(
            {
                "failure_class": cls,
                "failure_type": "owner_preempted",
                "label_row": cls == "ok",
                "bench_role": None,
            }
        )
        assert (data.preempted, data.infra, data.measured) == (1, 0, 0), cls
        assert not data.rejected_ok and not data.other_failures
        assert not data.condition_failures and not data.workload_failures
        assert data.ok_rows == []


@pytest.mark.parametrize(
    ("infra", "records", "preempted", "ok"),
    [
        (1999, 100000, 0, True),
        (2000, 100000, 0, False),
        (2001, 100000, 0, False),
        (10**15 // 50 - 1, 10**15, 0, True),
        (10**15 // 50, 10**15, 0, False),
        (0, 1, 0, True),
        (1, 1, 1, False),  # everything pre-empted: nothing counted
        (0, 5, 5, False),
        (999, 50000, 0, True),  # 1.998 %
        (1000, 50000, 0, False),  # exactly 2 %
    ],
)
def test_tester_r2_crit1_boundaries(
    infra: int, records: int, preempted: int, ok: bool
) -> None:
    c = FailureCounts(records=records, infra=infra, preempted=preempted)
    assert crit1_infra_ok(c) is ok


def test_tester_r2_display_rate_may_show_two_percent_while_met() -> None:
    c = FailureCounts(records=100000, infra=1999, preempted=0)
    assert infra_failure_rate(c) == 0.02 and crit1_infra_ok(c) is True
