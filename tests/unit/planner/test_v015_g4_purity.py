"""V0.15 G4 (D336, brief §4): the planner stays pure with a model plugged in.

* no module of the planner package imports ``meshforge.controller.ml`` (or
  any ML library, numpy included) - the model and the feature builder are
  injected (``MlInput``);
* ``planner/predictor.py`` imports only the standard library's pure parts
  and the planner itself (no clock: the predict budget lives in
  ``controller/ml/predictor.py``);
* ``controller/ml/predictor.py`` and ``controller/ml/replay.py`` import no ML
  library (serving the planner input needs none).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import meshforge.controller.ml as ml_pkg
import meshforge.controller.planner as planner_pkg

pytestmark = pytest.mark.unit

PLANNER = Path(planner_pkg.__file__).parent
ML = Path(ml_pkg.__file__).parent
ML_LIBS = {"sklearn", "lightgbm", "xgboost", "numpy", "scipy", "pandas", "torch"}
PURE_STD = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "math",
    "typing",
}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


@pytest.mark.parametrize("path", sorted(PLANNER.rglob("*.py")), ids=lambda p: p.name)
def test_the_planner_never_imports_controller_ml(path: Path) -> None:
    for module in _imports(path):
        assert not module.startswith("meshforge.controller.ml"), (path.name, module)
        assert module.split(".")[0] not in ML_LIBS, (path.name, module)


def test_the_predictor_protocol_is_pure() -> None:
    for module in _imports(PLANNER / "predictor.py"):
        top = module.split(".")[0]
        if top == "meshforge":
            assert module.startswith("meshforge.controller.planner."), module
        else:
            assert top in PURE_STD, module


@pytest.mark.parametrize("name", ["predictor.py", "replay.py"])
def test_serving_the_planner_input_needs_no_ml_library(name: str) -> None:
    for module in _imports(ML / name):
        assert module.split(".")[0] not in ML_LIBS, module
        assert not module.startswith("meshforge.controller.ml.train"), module
