"""V0.15 G5a (D371): the regret part of the model set's activation gate.

* :func:`ml_regret_check` - the candidate set vs **what serves now**, paired
  on the slice's groups: ``passed`` (also exactly at x 1.05), ``rejected``,
  ``skipped`` below 10 common groups;
* mutation guard: the comparison is against the current serving set, not
  against raw (a candidate equal to raw passes a raw comparison but is
  rejected against a better serving set);
* :func:`regret.apply` - the gate document's ``regret`` block, and a rejection
  turns the set ``rejected`` with rule ``regret_worse``; ``pending_g4`` is
  never written (old rows keep it: the API's ``gate`` is a free document);
* the slice is the error gate's own D329 split: :func:`held_out_decisions`
  over the dataset rows == :func:`holdout_mask` over the table.
"""

from __future__ import annotations

import math
from typing import Any

import pytest
from mf_eval_world import fabric, group, records
from mf_ml_stub import Rule, stub_ml

pytest.importorskip("pyarrow", reason="needs the controller extra")

from meshforge.controller.ml import regret
from meshforge.controller.ml.holdout import held_out_decisions, rows_of_dataset
from meshforge.controller.planner_eval.regret_gate import (
    MIN_GROUPS,
    RegretCheck,
    ml_regret_check,
)

pytestmark = pytest.mark.unit

HISTORY = {"node_a": 100.0, "node_b": 10.0, "node_c": 50.0, "node_d": 80.0}
MEASURED = {"node_a": 300.0, "node_b": 200.0, "node_c": 100.0, "node_d": 250.0}
GOOD = {
    ("compute", "node_b"): Rule(y_hat=math.log(10.0), half_width=0.1),
    ("compute", None): Rule(y_hat=0.0, half_width=0.1),
}
NEUTRAL = {("compute", None): Rule(y_hat=0.0, half_width=0.1)}
"""ML used with no correction: the analytical raw value (picks node_b)."""


def _slice(n: int) -> tuple[list[Any], list[Any]]:
    request, snapshot = fabric(HISTORY)
    groups = [group(f"g{i:02d}", MEASURED) for i in range(n)]
    recs = [r for g in groups for r in records(g, request, snapshot, reps=1)]
    return groups, recs


def test_a_better_candidate_passes() -> None:
    groups, recs = _slice(12)
    good, _ = stub_ml(GOOD)
    neutral, _ = stub_ml(NEUTRAL)
    check = ml_regret_check(groups, recs, None, good, neutral)
    assert check.status == "passed" and check.groups == 12
    assert check.regret_new == 0.0 and check.regret_active == 100.0


def test_a_worse_candidate_is_rejected_against_the_serving_set() -> None:
    """Mutation guard: the candidate (== raw) is compared with the serving
    set (good); compared with raw / no model it would pass."""
    groups, recs = _slice(12)
    good, _ = stub_ml(GOOD)
    neutral, _ = stub_ml(NEUTRAL)
    check = ml_regret_check(groups, recs, None, neutral, good)
    assert check.status == "rejected"
    assert check.regret_new == 100.0 and check.regret_active == 0.0
    assert check.reason is not None and "holdout_regret" in check.reason
    # what the mutant would compute: candidate vs no model (raw, no calibration)
    vs_raw = ml_regret_check(groups, recs, None, neutral, None)
    assert vs_raw.status == "passed"


def test_the_boundary_and_the_minimum() -> None:
    groups, recs = _slice(MIN_GROUPS - 1)
    good, _ = stub_ml(GOOD)
    neutral, _ = stub_ml(NEUTRAL)
    few = ml_regret_check(groups, recs, None, neutral, good)
    assert few.status == "skipped" and few.groups == MIN_GROUPS - 1
    assert few.reason is not None and "too_few_groups" in few.reason
    groups, recs = _slice(MIN_GROUPS)
    same = ml_regret_check(groups, recs, None, good, good)
    assert same.status == "passed"  # equal regret passes (boundary included)


def _doc(accepted: bool = True) -> dict[str, Any]:
    return {
        "set_version": "sha256:" + "cd" * 32,
        "accepted": accepted,
        "rule": None,
        "reason": None,
        "gate": {
            "accepted": accepted,
            "rule": None,
            "rules_failed": [],
            "regret": {"status": "not_run", "reason": "..."},
        },
    }


@pytest.mark.parametrize("status", ["passed", "skipped"])
def test_apply_records_the_block_and_keeps_the_verdict(status: str) -> None:
    check = RegretCheck(status, 12, 1.0, 1.0, None)  # type: ignore[arg-type]
    out = regret.apply(_doc(), regret.RegretOutcome(check, "sha256:" + "ef" * 32))
    assert out["accepted"] is True and out["rule"] is None
    block = out["gate"]["regret"]
    assert block["status"] == status and block["groups"] == 12
    assert block["current"] == {
        "kind": "set",
        "set_version": "sha256:" + "ef" * 32,
        "note": None,
    }
    assert block["tolerance"] == 0.05 and block["min_groups"] == MIN_GROUPS
    assert "pending_g4" not in repr(out)


def test_apply_turns_a_regret_rejection_into_a_rejected_set() -> None:
    check = RegretCheck("rejected", 12, 9.0, 1.0, "holdout_regret 9 % > 1.05 x 1 %")
    out = regret.apply(_doc(), regret.RegretOutcome(check, None))
    assert out["accepted"] is False and out["rule"] == regret.REGRET_RULE
    assert out["reason"].startswith("regret_worse: holdout_regret")
    gate = out["gate"]
    assert gate["accepted"] is False and gate["rule"] == "regret_worse"
    assert gate["rules_failed"] == ["regret_worse"]
    assert gate["regret"]["current"]["kind"] == "calibration"
    assert gate["regret"]["regret_candidate_pct"] == 9.0


def test_the_regret_slice_is_the_error_gates_split() -> None:
    pytest.importorskip("numpy", reason="needs the ml extra")
    from mf_ml_synthetic import synthetic_rows

    from meshforge.controller.ml.dataset import sort_key
    from meshforge.controller.ml.train.data import table_from_rows
    from meshforge.controller.ml.train.gate import holdout_mask

    rows = sorted(
        synthetic_rows("signal", families=4, groups_per_family=3, runs_per_group=6),
        key=sort_key,
    )
    table = table_from_rows(rows)
    mask, _counts = holdout_mask(table)
    from_table = {d for d, held in zip(table.decision_id, mask, strict=True) if held}
    held, counts = held_out_decisions(rows_of_dataset(rows))
    assert set(held) == from_table and held
    assert counts["decisions"] == len({r["decision_id"] for r in rows})
