"""V0.15 G2 (D340, brief §4): a controller **without** the ``ml`` extra runs
exactly as V0.14.

A second virtual environment per test run is too costly, so the extra is
removed by **import failure**: a fresh interpreter sets
``sys.modules[name] = None`` for scikit-learn, LightGBM, XGBoost (and
threadpoolctl) before anything else - every ``import`` of them raises
``ImportError`` and ``importlib.util.find_spec`` answers None, as on a machine
that never installed them. In that interpreter:

* the controller's process modules import (server, HTTP app, planner,
  dispatcher, the ML dataset side);
* the whole V0.14 planner unit suite and every ``tests/unit/ml`` file pass
  (the G2 training tests skip with "needs the ml extra");
* ``python -m meshforge.controller.ml.train compare`` exits 2 with the
  install hint, never a traceback.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = [pytest.mark.component, pytest.mark.timeout(600)]

REPO = Path(__file__).resolve().parents[3]
BLOCK = textwrap.dedent(
    """
    import sys
    for name in ("sklearn", "lightgbm", "xgboost", "threadpoolctl"):
        sys.modules[name] = None  # as if never installed
    """
)


def _python(body: str, *, timeout: float = 540) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", BLOCK + textwrap.dedent(body)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def test_the_controller_imports_without_the_ml_extra() -> None:
    pytest.importorskip("fastapi", reason="needs the controller extra")
    result = _python(
        """
        import meshforge.controller.server
        import meshforge.controller.http
        import meshforge.controller.jobs.dispatch
        import meshforge.controller.planner.plan
        import meshforge.controller.ml.export
        import meshforge.controller.ml.cache
        from meshforge.controller.ml.train import missing_libraries
        assert missing_libraries() == ["sklearn", "lightgbm", "xgboost"], missing_libraries()
        try:
            import meshforge.controller.ml.train.algorithms
        except ImportError:
            pass
        else:
            raise SystemExit("the block did not hold")
        print("ok")
        """
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_the_v014_planner_suite_passes_without_the_ml_extra() -> None:
    pytest.importorskip("pyarrow", reason="needs the controller extra")
    result = _python(
        """
        import pytest
        raise SystemExit(pytest.main([
            "-q", "-p", "no:cacheprovider", "tests/unit/planner", "tests/unit/ml",
        ]))
        """
    )
    tail = result.stdout[-3000:]
    assert result.returncode == 0, tail + result.stderr[-2000:]
    assert " passed" in tail and "failed" not in tail
    assert "skipped" in tail  # the G2 training tests: needs the ml extra


def test_compare_without_the_extra_is_exit_2_with_the_hint(tmp_path: Path) -> None:
    result = _python(
        f"""
        from meshforge.controller.ml.train.__main__ import main
        raise SystemExit(main([
            "compare", "--dataset", {str(tmp_path / "d.parquet")!r},
            "--out", {str(tmp_path / "o")!r},
        ]))
        """
    )
    assert result.returncode == 2, result.stderr
    assert "--extra ml" in result.stderr and "Traceback" not in result.stderr
