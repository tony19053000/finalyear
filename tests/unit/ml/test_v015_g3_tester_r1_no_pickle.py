# ruff: noqa: PERF401, SIM102
"""V0.15 G3 tester r1 - no deserialisation of code anywhere (D339, CLAUDE.md
section 9), written as an independent AST scan (not the implementer's grep)
plus import-time laziness of the heavy libraries in fresh interpreters.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

SRC = Path(__file__).resolve().parents[3] / "src" / "meshforge"
FORBIDDEN_MODULES = {
    "pickle",
    "cPickle",
    "_pickle",
    "dill",
    "cloudpickle",
    "joblib",
    "shelve",
    "marshal",
    "skops",
    "jsonpickle",
}
FORBIDDEN_CALLS = {
    ("np", "load"),
    ("numpy", "load"),
    ("torch", "load"),
    ("yaml", "load"),
    ("yaml", "unsafe_load"),
    ("yaml", "full_load"),
}


def _py_files() -> list[Path]:
    return [p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts]


def _violations(path: Path, tree: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in FORBIDDEN_MODULES:
                    found.append(f"{path}:{node.lineno} import {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in FORBIDDEN_MODULES:
                found.append(f"{path}:{node.lineno} from {node.module}")
        elif isinstance(node, ast.Call):
            fn = node.func
            if (
                isinstance(fn, ast.Attribute)
                and isinstance(fn.value, ast.Name)
                and (fn.value.id, fn.attr) in FORBIDDEN_CALLS
            ):
                found.append(f"{path}:{node.lineno} {fn.value.id}.{fn.attr}(...)")
            for kw in node.keywords:
                if kw.arg == "allow_pickle" and not (
                    isinstance(kw.value, ast.Constant) and kw.value.value is False
                ):
                    found.append(f"{path}:{node.lineno} allow_pickle=...")
            if isinstance(fn, ast.Name) and fn.id in {"eval", "exec", "__import__"}:
                found.append(f"{path}:{node.lineno} {fn.id}()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in FORBIDDEN_MODULES and "ml" in path.parts:
                # importlib.import_module("pickle") style
                found.append(f"{path}:{node.lineno} string {node.value!r}")
    return found


def test_no_deserialiser_or_dynamic_code_in_the_ml_modules() -> None:
    files = [
        p
        for p in _py_files()
        if "ml" in p.parts or p.name in {"ml_retrain_task.py", "ml_model_routes.py"}
    ]
    assert len(files) > 20
    bad: list[str] = []
    for path in files:
        bad += _violations(path, ast.parse(path.read_text(encoding="utf-8")))
    assert bad == [], "\n".join(bad)


def test_no_pickle_in_the_whole_product_source_tree() -> None:
    bad: list[str] = []
    for path in _py_files():
        text = path.read_text(encoding="utf-8")
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                a.name.split(".")[0] in FORBIDDEN_MODULES for a in node.names
            ):
                bad.append(f"{path}:{node.lineno}")
            if (
                isinstance(node, ast.ImportFrom)
                and (node.module or "").split(".")[0] in FORBIDDEN_MODULES
            ):
                bad.append(f"{path}:{node.lineno}")
    assert bad == [], "\n".join(bad)


def test_the_scan_catches_what_it_claims(tmp_path: Path) -> None:
    sample = textwrap.dedent(
        """
        import pickle
        from joblib import load
        import numpy as np
        np.load("x", allow_pickle=True)
        torch.load("m")
        eval("1")
        """
    )
    found = _violations(tmp_path / "s.py", ast.parse(sample))
    kinds = " ".join(found)
    for needle in ("import pickle", "from joblib", "np.load", "torch.load", "eval()"):
        assert needle in kinds, (needle, found)


def _run(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )


def test_importing_the_controller_pulls_in_no_ml_library() -> None:
    done = _run(
        """
        import sys
        import meshforge.controller.server
        import meshforge.controller.ml_retrain_task
        import meshforge.controller.ml.registry, meshforge.controller.ml.source
        import meshforge.controller.api.ml_model_routes
        bad = [m for m in ("sklearn", "lightgbm", "xgboost", "scipy.sparse", "pickle")
               if m in sys.modules and m != "pickle"]
        print("BAD", bad)
        """
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().endswith("BAD []"), done.stdout


def test_serving_imports_neither_training_nor_scikit_learn_nor_boosters() -> None:
    done = _run(
        """
        import sys
        import meshforge.controller.ml.serve, meshforge.controller.ml.artefact
        bad = [m for m in sys.modules if m.split(".")[0] in
               ("sklearn", "lightgbm", "xgboost", "pyarrow")
               or m.startswith("meshforge.controller.ml.train")]
        print("BAD", sorted(bad)[:5])
        """
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().endswith("BAD []"), done.stdout
