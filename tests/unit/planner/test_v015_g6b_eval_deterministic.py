"""V0.15 G6b fix round 1 (Lead decision): the fold models' cut-off
``until`` is the newest ``finished_at`` of the dataset the folds train on,
never the wall clock - two evaluations of the same data give the same
report bytes (the stored row's ``created_at`` keeps the wall time)."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
from mf_eval_world import fabric, group, records
from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.dataset import parquet_bytes, sort_key
from meshforge.controller.ml.export import BuiltDataset
from meshforge.controller.ml.train import __main__ as train_main
from meshforge.controller.planner_eval.core import evaluate
from meshforge.controller.planner_eval.ml_inputs import FoldMlInputs, dataset_until
from meshforge.controller.planner_eval.model import (
    ML_EVALUATED,
    EvalConfig,
    EvalMl,
    default_policies,
)
from meshforge.controller.planner_eval.report import report_text
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

FAST = ["--terms", "compute", "--algorithms", "ridge", "--min-rows", "40"]


class _Models:
    mode = "auto"

    async def serving_version(self) -> tuple[str | None, str | None]:
        return None, None


class _Loader:
    async def load(self, version: str) -> Any:
        return None


class _Dataset:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = tuple(sorted(rows, key=sort_key))
        self.parquet = parquet_bytes(list(self.rows))


def _fold_inputs(rows: list[dict[str, Any]]) -> FoldMlInputs:
    async def build() -> BuiltDataset:
        return BuiltDataset(_Dataset(rows), 100, False)  # type: ignore[arg-type]

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        return train_main.main([*argv[argv.index("fit") :], *FAST]), ""

    return FoldMlInputs(
        PlannerSettings(),
        _Models(),  # type: ignore[arg-type]
        _Loader(),  # type: ignore[arg-type]
        build,
        child=child,
        missing=list,
    )


def _one_evaluation(rows: list[dict[str, Any]]) -> tuple[EvalMl, str]:
    families = {"fam00": frozenset[str](), "fam01": frozenset[str]()}
    ml = asyncio.run(_fold_inputs(rows).prepare(families, allow_leak=False))
    request, snapshot = fabric({"node_a": 100, "node_b": 10, "node_c": 50})
    g = group("g1", {"node_a": 300, "node_b": 100, "node_c": 200})
    report = evaluate(
        [g],
        records(g, request, snapshot),
        {},
        default_policies(ml=True),
        EvalConfig(bootstrap_resamples=50),
        campaign_ids=["camp_x"],
        ml=ml,
    )
    return ml, report_text(report)


def test_two_evaluations_of_the_same_data_give_the_same_report_bytes() -> None:
    rows = synthetic_rows("signal", families=3, groups_per_family=3, runs_per_group=6)
    first_ml, first = _one_evaluation(rows)
    second_ml, second = _one_evaluation(rows)
    assert first_ml.status == ML_EVALUATED
    newest = max(r["finished_at"] for r in rows)
    assert first_ml.until == second_ml.until == newest.isoformat()
    assert all(f["until"] == newest.isoformat() for f in first_ml.folds)
    assert first == second  # byte-identical, ML block included


def test_dataset_until_is_the_newest_finish_in_utc_and_epoch_when_empty() -> None:
    a = dt.datetime(2026, 10, 1, 12, tzinfo=dt.UTC)
    b = dt.datetime(2026, 10, 2, 9, tzinfo=dt.timezone(dt.timedelta(hours=5)))
    naive = dt.datetime(2026, 9, 1)  # noqa: DTZ001 - a naive stamp on purpose
    assert dataset_until([{"finished_at": a}, {"finished_at": b}]) == b.astimezone(
        dt.UTC
    )
    assert dataset_until([{"finished_at": naive}]) == naive.replace(tzinfo=dt.UTC)
    assert dataset_until([]) == dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    assert dataset_until([{"finished_at": "2026-10-01"}]).year == 1970  # not a time
