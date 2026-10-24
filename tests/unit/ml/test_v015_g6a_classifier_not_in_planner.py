"""V0.15 G6a (D343, D375): the 0.15.10 classifier is **report only** - never
registered, never wired into ``plan()``.

An AST scan of every MeshForge module: the classifier code
(``ml/train/classify``), its examples builder and its service are imported
only by the few modules that serve the experiment (the child entry, the
route, the server wiring); the planner package, the dispatcher, the model
source / predictor / registry / serving code import none of them, and the
planner package does not even name them.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import meshforge

pytestmark = pytest.mark.unit

SRC = Path(meshforge.__file__).parent
CLASSIFIER_MODULES = {
    "meshforge.controller.ml.train.classify": {"controller/ml/train/__main__.py"},
    "meshforge.controller.planner_eval.classifier_examples": {
        "controller/planner_eval/classifier_service.py"
    },
    "meshforge.controller.planner_eval.classifier_service": {
        "controller/api/ml_classify_routes.py",
        "controller/server.py",
    },
}
NEVER = (
    "controller/planner/",
    "controller/jobs/",
    "controller/ml/predictor.py",
    "controller/ml/source.py",
    "controller/ml/registry.py",
    "controller/ml/serve.py",
    "controller/ml/train/fit.py",
    "controller/ml/train/gate.py",
    "controller/ml_retrain_task.py",
)


def _imported(path: Path) -> set[str]:
    package = ".".join(path.relative_to(SRC.parent).with_suffix("").parts[:-1])
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            out.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = package.split(".")
                kept = parts[: len(parts) - node.level + 1]
                base = ".".join([*kept, base]).strip(".")
            out.add(base)
            out.update(f"{base}.{a.name}" for a in node.names)
    return out


def _importers() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {name: set() for name in CLASSIFIER_MODULES}
    for path in sorted(SRC.rglob("*.py")):
        names = _imported(path)
        rel = path.relative_to(SRC).as_posix()
        for module in CLASSIFIER_MODULES:
            if module in names:
                found[module].add(rel)
    return found


def test_the_classifier_is_imported_only_where_the_experiment_runs() -> None:
    found = _importers()
    for module, allowed in CLASSIFIER_MODULES.items():
        assert found[module] <= allowed, (module, found[module] - allowed)
    assert found["meshforge.controller.ml.train.classify"], "scan found nothing"


def test_the_planner_and_serving_never_reach_it() -> None:
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if not rel.startswith(NEVER):
            continue
        names = _imported(path)
        for module in CLASSIFIER_MODULES:
            assert module not in names, (rel, module)


def test_the_planner_package_does_not_name_it() -> None:
    for path in sorted((SRC / "controller" / "planner").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for word in (
            "train.classify",
            "classifier_examples",
            "classifier_service",
            "ClassifySettings",
        ):
            assert word not in text, (path.name, word)
