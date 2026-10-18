"""V0.9 G4 lane B tester r1 on real PostgreSQL: the original lane-A hostile
condition cases (restored after the A x B merge moved the writer to
``condition_outcome``) plus lane B's own sources (session label, facts).

Every case: the record is written (PostgreSQL does not refuse the row), nothing
is lost or skipped, and a value the database cannot store becomes null.
"""

# ruff: noqa: F811
from __future__ import annotations

import math
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from sqlalchemy import text
from test_v09_g3a_recorder_cap import (  # noqa: F401  (fixtures are imported)
    _attempts,
    _recorded,
    engine,
    node_id,
)

from meshforge.controller.telemetry.recorder import RunRecorder

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(180)]

BASE = {"campaign_id": "camp_1", "group_id": "g", "config_id": "c", "repetition": 1}


def _lookup(table: dict[str, dict[str, Any]]) -> Any:
    async def campaign_of(db: Any, job_ids: Any) -> dict[str, dict[str, Any]]:
        return {j: table[j] for j in job_ids if j in table}

    return campaign_of


async def _rows(engine: Any) -> dict[str, dict[str, Any]]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text(
                "SELECT attempt_id, condition, condition_measured, failure_class "
                "FROM run_record"
            )
        )
        return {r[0]: dict(r._mapping) for r in result}


async def test_original_hostile_condition_values_still_never_lose_the_record(
    engine: Any, node_id: str
) -> None:
    """9b80be2's cases: 129 chars, NUL, NUL in measured, NaN/inf nested, int
    condition, a not-canonical 'ok:1' - now through the campaign identity."""
    cases: list[dict[str, Any]] = [
        {"condition": "c" * 129},
        {"condition": "a\x00b"},
        {"condition": "none", "condition_facts": {"k": "v\x00"}},
        {
            "condition": "queue:1",
            "condition_facts": {
                "fillers_ahead": 1,
                "x": float("nan"),
                "y": [float("inf")],
            },
        },
        {"condition": 7, "condition_facts": "not an object"},
        {"condition": "ok:1"},
        {"condition": "lone\ud800"},
        {"condition": "cpu_load:1", "session_label": "nul\x00label"},
        {"condition": "none", "session_label": "x" * 100_000},
        {"condition": "none", "session_label": "名" * 1000},
        {"condition": "queue:1", "condition_facts": {"fillers_ahead": math.inf}},
    ]
    made = await _attempts(engine, node_id, len(cases), start_s=0)
    table = {made[i][0]: {**BASE, **case} for i, case in enumerate(cases)}
    recorder = RunRecorder(engine, campaign_of=_lookup(table))
    await recorder.sweep()
    assert await _recorded(engine) == {a for _, a in made}  # all written
    assert recorder.skipped == 0
    rows = await _rows(engine)
    by_case = [rows[made[i][1]] for i in range(len(cases))]
    assert by_case[0]["condition"] is None  # too long
    assert by_case[1]["condition"] is None  # NUL
    assert (
        by_case[2]["condition"] == "none" and by_case[2]["condition_measured"] is None
    )
    assert by_case[3]["condition_measured"]["controller"]["x"] is None
    assert by_case[3]["condition_measured"]["controller"]["y"] == [None]
    assert by_case[4]["condition"] is None
    assert by_case[5]["condition"] is None  # not name[:arg]
    assert by_case[6]["condition"] is None  # lone surrogate
    assert by_case[7]["condition_measured"] is None  # NUL label: whole value null
    assert by_case[9]["condition_measured"] is not None  # unicode label stored
    for row in by_case:
        if row["condition"] is None and row["condition_measured"] is not None:
            assert row["failure_class"] == "condition_failed"
