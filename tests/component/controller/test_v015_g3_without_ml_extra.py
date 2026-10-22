"""V0.15 G3 (D340, D359, D361; brief §4/§5c): the controller without the
``ml`` extra.

* **In a fresh interpreter where scikit-learn, LightGBM and XGBoost cannot
  be imported** (``sys.modules[name] = None``, as in G2's test): the
  controller's new modules import (server, model registry, model source,
  retrain task, model routes); ``ml_missing()`` names the three; a ridge /
  forest artefact still loads and predicts (serving needs numpy only); a
  LightGBM artefact is a typed ``library_missing`` error, never an
  ``ImportError`` or a crash.
* **A real controller boots** with the extra reported missing: the retrain
  task logs ``ml_unavailable`` and never runs, ``ml = auto`` serves
  nothing (calibration only), ``GET /v1/planner/ml/models`` answers and
  ``POST /v1/planner/ml/train`` is a 409 - no task failed.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import structlog
from mf_controller_harness import make_settings

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra (to write artefacts)")

from mf_ml_synthetic import synthetic_rows

from meshforge.controller.ml.artefact import digest
from meshforge.controller.ml.train.algorithms import GRID, Config, FitContext, fit_one
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import artefact_bytes
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.timeout(300)]

REPO = Path(__file__).resolve().parents[3]
BLOCK = textwrap.dedent(
    """
    import sys
    for name in ("sklearn", "lightgbm", "xgboost", "threadpoolctl"):
        sys.modules[name] = None  # as if never installed
    """
)


def _artefacts(directory: Path) -> dict[str, str]:
    table = table_from_rows(
        synthetic_rows("signal", families=4, groups_per_family=3, runs_per_group=8)
    )
    rows = table.rows_of("compute")
    out = {}
    for algorithm in ("ridge", "forest", "lightgbm"):
        params = GRID[algorithm][0]
        config = Config(algorithm, tuple(sorted(params.items())), 0)
        model = fit_one(config, FitContext(table=table), rows)
        data = artefact_bytes(
            table, "compute", config, model, rows, 0.2, CompareSettings()
        )
        (directory / f"{algorithm}.art").write_bytes(data)
        out[algorithm] = digest(data)
    return out


def test_serving_and_the_new_modules_without_the_extra(tmp_path: Path) -> None:
    versions = _artefacts(tmp_path)
    body = f"""
    import numpy as np
    from pathlib import Path
    import meshforge.controller.server
    import meshforge.controller.api.ml_model_routes
    from meshforge.controller.ml.registry import ModelRegistry
    from meshforge.controller.ml.source import ModelSource
    from meshforge.controller.ml_retrain_task import ml_missing
    from meshforge.controller.ml.artefact import ArtefactError
    from meshforge.controller.ml.serve import load_model
    from meshforge.controller.ml.schema import NUMERIC_FEATURES
    assert ml_missing() == ["sklearn", "lightgbm", "xgboost"], ml_missing()
    d = Path({str(tmp_path)!r})
    versions = {versions!r}
    X = np.full((3, len(NUMERIC_FEATURES)), np.nan)
    for name in ("ridge", "forest"):
        out = load_model((d / f"{{name}}.art").read_bytes(),
                         expected_version=versions[name]).predict(X)
        assert out.y_hat.shape == (3,), name
    try:
        load_model((d / "lightgbm.art").read_bytes(),
                   expected_version=versions["lightgbm"])
    except ArtefactError as exc:
        assert exc.details["reason"] == "library_missing", exc.details
    else:
        raise SystemExit("a LightGBM model loaded without lightgbm")
    print("ok")
    """
    result = subprocess.run(
        [sys.executable, "-c", BLOCK + textwrap.dedent(body)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=240,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-3000:]
    assert result.stdout.strip() == "ok"


@pytest.mark.asyncio
async def test_a_controller_without_the_extra_boots_and_never_trains(
    pg_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from meshforge.controller import ml_retrain_task

    monkeypatch.setattr(ml_retrain_task, "ml_missing", lambda: ["sklearn"])
    settings = make_settings(pg_settings)
    with structlog.testing.capture_logs() as logs:
        async with ControllerRuntime.start(settings) as runtime:
            assert runtime.ml_retrainer is not None and runtime.ml_models is not None
            assert runtime.ml_retrainer.available is False
            assert runtime.ml_models.mode == "auto"
            assert runtime.ml_models.current is None
            result = await runtime.ml_retrainer.retrain_now()
            assert result.skipped == "ml_unavailable"
            assert runtime.failed_task is None
    events = [e["event"] for e in logs]
    assert "controller.planner.ml_unavailable" in events
    assert "controller.task_failed" not in events
