"""V0.15 G5a on a real PostgreSQL + a real CAS.

* **the evaluator's ML rows end to end** - a campaign through the real
  dispatcher, ``POST /v1/planner/eval-reports`` with the evaluator's
  :class:`FoldMlInputs` wired: the dataset is built from the database (the
  real builder), one fold child runs per evaluated family (the real training
  code, in process; with the campaign's few runs the fold is
  ``insufficient_data`` -> ``no_fold_model``, the ML row equals calibrated);
  then with a fold trained on a synthetic dataset, the stored decisions are
  re-planned with a **real** model loaded through the serving path. The
  report is ``eval_version`` 2, validates against the API model and renders;
* **the D371 regret check in the retrain task** - a real ``fit`` child on a
  synthetic dataset: the accepted set is stored ``active`` with
  ``gate.regret`` ``skipped`` (no labelled groups for its slice) - never
  ``pending_g4``; a regret rejection stores the set ``rejected`` with rule
  ``regret_worse`` and the serving set is unchanged;
* **the registry loader** loads a stored set by version, whatever its
  status.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("fastapi", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

import httpx
from alembic import command
from fastapi import FastAPI
from mf_eval_pg import (
    FAMILY,
    dispatch_runs,
    finish_runs,
    make_campaign,
    planner,
    register_nodes,
)
from mf_ml_synthetic import parquet_file, synthetic_rows
from sqlalchemy import text

from meshforge.cli.planner_commands import eval_lines
from meshforge.common.cas import CasStore
from meshforge.controller.api import planner_eval_routes as routes
from meshforge.controller.api.auth import current_user, require_admin
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.api.planner_eval_models import EvalReportOut
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.ml import export, regret
from meshforge.controller.ml.dataset import Dataset, parquet_bytes, sort_key
from meshforge.controller.ml.registry import ModelRegistry
from meshforge.controller.ml.registry_loader import RegistryLoader
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.ml.train import __main__ as train_main
from meshforge.controller.ml_retrain_task import MlRetrainer
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_eval.ml_inputs import FoldMlInputs
from meshforge.controller.planner_eval.regret_gate import RegretCheck
from meshforge.controller.planner_eval.report import render_markdown
from meshforge.controller.planner_eval.service import EvalService
from meshforge.controller.planner_eval.store import EvalReportStore
from meshforge.controller.settings import PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]

FAST = ["--terms", "compute", "--algorithms", "ridge", "--min-rows", "60"]


class _Principal:
    user_id = "usr_test"
    name = "t"
    is_admin = True


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=6)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def cas(tmp_path: Path) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    return store


async def _campaign(engine: Any) -> str:
    nodes = await register_nodes(engine, 3)
    campaign_id, runs = await make_campaign(engine, nodes, [20, 22])
    dispatched = await dispatch_runs(ExecutionStore(engine), planner(nodes), runs)
    groups = sorted({r.group_id for r in runs})
    best = {groups[0]: nodes[2], groups[1]: nodes[0]}

    def total(run: Any) -> int:
        fast = best[run.group_id] == run.node_id
        return (1_000 if fast else 3_000) + run.repetition

    await finish_runs(engine, dispatched, total)
    return campaign_id


def _app(service: EvalService) -> FastAPI:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(routes.planner_eval_router)
    user = _Principal()
    app.dependency_overrides[current_user] = lambda: user
    app.dependency_overrides[require_admin] = lambda: user
    setattr(
        app.state, routes.PLANNER_EVAL_STATE_KEY, routes.PlannerEvalApi(service=service)
    )
    return app


def _in_process_child(dataset: Path | None, seen: list[list[str]]) -> Any:
    """The real training code in this process; ``dataset`` replaces the
    controller's dataset file (a synthetic one with enough rows)."""

    async def child(argv: Sequence[str], timeout_s: float) -> tuple[int, str]:
        args = list(argv[argv.index("fit") :])
        if dataset is not None:
            args[args.index("--dataset") + 1] = str(dataset)
        seen.append(args)
        return await asyncio.to_thread(train_main.main, [*args, *FAST]), ""

    return child


async def _report(engine: Any, cas: CasStore, child: Any, campaign: str) -> Any:
    registry = ModelRegistry(engine, cas)
    settings = PlannerSettings()
    inputs = FoldMlInputs(
        settings,
        ModelSource(registry, "auto"),
        RegistryLoader(registry),
        lambda: export.build_dataset(engine, max_runs=1000),
        child=child,
        missing=list,
    )
    service = EvalService(
        engine,
        CalibrationStore(engine),
        EvalReportStore(engine),
        CalibrationConfig(),
        max_rows=10_000,
        ml=inputs,
    )
    transport = httpx.ASGITransport(app=_app(service))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        response = await client.post(
            "/v1/planner/eval-reports", json={"campaign_ids": [campaign]}
        )
    assert response.status_code == 201, response.text
    return response.json()


