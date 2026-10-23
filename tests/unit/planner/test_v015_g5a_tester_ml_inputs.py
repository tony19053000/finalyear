"""V0.15 G5a tester: FoldMlInputs under failing / odd fold children."""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.dataset import parquet_bytes, sort_key
from meshforge.controller.ml.export import BuiltDataset
from meshforge.controller.ml.train import __main__ as train_main
from meshforge.controller.planner_eval.ml_inputs import FoldMlInputs
from meshforge.controller.planner_eval.model import ML_EVALUATED
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)
FAST = ["--terms", "compute", "--algorithms", "ridge", "--min-rows", "40"]


class _M:
    mode = "auto"

    async def serving_version(self) -> tuple[None, None]:
        return (None, None)


class _L:
    async def load(self, v: str) -> Any:
        return None


def _make(child: Any, rows: list[dict[str, Any]] | None = None) -> FoldMlInputs:
    data = sorted(
        rows
        or synthetic_rows("signal", families=4, groups_per_family=3, runs_per_group=6),
        key=sort_key,
    )

    class _D:
        pass

    d = _D()
    d.rows = tuple(data)  # type: ignore[attr-defined]
    d.parquet = parquet_bytes(data)  # type: ignore[attr-defined]

    async def build() -> BuiltDataset:
        return BuiltDataset(d, 100, False)  # type: ignore[arg-type]

    return FoldMlInputs(
        PlannerSettings(),
        _M(),
        _L(),
        build,
        child=child,
        missing=list,
        clock=lambda: NOW,  # type: ignore[arg-type]
    )


def test_one_family_times_out_the_rest_still_get_models() -> None:
    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        args = list(argv[argv.index("fit") :])
        if "fam01" in args:
            raise TimeoutError
        if "fam02" in args:
            return 137, "Killed"
        return train_main.main([*args, *FAST]), ""

    out = asyncio.run(
        _make(child).prepare(
            {f"fam0{i}": frozenset() for i in range(4)}, allow_leak=False
        )
    )
    assert out.status == ML_EVALUATED
    by = {f["family"]: f for f in out.folds}
    assert by["fam01"]["reason"].startswith("timeout")
    assert by["fam02"]["reason"].startswith("child_failed")
    assert out.inputs["fam01"] is None and out.inputs["fam02"] is None
    assert out.inputs["fam00"] is not None and out.inputs["fam03"] is not None
    for fam in ("fam00", "fam03"):
        assert fam not in by[fam]["trained_families"]


def test_no_families_and_all_folds_failing_are_unavailable_models_not_errors() -> None:
    async def dead(_a: Sequence[str], _t: float) -> tuple[int, str]:
        return 1, "x"

    out = asyncio.run(_make(dead).prepare({}, allow_leak=False))
    assert out.evaluated and out.inputs == {} and out.folds == []
    out = asyncio.run(
        _make(dead).prepare({"fam00": frozenset({"a"})}, allow_leak=False)
    )
    assert out.inputs == {"fam00": None}
    assert out.terms == {}


def test_a_family_that_is_not_in_the_dataset_trains_on_everything_without_error() -> (
    None
):
    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        args = list(argv[argv.index("fit") :])
        return train_main.main([*args, *FAST]), ""

    out = asyncio.run(_make(child).prepare({"ghost": frozenset()}, allow_leak=False))
    assert out.folds[0]["family"] == "ghost"
    assert "ghost" not in out.folds[0]["trained_families"]
    assert out.terms == {}  # nothing held out to measure
