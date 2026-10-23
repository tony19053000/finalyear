"""V0.15 G5a tester: registry loader concurrency / hostile cases and
ablation / importance determinism across interpreter hash seeds."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")
from mf_ml_synthetic import parquet_file, synthetic_rows

from meshforge.controller.ml.artefact import ArtefactError
from meshforge.controller.ml.registry import MlRegistryError
from meshforge.controller.ml.registry_loader import RegistryLoader
from meshforge.controller.ml.schema import FEATURE_VERSION
from meshforge.controller.ml.serve import LoadedSet, load_model
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set

pytestmark = pytest.mark.unit


class _Row:
    def __init__(self, version: str, result: object, fv: int = FEATURE_VERSION) -> None:
        self.set_version, self.result, self.feature_version = version, result, fv
        self.status = "rejected"


class _Reg:
    def __init__(self) -> None:
        r = train_set(
            table_from_rows(
                synthetic_rows(
                    "signal", families=4, groups_per_family=3, runs_per_group=6
                )
            ),
            CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=50),
        )
        self.result = r
        self.row = _Row(r.set_version, r)
        self.gets = 0
        self.mode = "ok"

    async def get(self, version: str):  # type: ignore[no-untyped-def]
        self.gets += 1
        await asyncio.sleep(0.01)
        if self.mode == "db":
            raise MlRegistryError("down", details={})
        return self.row if version == self.row.set_version else None

    async def load(self, row):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0.01)
        if self.mode == "corrupt":
            raise ArtefactError("bad", details={"reason": "digest_mismatch"})
        models = {
            m.term: load_model(m.artefact, expected_version=m.version)
            for m in row.result.learned
        }
        return LoadedSet(row.set_version, row.feature_version, models)


def test_concurrent_loads_of_one_version_all_get_the_same_valid_answer() -> None:
    reg = _Reg()
    loader = RegistryLoader(reg)  # type: ignore[arg-type]

    async def run() -> None:
        got = await asyncio.gather(
            *(loader.load(reg.row.set_version) for _ in range(8))
        )
        assert all(g is not None and g.version == reg.row.set_version for g in got)
        assert loader.cached == (reg.row.set_version,)  # cache not duplicated
        again = await loader.load(reg.row.set_version)
        assert again is not None
        before = reg.gets
        await loader.load(reg.row.set_version)
        assert reg.gets == before  # now served from cache

    asyncio.run(run())


def test_db_outage_then_recovery_is_not_cached_as_a_miss() -> None:
    reg = _Reg()
    loader = RegistryLoader(reg)  # type: ignore[arg-type]

    async def run() -> None:
        reg.mode = "db"
        with pytest.raises(MlRegistryError):
            await loader.load(reg.row.set_version)
        reg.mode = "ok"
        assert await loader.load(reg.row.set_version) is not None

    asyncio.run(run())


def test_corrupt_artefact_is_none_then_recovers_when_fixed() -> None:
    reg = _Reg()
    loader = RegistryLoader(reg)  # type: ignore[arg-type]

    async def run() -> None:
        reg.mode = "corrupt"
        assert await loader.load(reg.row.set_version) is None
        reg.mode = "ok"
        assert await loader.load(reg.row.set_version) is not None

    asyncio.run(run())


def test_blocking_from_a_thread_while_the_loop_is_busy_and_on_loop_thread() -> None:
    reg = _Reg()
    loader = RegistryLoader(reg)  # type: ignore[arg-type]

    async def run() -> None:
        loop = asyncio.get_running_loop()
        fn = loader.blocking(loop)
        results = await asyncio.gather(
            *(asyncio.to_thread(fn, reg.row.set_version) for _ in range(4))
        )
        assert all(r is not None for r in results)
        assert await asyncio.to_thread(fn, "sha256:" + "11" * 32) is None
        with pytest.raises(RuntimeError):
            fn(reg.row.set_version)

    asyncio.run(run())


def test_loader_size_must_be_positive() -> None:
    with pytest.raises(ValueError):
        RegistryLoader(_Reg(), size=0)  # type: ignore[arg-type]


# ------------------------------------------------- ablation determinism


def _compare(dataset: Path, out: Path, seed: str, *extra: str) -> str:
    env = {**os.environ, "PYTHONHASHSEED": seed}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "meshforge.controller.ml.train",
            "compare",
            "--dataset",
            str(dataset),
            "--out",
            str(out),
            "--terms",
            "compute",
            "--algorithms",
            "ridge",
            "--min-rows",
            "60",
            *extra,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    return (out / "report.json").read_text(encoding="utf-8")


def test_ablation_report_is_byte_identical_across_processes_and_hash_seeds(
    tmp_path: Path,
) -> None:
    dataset = parquet_file(
        synthetic_rows("signal", families=5, groups_per_family=3, runs_per_group=8),
        tmp_path / "d.parquet",
    )
    a = _compare(dataset, tmp_path / "a", "0", "--ablations")
    b = _compare(dataset, tmp_path / "b", "12345", "--ablations")
    assert a == b
    doc = json.loads(a)
    assert "ablations" in doc
    assert doc["terms"][0]["importance_repeats"] == 5


def test_ablations_on_a_dataset_with_a_nan_column_and_few_families(
    tmp_path: Path,
) -> None:
    from meshforge.controller.ml.schema import NUMERIC_FEATURES

    col = next(iter(NUMERIC_FEATURES))
    rows = synthetic_rows(
        "signal",
        families=2,
        groups_per_family=2,
        runs_per_group=40,
        nan_column=getattr(col, "value", str(col)),
    )
    dataset = parquet_file(rows, tmp_path / "d.parquet")
    text = _compare(dataset, tmp_path / "o", "1", "--ablations")
    json.loads(text)  # a valid document, no NaN tokens
    assert "NaN" not in text
