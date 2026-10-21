"""V0.14 G3 tester round 2: O1 - objective null on pre-V0.14 rows (API model
and ``compute explain``), and present when set."""

from __future__ import annotations

import datetime as dt
import io

import pytest
from rich.console import Console

from meshforge.cli.explain_commands import decision_tables
from meshforge.controller.api.decision_routes import _out
from meshforge.controller.jobs.decision_store import DecisionRecordOut

pytestmark = pytest.mark.unit


def _record(**kw: object) -> DecisionRecordOut:
    return DecisionRecordOut(
        id=1,
        attempt_no=1,
        attempt_id=None,
        outcome="waiting",
        policy="cost_model",
        chosen_node_id=None,
        created_at=dt.datetime(2026, 10, 22, tzinfo=dt.UTC),
        snapshot_digest="d" * 64,
        reason_key="k",
        candidates=[],
        **kw,  # type: ignore[arg-type]
    )


def _render(payload: dict[str, object]) -> str:
    out = io.StringIO()
    console = Console(file=out, width=1000, color_system=None)
    for table in decision_tables(payload):
        console.print(table)
    return " ".join(out.getvalue().split())


def test_v014_g3_tester_r2_pre_v014_row_has_null_objective_and_explain_is_unchanged() -> (
    None
):
    old = _out(_record())
    assert old.objective is None
    dumped = old.model_dump(mode="json")
    assert dumped["objective"] is None
    text = _render({"decisions": [dumped]})
    assert "objective" not in text
    assert "(policy cost_model)" in text


@pytest.mark.parametrize("objective", ["min_time", "min_network", "prefer_local"])
def test_v014_g3_tester_r2_objective_roundtrips_to_explain(objective: str) -> None:
    out = _out(_record(objective=objective))
    assert out.objective == objective
    text = _render({"decisions": [out.model_dump(mode="json")]})
    assert f"(policy cost_model), objective {objective}" in text.replace(
        "|", " "
    ).replace("  ", " ")


def test_v014_g3_tester_r2_hostile_objective_value_does_not_crash_explain() -> None:
    payload = {
        "decisions": [
            {
                "id": 1,
                "attempt_no": 1,
                "outcome": "x",
                "policy": "p",
                "objective": {"evil": 1},
                "candidates": [],
            }
        ]
    }
    assert "objective" not in _render(payload)
