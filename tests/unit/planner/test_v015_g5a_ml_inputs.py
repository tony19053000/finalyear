"""V0.15 G5a (D368/D369): what the evaluator's ML rows plan with
(:class:`FoldMlInputs`).

* ``ml = off`` or the ``ml`` extra missing -> ``ml_unavailable`` with the
  reason (no dataset is built, no child runs);
* one fold per evaluated family, sorted, each with its family's
  fingerprints excluded, the same cut-off for all; per-term metrics pooled;
* ``allow_leak`` -> the serving set for every family, loaded by version;
* a database failure is an :class:`EvalLoadError` (the API's 503).
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.dataset import parquet_bytes, sort_key
from meshforge.controller.ml.export import BuiltDataset
from meshforge.controller.ml.loader import MlLoadError
from meshforge.controller.ml.train import __main__ as train_main
from meshforge.controller.planner_eval.loader import EvalLoadError
from meshforge.controller.planner_eval.ml_inputs import FoldMlInputs
from meshforge.controller.planner_eval.model import ML_EVALUATED, ML_UNAVAILABLE
from meshforge.controller.settings import PlannerSettings

pytestmark = pytest.mark.unit

NOW = dt.datetime(2026, 12, 1, tzinfo=dt.UTC)


class FakeModels:
    def __init__(self, mode: str = "auto", version: str | None = None) -> None:
        self.mode = mode
        self.version = version

    async def serving_version(self) -> tuple[str | None, str | None]:
        return ("active", self.version) if self.version else (None, None)


class FakeLoader:
    def __init__(self, answer: Any = None) -> None:
        self.answer = answer
        self.asked: list[str] = []

    async def load(self, version: str) -> Any:
        self.asked.append(version)
        return self.answer


class _Dataset:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        ordered = sorted(rows, key=sort_key)
        self.rows = tuple(ordered)
        self.parquet = parquet_bytes(ordered)


def _build(rows: list[dict[str, Any]]) -> Any:
    calls: list[int] = []

    async def build() -> BuiltDataset:
        calls.append(1)
        return BuiltDataset(_Dataset(rows), 100, False)  # type: ignore[arg-type]

    build.calls = calls  # type: ignore[attr-defined]
    return build


def _child(seen: list[list[str]]) -> Any:
    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        args = list(argv[argv.index("fit") :])
        seen.append(args)
        fast = ["--terms", "compute", "--algorithms", "ridge", "--min-rows", "40"]
        return train_main.main([*args, *fast]), ""

    return child


def _inputs(**kw: Any) -> FoldMlInputs:
    rows = kw.pop("rows", None) or synthetic_rows(
        "signal", families=4, groups_per_family=3, runs_per_group=6
    )
    args: dict[str, Any] = {
        "settings": PlannerSettings(),
        "models": FakeModels(),
        "loader": FakeLoader(),
        "build": _build(rows),
        "child": _child([]),
        "missing": list,
    }
    args.update(kw)
    return FoldMlInputs(
        args.pop("settings"),
        args.pop("models"),
        args.pop("loader"),
        args.pop("build"),
        **args,
    )


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"models": FakeModels(mode="off")}, "ml = off"),
        ({"missing": lambda: ["sklearn", "xgboost"]}, "missing: sklearn, xgboost"),
    ],
)
def test_unavailable_without_work(kw: dict[str, Any], reason: str) -> None:
    build = _build([])
    seen: list[list[str]] = []
    inputs = _inputs(build=build, child=_child(seen), **kw)
    out = asyncio.run(inputs.prepare({"fam00": frozenset()}, allow_leak=False))
    assert out.status == ML_UNAVAILABLE and reason in (out.reason or "")
    assert not out.evaluated and build.calls == [] and seen == []


def test_one_fold_per_family_with_its_fingerprints_and_one_cutoff() -> None:
    seen: list[list[str]] = []
    rows = synthetic_rows("signal", families=4, groups_per_family=3, runs_per_group=6)
    newest = max(r["finished_at"] for r in rows)  # G6b: the dataset's, not the clock
    build = _build(rows)
    inputs = _inputs(child=_child(seen), build=build)
    families = {"fam01": frozenset({"fpB", "fpA"}), "fam00": frozenset()}
    out = asyncio.run(inputs.prepare(families, allow_leak=False))
    assert out.status == ML_EVALUATED and build.calls == [1]
    assert [s[s.index("--exclude-family") + 1] for s in seen] == ["fam00", "fam01"]
    assert all(s[s.index("--until") + 1] == newest.isoformat() for s in seen)
    assert out.until == newest.isoformat()
    assert "--exclude-groups" not in seen[0]
    groups_file = Path(seen[1][seen[1].index("--exclude-groups") + 1])
    assert not groups_file.exists()  # the scratch directory is gone
    assert [f["family"] for f in out.folds] == ["fam00", "fam01"]
    for fold in out.folds:
        assert (
            fold["source"] == "fold" and fold["family"] not in fold["trained_families"]
        )
    assert set(out.inputs) == {"fam00", "fam01"}
    assert all(m is not None for m in out.inputs.values())
    assert out.terms["compute"]["families"] == 2


def test_allow_leak_uses_the_serving_set_by_version() -> None:
    marker = object()
    loader = FakeLoader(marker)
    version = "sha256:" + "ab" * 32
    inputs = _inputs(models=FakeModels(version=version), loader=loader)
    out = asyncio.run(
        inputs.prepare({"b": frozenset(), "a": frozenset()}, allow_leak=True)
    )
    assert out.status == ML_EVALUATED and loader.asked == [version]
    assert out.inputs == {"a": marker, "b": marker}
    assert {f["source"] for f in out.folds} == {"active"}
    nothing = asyncio.run(_inputs().prepare({"a": frozenset()}, allow_leak=True))
    assert nothing.inputs == {"a": None}
    assert nothing.folds[0]["reason"] == "no model set serves"


def test_a_database_failure_is_an_eval_load_error() -> None:
    async def broken() -> BuiltDataset:
        raise MlLoadError("down", details={"cause": "OSError"})

    with pytest.raises(EvalLoadError):
        asyncio.run(_inputs(build=broken).prepare({"a": frozenset()}, allow_leak=False))
