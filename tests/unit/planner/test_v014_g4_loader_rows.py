"""V0.14 G4 (D297 "loader: DB and Parquet export - same rows"): the loader's pure
halves.

* :func:`groups_from_labels` turns the campaign labels (``build_labels``, the
  one rule) into evaluator groups: medians per configuration, the label, the
  tie set, and the ``mode3`` node set from ``bench_run`` (sorted);
* :func:`records_from_rows` reads label rows - the database's dicts and the
  same records written to the telemetry Parquet export and read back give the
  same records (so an offline export feeds the same core).
"""

from __future__ import annotations

import datetime as dt
import io
from typing import Any

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

import pyarrow.parquet as pq

from meshforge.controller.planner_eval.loader import (
    groups_from_labels,
    records_from_rows,
)
from meshforge.controller.telemetry.export import parquet_bytes
from meshforge.controller.telemetry.labels import PlannedRunState, build_labels
from meshforge.controller.telemetry.schema import COLUMN_NAMES, SCHEMA_VERSION

pytestmark = pytest.mark.unit

CAMP = "camp_01AAAAAAAAAAAAAAAAAAAAAAAA"
T0 = dt.datetime(2026, 10, 22, 9, tzinfo=dt.UTC)
CONFIGS = {
    "cfg_local": ("local", "node_a", ()),
    "cfg_b": ("mode1", "node_b", ()),
    "cfg_set": ("mode3", "node_b", ("node_c", "node_b")),
}
TOTALS = {"cfg_local": 900, "cfg_b": 500, "cfg_set": 100}


def _planned() -> list[PlannedRunState]:
    return [
        PlannedRunState(
            group_id="grp_1",
            config_id=cid,
            repetition=rep,
            family="cpu.sort",
            params={"repeats": 20},
            condition="none",
            strategy=strategy,
            node_id=node,
            origin_node_id="node_a",
            state="done",
            recorded=True,
        )
        for cid, (strategy, node, _set) in CONFIGS.items()
        for rep in (1, 2, 3)
    ]


def _rows() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for n, run in enumerate(_planned()):
        row: dict[str, Any] = dict.fromkeys(COLUMN_NAMES)
        row.update(
            schema_version=SCHEMA_VERSION,
            attempt_id=f"att_{n:04d}",
            job_id=f"job_{n:04d}",
            attempt_no=1,
            campaign_id=CAMP,
            group_id=run.group_id,
            config_id=run.config_id,
            repetition=run.repetition,
            fingerprint="ab" * 32,
            node_id=run.node_id,
            submitter_node_id="node_a",
            strategy=run.strategy,
            placement_mode="pinned",
            workload_family="cpu.sort",
            workload_status="ok",
            condition="none",
            total_ms=TOTALS[run.config_id] + run.repetition,
            attempt_state="done",
            failure_class="ok",
            attempt_created_at=T0 + dt.timedelta(seconds=n),
            attempt_ended_at=T0 + dt.timedelta(seconds=n + 1),
            recorded_at=T0 + dt.timedelta(seconds=n + 2),
        )
        out.append(row)
    return out


def test_groups_from_labels_carry_medians_label_and_sorted_sets() -> None:
    labels = build_labels(CAMP, _planned(), _rows())
    (group,) = groups_from_labels(labels, {"cfg_set": ["node_c", "node_b"]})
    assert (group.campaign_id, group.family, group.label) == (CAMP, "cpu.sort", "best")
    assert group.best_config_id == "cfg_set"
    by_id = {c.config_id: c for c in group.configs}
    assert by_id["cfg_set"].node_set == ("node_b", "node_c")
    assert by_id["cfg_set"].strategy == "mode3"
    assert by_id["cfg_b"].node_set == ()
    assert by_id["cfg_local"].median_ms == 902.0
    assert by_id["cfg_local"].origin_node_id == "node_a"
    assert all(c.repetitions == 3 for c in group.configs)


def test_parquet_rows_give_the_same_records_as_database_rows() -> None:
    rows = _rows()
    from_db = records_from_rows(CAMP, rows)
    table = pq.read_table(io.BytesIO(parquet_bytes(rows))).to_pylist()
    from_file = records_from_rows(CAMP, table)
    key = lambda r: r.attempt_id  # noqa: E731 - sort key
    assert sorted(from_db, key=key) == sorted(from_file, key=key)
    assert len(from_db) == 9
    assert {r.fingerprint for r in from_db} == {"ab" * 32}


def test_rows_without_a_config_or_repetition_are_skipped() -> None:
    rows = _rows()[:2]
    rows[0] = {**rows[0], "config_id": None}
    rows[1] = {**rows[1], "repetition": True}
    assert records_from_rows(CAMP, rows) == []
