"""V0.15 G1 (brief §4, §5a): what the ML package may import.

* The pure modules (``schema``, ``features``, ``targets``, ``splits``,
  ``dataset``) import only the standard library's pure parts, ``pyarrow``
  (``dataset`` only) and pure MeshForge modules - never the database, the
  clock, logging, or the loader / export.
* No module of ``controller/ml`` imports an ML library (G1 trains nothing)
  or ``pickle`` / ``joblib`` / ``marshal`` / ``shelve`` (no pickle loading
  anywhere, D339).
* The planner package still imports no ML library.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import meshforge.controller.ml as ml_pkg
import meshforge.controller.planner as planner_pkg

pytestmark = pytest.mark.unit

ML_ROOT = Path(ml_pkg.__file__).parent
PURE = (
    "schema.py",
    "features.py",
    "targets.py",
    "splits.py",
    "dataset.py",
    "replay.py",  # V0.15 G4: re-plan with the stored model version (pure)
)
ALLOWED_STD = {
    "__future__",
    "collections",
    "dataclasses",
    "datetime",
    "enum",
    "hashlib",
    "json",
    "math",
    "random",
    "typing",
}
ALLOWED_MESHFORGE = (
    "meshforge.controller.ml.",
    "meshforge.controller.planner.",
    "meshforge.controller.telemetry.parquet_options",
)
ML_LIBRARIES = {
    "sklearn",
    "lightgbm",
    "xgboost",
    "torch",
    "tensorflow",
    "scipy",
    "pandas",
    "numpy",
    "joblib",
}
UNSAFE = {"pickle", "joblib", "marshal", "shelve", "dill", "cloudpickle"}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for item in ast.walk(tree):
        if isinstance(item, ast.Import):
            out.extend(a.name for a in item.names)
        elif isinstance(item, ast.ImportFrom) and item.module:
            out.append(item.module)
    return out


@pytest.mark.parametrize("name", PURE)
def test_pure_modules_import_nothing_impure(name: str) -> None:
    for module in _imports(ML_ROOT / name):
        top = module.split(".")[0]
        if top == "meshforge":
            assert module.startswith(ALLOWED_MESHFORGE), f"{name} imports {module}"
            assert not module.startswith(
                ("meshforge.controller.ml.loader", "meshforge.controller.ml.export")
            ), f"{name} imports {module}"
        elif top == "pyarrow":
            assert name == "dataset.py", f"{name} imports {module}"
        else:
            assert top in ALLOWED_STD, f"{name} imports {module}"


def test_every_ml_module_is_listed_as_pure_or_io() -> None:
    names = {p.name for p in ML_ROOT.glob("*.py")}
    # V0.15 G2: cache.py (D355, I/O through loader / export)
    # V0.15 G4: predictor.py (the planner input + budget clock, D336/D363)
    assert names == {
        *PURE,
        "__init__.py",
        "loader.py",
        "export.py",
        "cache.py",
        "predictor.py",
    }


@pytest.mark.parametrize("path", sorted(ML_ROOT.glob("*.py")), ids=lambda p: p.name)
def test_no_ml_library_and_no_pickle_in_the_ml_package(path: Path) -> None:
    for module in _imports(path):
        top = module.split(".")[0]
        assert top not in ML_LIBRARIES, f"{path.name} imports {module} (G1: no ML)"
        assert top not in UNSAFE, f"{path.name} imports {module}"


def test_the_planner_imports_no_ml_library() -> None:
    root = Path(planner_pkg.__file__).parent
    for path in root.glob("*.py"):
        for module in _imports(path):
            assert module.split(".")[0] not in ML_LIBRARIES | UNSAFE, (
                f"{path.name} imports {module}"
            )