async def test_the_evaluator_ml_rows_end_to_end(
    engine: Any, cas: CasStore, tmp_path: Path
) -> None:
    campaign = await _campaign(engine)
    seen: list[list[str]] = []
    record = await _report(engine, cas, _in_process_child(None, seen), campaign)
    report = record["report"]
    assert report["eval_version"] == 2
    ml = report["ml"]
    assert ml["status"] == "evaluated"
    assert [s[s.index("--exclude-family") + 1] for s in seen] == [FAMILY]
    (fold,) = ml["folds"]
    assert fold["family"] == FAMILY and fold["reason"] == "insufficient_data"
    assert FAMILY not in fold["trained_families"]
    rows = {p["key"]: p for p in report["policies"]}
    assert (
        rows["cost_model:ml:min_time"]["mean_regret_pct"]
        == rows["cost_model:calibrated:min_time"]["mean_regret_pct"]
    )
    total = ml["fallback"]["total"]
    assert total["no_fold_model"] == total["decisions"] == 18
    EvalReportOut.model_validate(report)
    assert "## ML planner" in render_markdown(report)
    assert any(line.startswith("ml_criterion_1:") for line in eval_lines(record))

    # a fold with a real learned model (trained on a synthetic dataset): the
    # stored decisions are re-planned with it, through the serving path
    synthetic = parquet_file(
        synthetic_rows("signal", families=5, groups_per_family=3, runs_per_group=8),
        tmp_path / "synthetic.parquet",
    )
    seen.clear()
    learned = await _report(engine, cas, _in_process_child(synthetic, seen), campaign)
    ml = learned["report"]["ml"]
    (fold,) = ml["folds"]
    assert fold["source"] == "fold" and fold["learned_terms"] == ["compute"]
    assert fold["reason"] is None and fold["set_version"].startswith("sha256:")
    total = ml["fallback"]["total"]
    assert total["no_fold_model"] == 0 and total["decisions"] == 18
    assert sum(total["terms"].values()) > 0  # the model was asked per term
    # the synthetic dataset has no row of the campaign's family: no held-out
    # prediction metrics (the fold metrics are covered by the unit tests)
    assert ml["terms"] == {}
    EvalReportOut.model_validate(learned["report"])
    async with engine.connect() as conn:
        sets = (await conn.execute(text("SELECT count(*) FROM ml_model_set"))).scalar()
    assert sets == 0  # fold sets are never registered


def _built(rows: list[dict[str, Any]]) -> export.BuiltDataset:
    ordered = sorted(rows, key=sort_key)
    data = parquet_bytes(ordered)
    dataset = Dataset(
        rows=tuple(ordered),
        counts={"rows": len(ordered)},
        parquet=data,
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
    )
    return export.BuiltDataset(dataset=dataset, max_runs=1000, truncated=False)


def _retrainer(engine: Any, cas: CasStore) -> tuple[MlRetrainer, ModelSource, Any]:
    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")
    task = MlRetrainer(engine, registry, source, PlannerSettings(ml_min_rows=100))
    return task, source, registry


async def _move_watermark(engine: Any, n: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run_record (schema_version, attempt_id, job_id, "
                "attempt_no, node_id, strategy, placement_mode, attempt_state, "
                "failure_class, attempt_created_at) VALUES (3, :att, :job, "
                "1, 'node_x', 'mode1', 'auto', 'done', 'ok', now())"
            ),
            {"att": f"att_{n}", "job": f"job_{n}"},
        )


async def test_the_regret_check_in_the_retrain_task(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    datasets = {
        seed: _built(
            synthetic_rows(
                "signal", families=6, groups_per_family=5, runs_per_group=10, seed=seed
            )
        )
        for seed in (1, 7)
    }
    current = {"seed": 1}

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return datasets[current["seed"]]

    monkeypatch.setattr(export, "build_dataset", fake_build)
    task, source, registry = _retrainer(engine, cas)

    # 1. a regret rejection: stored rejected, rule regret_worse; nothing serves
    real_check = regret.check_candidate
    seen: list[Any] = []

    async def rejecting(*_a: Any, **kw: Any) -> regret.RegretOutcome:
        seen.append(kw["reference_version"])
        check = RegretCheck("rejected", 12, 9.0, 1.0, "holdout_regret 9 % > 1.05 x 1 %")
        return regret.RegretOutcome(check, kw["reference_version"])

    monkeypatch.setattr(regret, "check_candidate", rejecting)
    first = await task.retrain_now("manual")
    assert first.error is None and first.row is not None, first
    assert first.row.status == "rejected"
    assert first.row.reason is not None and first.row.reason.startswith("regret_worse")
    assert first.row.gate["rule"] == "regret_worse"
    assert first.row.gate["rules_failed"] == ["regret_worse"]
    assert first.row.gate["regret"]["status"] == "rejected"
    assert seen == [None]  # nothing served: compared with the calibration
    assert source.current is None

    # 2. the real check: no labelled groups for the slice -> skipped, the
    # error gate alone decides; never pending_g4
    monkeypatch.setattr(regret, "check_candidate", real_check)
    current["seed"] = 7
    await _move_watermark(engine, 1)
    second = await task.retrain_now("manual")
    assert second.error is None and second.row is not None, second
    assert second.row.status == "active"
    block = second.row.gate["regret"]
    assert block["status"] == "skipped" and "too_few_groups" in block["reason"]
    assert block["current"]["kind"] == "calibration"
    assert block["held_decisions"] > 0 and block["jobs"] > 0
    assert "pending_g4" not in str(second.row.gate)
    assert source.current is not None
    assert source.current.set_version == second.row.set_version

    # the registry loader: stored sets by version, whatever their status
    loader = RegistryLoader(registry)
    for row in (first.row, second.row):
        loaded = await loader.load(row.set_version)
        assert loaded is not None and loaded.version == row.set_version
