"""V0.15 G2 (D339, D340, brief §4): who may import the ML libraries, and no
pickle anywhere.

* scikit-learn / LightGBM / XGBoost / threadpoolctl are imported only by
  modules of ``meshforge.controller.ml.train`` (the import-linter contract
  ``ml-libraries-only-in-ml-train`` says the same for the direct edges);
* nothing outside that package imports it (in G2 nobody does; G3's worker
  will be added here deliberately) - the chain the contract's ignored edges
  cannot see;
* ``train/__init__`` imports no ML library, so a controller without the
  extra can ask :func:`missing_libraries` and give the install hint;
* no pickle / joblib / marshal / shelve / dill / cloudpickle import and no
  ``.dump`` / ``.load`` call of those under ``controller/ml`` (recursive);
* the planner still imports no ML library (G1's test covers the planner
  package; repeated for its subpackages).
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

import pytest

import meshforge
import meshforge.controller.ml as ml_pkg
import meshforge.controller.planner as planner_pkg

pytestmark = pytest.mark.unit

SRC = Path(meshforge.__file__).parent
TRAIN = Path(ml_pkg.__file__).parent / "train"
ML_LIBS = {"sklearn", "lightgbm", "xgboost", "threadpoolctl"}
UNSAFE = {"pickle", "joblib", "marshal", "shelve", "dill", "cloudpickle"}


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.append(node.module)
    return out


def _in_train(path: Path) -> bool:
    return TRAIN in path.parents


SERVE = Path(ml_pkg.__file__).parent / "serve.py"
SERVE_LIBS = {"lightgbm", "xgboost"}
"""V0.15 G3 (D359): serving loads LightGBM / XGBoost models with their own
library - never scikit-learn, never threadpoolctl."""


def test_ml_libraries_only_in_the_train_package() -> None:
    offenders = [
        f"{path.relative_to(SRC)} imports {module}"
        for path in sorted(SRC.rglob("*.py"))
        if not _in_train(path)
        for module in _imports(path)
        if module.split(".")[0] in ML_LIBS
        and not (path == SERVE and module.split(".")[0] in SERVE_LIBS)
    ]
    assert offenders == []
    used = {
        module.split(".")[0] for path in TRAIN.glob("*.py") for module in _imports(path)
    }
    assert {"sklearn", "lightgbm", "xgboost"} <= used  # the test is not vacuous


def test_nothing_outside_the_train_package_imports_it() -> None:
    offenders = [
        f"{path.relative_to(SRC)} imports {module}"
        for path in sorted(SRC.rglob("*.py"))
        if not _in_train(path)
        for module in _imports(path)
        if module.startswith("meshforge.controller.ml.train")
    ]
    assert offenders == []


def test_the_train_package_init_imports_no_ml_library() -> None:
    for module in _imports(TRAIN / "__init__.py"):
        assert module.split(".")[0] not in ML_LIBS | {"numpy"}, module


def test_no_pickle_anywhere_under_controller_ml() -> None:
    root = Path(ml_pkg.__file__).parent
    for path in sorted(root.rglob("*.py")):
        for module in _imports(path):
            assert module.split(".")[0] not in UNSAFE, f"{path.name} imports {module}"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id not in UNSAFE, f"{path.name}: {node.value.id}"


def test_the_planner_tree_imports_no_ml_library() -> None:
    root = Path(planner_pkg.__file__).parent
    for path in root.rglob("*.py"):
        for module in _imports(path):
            assert module.split(".")[0] not in ML_LIBS | UNSAFE, path.name


def test_the_import_contract_is_declared() -> None:
    pyproject = SRC.parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    linter = data["tool"]["importlinter"]
    assert linter["include_external_packages"] is True
    (contract,) = [
        c for c in linter["contracts"] if c["id"] == "ml-libraries-only-in-ml-train"
    ]
    assert contract["source_modules"] == ["meshforge"]
    assert set(contract["forbidden_modules"]) == {"sklearn", "lightgbm", "xgboost"}
    assert all(
        rule.startswith("meshforge.controller.ml.train.")
        or rule
        in (
            "meshforge.controller.ml.serve -> lightgbm",
            "meshforge.controller.ml.serve -> xgboost",
        )
        for rule in contract["ignore_imports"]
    )
    extra = data["project"]["optional-dependencies"]["ml"]
    assert any(r.startswith("xgboost-cpu") for r in extra)
    for extra_name in ("agent", "controller"):  # agents never install them
        deps = " ".join(data["project"]["optional-dependencies"][extra_name])
        for lib in ("scikit-learn", "lightgbm", "xgboost"):
            assert lib not in deps, (extra_name, lib)
