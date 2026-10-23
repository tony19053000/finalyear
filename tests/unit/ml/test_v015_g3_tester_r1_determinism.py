# ruff: noqa: S311
"""V0.15 G3 tester r1 - determinism (criterion 3, D352).

Same dataset -> byte-identical ``set.json`` + artefacts + version, in fresh
processes, whatever the hash seed, the BLAS / OpenMP thread count, the row
order of the file and the library-warm state of the process. All six
algorithms (the implementer's test uses four), two terms.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
pytest.importorskip("xgboost", reason="needs the ml extra")

from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml.artefact import digest
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import load, table_from_rows
from meshforge.controller.ml.train.fit import train_set

pytestmark = pytest.mark.unit

ALL = "ridge,knn,tree,forest,lightgbm,xgboost"


def _rows() -> list[dict[str, object]]:
    return synthetic_rows(
        "signal",
        families=6,
        groups_per_family=5,
        runs_per_group=10,
        terms=("compute", "output"),
        seed=9,
    )


def _fit(dataset: Path, out: Path, *, hash_seed: str, threads: str) -> None:
    env = {
        **os.environ,
        "PYTHONHASHSEED": hash_seed,
        "OMP_NUM_THREADS": threads,
        "OPENBLAS_NUM_THREADS": threads,
        "MKL_NUM_THREADS": threads,
    }
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "fit",
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            "--terms",
            "compute,output",
            "--algorithms",
            ALL,
            "--min-rows",
            "100",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
        timeout=900,
    )
    assert done.returncode == 0, done.stderr[-2000:]


def _files(directory: Path) -> dict[str, bytes]:
    return {
        p.name: p.read_bytes()
        for p in sorted(directory.iterdir())
        if p.name != "timings.json"
    }


@pytest.mark.timeout(1800)
def test_all_algorithms_bytes_survive_threads_hash_seed_and_row_order(
    tmp_path: Path,
) -> None:
    rows = _rows()
    canonical = parquet_file(rows, tmp_path / "c.parquet")
    shuffled = parquet_file(rows, tmp_path / "s.parquet", shuffle_seed=99)
    runs = [
        ("a", canonical, "0", "1"),
        ("b", canonical, "random", "4"),
        ("c", shuffled, "12345", "8"),
        ("d", shuffled, "1", "1"),
    ]
    outputs = []
    for name, dataset, seed, threads in runs:
        out = tmp_path / name
        _fit(dataset, out, hash_seed=seed, threads=threads)
        outputs.append(_files(out))
    first = outputs[0]
    for other in outputs[1:]:
        assert other.keys() == first.keys()
        for key in first:
            assert other[key] == first[key], f"{key} differs"
    doc = json.loads(first["set.json"])
    assert doc["status"] == "trained"
    for member in doc["members"]:
        if member["algorithm"] != "none":
            assert digest(first[member["artefact"]]) == member["version"]
    # the set version must be the same in every run too
    assert len({json.loads(o["set.json"])["set_version"] for o in outputs}) == 1


def test_in_process_repeat_and_row_permutation_give_the_same_set() -> None:
    rows = _rows()
    settings = CompareSettings(
        terms=("compute",),
        algorithms=("ridge", "knn", "tree", "forest", "lightgbm", "xgboost"),
        min_rows=100,
    )
    first = train_set(table_from_rows(rows), settings)
    again = train_set(table_from_rows(rows), settings)
    shuffled = list(rows)
    import random

    random.Random(5).shuffle(shuffled)
    permuted = train_set(table_from_rows(shuffled), settings)
    assert first.set_version == again.set_version == permuted.set_version
    assert [m.artefact for m in first.members] == [m.artefact for m in again.members]
    assert [m.artefact for m in first.members] == [m.artefact for m in permuted.members]


def test_a_changed_row_changes_the_version(tmp_path: Path) -> None:
    rows = _rows()
    settings = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
    base = train_set(table_from_rows(rows), settings)
    rows[3] = {**rows[3], "y": float(rows[3]["y"]) + 0.5}  # type: ignore[arg-type]
    changed = train_set(table_from_rows(rows), settings)
    assert base.set_version != changed.set_version
    assert base.dataset_digest != changed.dataset_digest


def test_dataset_load_is_order_independent(tmp_path: Path) -> None:
    rows = _rows()
    a = load(parquet_file(rows, tmp_path / "a.parquet"))
    b = load(parquet_file(rows, tmp_path / "b.parquet", shuffle_seed=7))
    assert a.digest == b.digest


_PER_ALGORITHM = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from mf_ml_synthetic import synthetic_rows
from meshforge.controller.ml.train.algorithms import GRID, Config, FitContext, fit_one
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import artefact_bytes
from meshforge.controller.ml.artefact import digest
rows = synthetic_rows("signal", families=5, groups_per_family=4, runs_per_group=10, seed=3)
table = table_from_rows(rows)
idx = table.rows_of("compute")
out = {}
for alg, grid in GRID.items():
    if alg not in ('ridge','knn','tree','forest','lightgbm','xgboost'):
        continue
    for i, params in enumerate(grid):
        cfg = Config(alg, tuple(sorted(params.items())), i)
        model = fit_one(cfg, FitContext(table=table, seed=0, fold="final"), idx)
        out[f"{alg}:{i}"] = digest(artefact_bytes(
            table, "compute", cfg, model, idx, 0.25,
            CompareSettings(terms=("compute",), min_rows=50)))
print(json.dumps(out, sort_keys=True))
"""


@pytest.mark.timeout(900)
def test_every_algorithm_and_grid_point_is_byte_stable_across_fresh_processes() -> None:
    support = str(Path(__file__).resolve().parents[2] / "_support")
    answers = []
    for seed, threads in (("0", "1"), ("777", "6"), ("random", "2")):
        env = {
            **os.environ,
            "PYTHONHASHSEED": seed,
            "OMP_NUM_THREADS": threads,
            "OPENBLAS_NUM_THREADS": threads,
        }
        done = subprocess.run(
            [sys.executable, "-c", _PER_ALGORITHM, support],
            capture_output=True,
            text=True,
            check=False,
            env=env,
            timeout=800,
        )
        assert done.returncode == 0, done.stderr[-2000:]
        answers.append(json.loads(done.stdout.strip().splitlines()[-1]))
    assert answers[0] == answers[1] == answers[2]
    assert len({k.split(":")[0] for k in answers[0]}) == 6
