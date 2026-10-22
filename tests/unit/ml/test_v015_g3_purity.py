"""V0.15 G3 (D339, D359, brief §4/§5c): no pickle anywhere, and serving needs
no scikit-learn.

* **No unpickling under ``src/``** (AST, every module): no import of
  ``pickle`` / ``joblib`` / ``cloudpickle`` / ``dill`` / ``marshal`` /
  ``shelve``, no ``allow_pickle=True`` keyword, no ``torch.load`` call -
  anywhere in MeshForge (the GPU service and agent included; their tensors
  travel as raw bytes).
* **Serving without scikit-learn, in a fresh interpreter**: with
  ``sklearn`` made unimportable, ``meshforge.controller.ml.serve`` imports
  and loads + predicts ridge / kNN / tree / forest artefacts written by the
  trainer; the predictions equal the trainer's in-memory ones; and
  ``sklearn`` is never in ``sys.modules``.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import meshforge

pytestmark = pytest.mark.unit

SRC = Path(meshforge.__file__).parent
UNSAFE = {"pickle", "joblib", "cloudpickle", "dill", "marshal", "shelve"}


def _offences(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.extend(
                f"import {a.name}" for a in node.names if a.name.split(".")[0] in UNSAFE
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.split(".")[0] in UNSAFE:
                out.append(f"from {node.module}")
        elif isinstance(node, ast.keyword) and node.arg == "allow_pickle":
            if not (isinstance(node.value, ast.Constant) and node.value.value is False):
                out.append("allow_pickle not False")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "load"
            and isinstance(node.value, ast.Name)
            and node.value.id in {"torch", *UNSAFE}
        ):
            out.append(f"{node.value.id}.load")
    return out


def test_no_unpickling_anywhere_under_src() -> None:
    offenders = {
        str(path.relative_to(SRC)): found
        for path in sorted(SRC.rglob("*.py"))
        if (found := _offences(path))
    }
    assert offenders == {}


def test_the_scan_finds_what_it_must(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text(
        "import pickle\nfrom joblib import load\nimport numpy as np\n"
        "np.load('x', allow_pickle=True)\nimport torch\ntorch.load('m')\n"
    )
    found = _offences(bad)
    assert len(found) == 4, found


_CHILD = """
import json, sys
sys.modules["sklearn"] = None  # any import of scikit-learn fails
import numpy as np
from meshforge.controller.ml.serve import load_model
from pathlib import Path
out = {}
d = Path(sys.argv[1])
X = np.load(d / "X.npy", allow_pickle=False)
for f in sorted(d.glob("*.art")):
    model = load_model(f.read_bytes(), expected_version=f.stem.replace("_", ":"))
    out[f.name] = model.predict(X).y_hat.tolist()
assert not any(m == "sklearn" or m.startswith("sklearn.") for m in sys.modules
               if sys.modules[m] is not None)
print(json.dumps(out))
"""


@pytest.mark.timeout(120)
def test_serving_needs_no_scikit_learn_in_a_fresh_process(tmp_path: Path) -> None:
    pytest.importorskip("sklearn", reason="needs the ml extra (to train)")
    from mf_ml_synthetic import synthetic_rows

    from meshforge.controller.ml.artefact import digest
    from meshforge.controller.ml.train.algorithms import (
        GRID,
        Config,
        FitContext,
        fit_one,
    )
    from meshforge.controller.ml.train.compare import CompareSettings
    from meshforge.controller.ml.train.data import table_from_rows
    from meshforge.controller.ml.train.fit import artefact_bytes

    table = table_from_rows(
        synthetic_rows("signal", families=4, groups_per_family=3, runs_per_group=8)
    )
    rows = table.rows_of("compute")
    train, test = rows[: len(rows) * 3 // 4], rows[len(rows) * 3 // 4 :]
    np.save(tmp_path / "X.npy", table.X[test], allow_pickle=False)
    expected: dict[str, list[float]] = {}
    for algorithm in ("ridge", "knn", "tree", "forest"):
        params = GRID[algorithm][0]
        config = Config(algorithm, tuple(sorted(params.items())), 0)
        model = fit_one(config, FitContext(table=table), train)
        data = artefact_bytes(
            table, "compute", config, model, train, 0.2, CompareSettings()
        )
        name = digest(data).replace(":", "_") + ".art"
        (tmp_path / name).write_bytes(data)
        expected[name] = model.predict(table.X[test], test).tolist()
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
        timeout=100,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    got = json.loads(result.stdout)
    assert set(got) == set(expected)
    for name, values in expected.items():
        np.testing.assert_allclose(got[name], values, rtol=0, atol=1e-9)
