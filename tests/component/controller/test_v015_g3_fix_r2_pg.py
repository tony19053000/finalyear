"""V0.15 G3 fix round 2 on a real PostgreSQL + a real CAS + the real blob GC.

* reviewer #1 - an artefact the CAS already holds (``put_bytes`` dedups) has
  its GC last-use moved to *now* by :meth:`ModelRegistry.store`, so a GC pass
  between the put and the commit cannot evict it (without the hook the same
  pass evicts it - the control);
* N1 - a value the database refuses (a check violation) is
  :class:`MlRegistryError` typed ``invalid_input``, **not retryable**;
* reviewer #4 - a ``fit`` child that exits 0 with unreadable / inconsistent
  output -> the retrain fails ``child_output_invalid`` (never ``crashed``),
  nothing stored.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from alembic import command
from mf_ml_synthetic import synthetic_rows
from sqlalchemy import text

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest
from meshforge.controller.blobs import BlobStore
from meshforge.controller.gc import ControllerGc
from meshforge.controller.ml import export
from meshforge.controller.ml.dataset import Dataset, parquet_bytes, sort_key
from meshforge.controller.ml.registry import MlRegistryError, ModelRegistry
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fit_main import set_document
from meshforge.controller.ml.train.gate import Reference, run_gate
from meshforge.controller.ml_retrain_task import MlRetrainer
from meshforge.controller.settings import PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]

FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
INPUTS = {"watermark": {"x": 1}, "train": {}}


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=6)
    try:
        async with eng.begin() as conn:
            await conn.execute(text("DELETE FROM ml_model_set"))
        yield eng
    finally:
        await eng.dispose()


def _rows(seed: int) -> list[dict[str, Any]]:
    return synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=10, seed=seed
    )


def _trained(seed: int) -> tuple[dict[str, Any], dict[str, bytes]]:
    table = table_from_rows(_rows(seed))
    result = train_set(table, FAST)
    gate = run_gate(table, FAST, result, Reference())
    return set_document(result, gate, FAST), {
        m.version: m.artefact for m in result.members if m.artefact
    }


def _rejected(doc: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(doc)
    out["accepted"] = False
    out["reason"] = "not_better: test"
    out["gate"] = {**(out.get("gate") or {}), "accepted": False}
    return out


# ------------------------------------------------------------ reviewer #1


async def _gc_pass_after_dedup_store(
    engine: Any, tmp_path: Path, *, hook: bool
) -> bool:
    """Blob stored long ago (old last-use); the registry stores a rejected
    set (never GC-protected) that dedups onto it; then one GC pass over a
    1-byte quota. True = the blob survived."""
    now = [1_000.0]
    blobs = BlobStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    blobs.cas.prepare()
    gc = ControllerGc(
        blobs,
        engine,
        index_dir=tmp_path / "gc",
        quota_bytes=1,
        min_age_s=100.0,
        interval_s=3600.0,
        wall=lambda: now[0],
    )
    await gc.start()
    try:
        doc, arts = _trained(501)
        ((version, data),) = arts.items()
        await asyncio.to_thread(blobs.cas.put_bytes, data, Digest.parse(version))
        now[0] += 10_000.0  # the blob's last use is now far older than min_age_s
        registry = ModelRegistry(engine, blobs.cas, used=gc.touch if hook else None)
        res = await registry.store(
            _rejected(doc), arts, reference_version=None, inputs=INPUTS
        )
        assert res.row is not None and res.row.status == "rejected"
        await gc.run_pass("test")
        return blobs.cas.path_for(Digest.parse(version)).exists()
    finally:
        await gc.close()


async def test_a_deduped_artefact_is_touched_so_gc_keeps_it(
    engine: Any, tmp_path: Path
) -> None:
    assert await _gc_pass_after_dedup_store(engine, tmp_path / "a", hook=True)


async def test_control_without_the_hook_the_same_pass_evicts_it(
    engine: Any, tmp_path: Path
) -> None:
    assert not await _gc_pass_after_dedup_store(engine, tmp_path / "b", hook=False)


async def test_a_new_artefact_does_not_call_the_hook(
    engine: Any, tmp_path: Path
) -> None:
    calls: list[list[Digest]] = []

    async def used(digests: list[Digest]) -> None:
        calls.append(digests)

    blobs = BlobStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    blobs.cas.prepare()
    registry = ModelRegistry(engine, blobs.cas, used=used)
    doc, arts = _trained(502)
    await registry.store(_rejected(doc), arts, reference_version=None, inputs=INPUTS)
    assert calls == []  # created: the CAS observer already recorded it as used
    other = _rejected(doc)
    other["gate"]["tag"] = "again"  # re-judge: same artefacts, deduped
    await registry.store(other, arts, reference_version=None, inputs=INPUTS)
    assert [[str(d) for d in c] for c in calls] == [list(arts)]


# ------------------------------------------------------------------- N1


async def test_a_refused_value_is_not_retryable(engine: Any, tmp_path: Path) -> None:
    blobs = BlobStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    blobs.cas.prepare()
    registry = ModelRegistry(engine, blobs.cas)
    doc, arts = _trained(511)
    bad = _rejected(doc)
    bad["set_version"] = "md5:not-a-set-version"  # ck_ml_model_set_set_version
    with pytest.raises(MlRegistryError) as caught:
        await registry.store(bad, arts, reference_version=None, inputs=INPUTS)
    assert caught.value.failure_type is FailureType.INVALID_INPUT
    assert caught.value.retryable is False


# ------------------------------------------------------------ reviewer #4


def _built() -> export.BuiltDataset:
    ordered = sorted(_rows(521), key=sort_key)
    data = parquet_bytes(ordered)
    dataset = Dataset(
        rows=tuple(ordered),
        counts={"rows": len(ordered)},
        parquet=data,
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
    )
    return export.BuiltDataset(dataset=dataset, max_runs=1000, truncated=False)


def _child_writing(
    write: Callable[[Path], None],
) -> Callable[[Any, float], Any]:
    async def child(argv: Any, timeout_s: float) -> tuple[int, str]:
        out = Path(argv[list(argv).index("--out") + 1])
        await asyncio.to_thread(out.mkdir)
        await asyncio.to_thread(write, out)
        return 0, ""

    return child


def _bad_json(out: Path) -> None:
    (out / "set.json").write_text("{truncated", "utf-8")


def _missing_version(out: Path) -> None:
    (out / "set.json").write_text(
        json.dumps({"members": [{"artefact": "a.art"}]}), "utf-8"
    )


def _artefact_mismatch(out: Path) -> None:
    doc, arts = _trained(522)
    (version,) = list(arts)
    for member in doc["members"]:
        if member["algorithm"] != "none":
            member["artefact"] = "a.art"
    (out / "set.json").write_text(json.dumps(doc), "utf-8")
    (out / "a.art").write_bytes(b"not the artefact " + version.encode())


def _missing_set_version(out: Path) -> None:
    doc, arts = _trained(523)
    del doc["set_version"]
    for member in doc["members"]:
        if member["algorithm"] != "none":
            name = member["version"].split(":", 1)[1] + ".art"
            member["artefact"] = name
            (out / name).write_bytes(arts[member["version"]])
    (out / "set.json").write_text(json.dumps(doc), "utf-8")


def _refused_by_db(out: Path) -> None:
    doc, arts = _trained(524)
    doc = _rejected(doc)
    doc["set_version"] = "md5:x"
    for member in doc["members"]:
        if member["algorithm"] != "none":
            name = member["version"].split(":", 1)[1] + ".art"
            member["artefact"] = name
            (out / name).write_bytes(arts[member["version"]])
    (out / "set.json").write_text(json.dumps(doc), "utf-8")


@pytest.mark.parametrize(
    ("write", "needle"),
    [
        (_bad_json, "JSONDecodeError"),
        (_missing_version, "missing field 'version'"),
        (_artefact_mismatch, "does not match"),
        (_missing_set_version, "missing field 'set_version'"),
        (_refused_by_db, "refused by the database"),
    ],
)
async def test_malformed_child_output_is_child_output_invalid(
    engine: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    write: Callable[[Path], None],
    needle: str,
) -> None:
    built = _built()

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    blobs = BlobStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    blobs.cas.prepare()
    registry = ModelRegistry(engine, blobs.cas)
    task = MlRetrainer(
        engine,
        registry,
        ModelSource(registry, "auto"),
        PlannerSettings(ml_min_rows=100),
        child=_child_writing(write),
    )
    result = await task.retrain_now("manual")
    assert result.error == "child_output_invalid", result
    assert needle in (result.detail or ""), result.detail
    assert result.row is None
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT count(*) FROM ml_model_set"))).one()
    assert count[0] == 0
