"""V0.15 G3 on a real PostgreSQL + a real CAS (D339, D358-D361).

* **store** - an accepted set becomes ``active`` (the old one
  ``superseded``), a rejected one is stored ``rejected`` with the gate's
  reason, the same set twice is ``same_set``, a set gated against a
  reference that no longer serves is ``reference_changed`` (nothing
  written); the artefacts land in the CAS under their digest;
* **one active** under two concurrent activations;
* **pin / unpin** - a rejected set and a set whose artefacts are gone are
  refused; pin overrides ``auto``; unpin restores; GC keeps the serving and
  rollback-window artefacts, not a rejected set's;
* **restart keeps the active set** (a new registry + source on a fresh
  engine load it); a configured pin of a rejected / unknown set refuses to
  start; ``ml = off`` serves nothing;
* **the retrain task end to end** (a real ``fit`` child, the dataset build
  stubbed with a synthetic one): a set is stored and served; a second run
  with an unchanged watermark is ``no_new_runs``; a new run record moves
  the watermark and the same data gives the same set (``same_set``); two
  requests at once -> one ``already_running``; a child that overruns is
  killed (``timeout``), nothing stored; a controller without the extra is
  ``ml_unavailable``;
* **the API** - list, detail, pin of a rejected set -> 409, train without
  the extra -> 409.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import random
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from alembic import command
from fastapi import FastAPI
from mf_ml_synthetic import synthetic_rows
from sqlalchemy import text

from meshforge.common.cas import CasStore
from meshforge.common.errors import ConfigError
from meshforge.common.hashing import Digest
from meshforge.controller.api import auth, ml_model_routes
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.ml import export
from meshforge.controller.ml.dataset import Dataset, parquet_bytes, sort_key
from meshforge.controller.ml.registry import (
    ModelRegistry,
    PinError,
    protected_artefacts,
)
from meshforge.controller.ml.source import ModelSource
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fit_main import set_document
from meshforge.controller.ml.train.gate import Reference, run_gate
from meshforge.controller.ml_retrain_task import MlRetrainer, run_child
from meshforge.controller.settings import PlannerSettings
from meshforge.controller.store.engine import create_engine, create_session_factory
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
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def cas(tmp_path: Path) -> CasStore:
    store = CasStore(tmp_path / "cas", max_blob_bytes=1 << 30)
    store.prepare()
    return store


def _rows(seed: int, kind: str = "signal") -> list[dict[str, Any]]:
    rows = synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=10, seed=seed
    )
    if kind == "noise":
        rng = random.Random(seed)  # noqa: S311 - reproducible test data
        for row in rows:
            row["y"] = rng.gauss(0.0, 0.3)
            row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    return rows


def _trained(
    seed: int, kind: str = "signal", reference: Reference | None = None
) -> tuple[dict[str, Any], dict[str, bytes]]:
    table = table_from_rows(_rows(seed, kind))
    result = train_set(table, FAST)
    gate = run_gate(table, FAST, result, reference or Reference())
    doc = set_document(result, gate, FAST)
    artefacts = {m.version: m.artefact for m in result.members if m.artefact}
    return doc, artefacts


async def _statuses(engine: Any) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = (
            await conn.execute(text("SELECT set_version, status FROM ml_model_set"))
        ).all()
    return {str(v): str(s) for v, s in rows}


async def test_store_activates_supersedes_rejects_and_skips(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    first, arts1 = _trained(1)
    assert first["accepted"] is True
    stored = await registry.store(first, arts1, reference_version=None, inputs=INPUTS)
    assert stored.row is not None and stored.row.status == "active"
    assert stored.superseded is None
    (member,) = stored.row.members
    assert member.artefact_digest == member.version
    assert cas.has(Digest.parse(member.version))  # the artefact is in the CAS
    assert (await registry.serving_version()) == ("active", first["set_version"])

    second, arts2 = _trained(2)
    stored2 = await registry.store(
        second, arts2, reference_version=first["set_version"], inputs=INPUTS
    )
    assert stored2.row is not None and stored2.row.status == "active"
    assert stored2.superseded == first["set_version"]

    noise, arts3 = _trained(3, "noise")
    assert noise["accepted"] is False
    stored3 = await registry.store(
        noise, arts3, reference_version=second["set_version"], inputs=INPUTS
    )
    assert stored3.row is not None and stored3.row.status == "rejected"
    assert (stored3.row.reason or "").startswith("not_better")
    # V0.15 G5a (D371): the child never writes pending_g4 again
    assert stored3.row.gate["regret"]["status"] == "not_run"
    assert stored3.row.inputs["watermark"] == {"x": 1}

    again = await registry.store(
        second, arts2, reference_version=second["set_version"], inputs=INPUTS
    )
    assert again.skipped == "same_set" and again.row is None
    stale, arts4 = _trained(4)
    changed = await registry.store(
        stale, arts4, reference_version=first["set_version"], inputs=INPUTS
    )
    assert changed.skipped == "reference_changed" and changed.row is None
    assert await _statuses(engine) == {
        first["set_version"]: "superseded",
        second["set_version"]: "active",
        noise["set_version"]: "rejected",
    }
    # an artefact that does not hash to its member's version is refused
    bad = dict(arts4)
    key = next(iter(bad))
    bad[key] = bad[key][:-1] + b" "
    with pytest.raises(ValueError, match="does not match"):
        await registry.store(stale, bad, reference_version=None, inputs=INPUTS)


async def test_one_active_set_under_concurrent_activations(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    docs = [_trained(seed) for seed in (11, 12, 13)]
    results = await asyncio.gather(
        *(
            registry.store(doc, arts, reference_version=None, inputs=INPUTS)
            for doc, arts in docs
        )
    )
    written = [r for r in results if r.row is not None]
    assert len(written) == 1, results
    assert all(r.skipped == "reference_changed" for r in results if r.row is None)
    statuses = await _statuses(engine)
    assert list(statuses.values()).count("active") == 1


async def test_pin_unpin_rollback_and_gc_protection(engine: Any, cas: CasStore) -> None:
    registry = ModelRegistry(engine, cas)
    first, arts1 = _trained(21)
    await registry.store(first, arts1, reference_version=None, inputs=INPUTS)
    second, arts2 = _trained(22)
    await registry.store(
        second, arts2, reference_version=first["set_version"], inputs=INPUTS
    )
    noise, arts3 = _trained(23, "noise")
    await registry.store(
        noise, arts3, reference_version=second["set_version"], inputs=INPUTS
    )
    with pytest.raises(PinError, match="rejected"):
        await registry.pin(noise["set_version"])
    with pytest.raises(PinError):
        await registry.pin("sha256:" + "0" * 64)
    # rollback: pin the superseded first set
    pinned = await registry.pin(first["set_version"])
    assert pinned.status == "pinned"
    assert (await registry.serving_version()) == ("pinned", first["set_version"])
    source = ModelSource(registry, "auto")
    loaded = await source.refresh()
    assert loaded is not None and loaded.set_version == first["set_version"]
    assert source.kind == "pinned"
    # a new retrain is now judged against the pinned set
    third, arts4 = _trained(24)
    refused = await registry.store(
        third, arts4, reference_version=second["set_version"], inputs=INPUTS
    )
    assert refused.skipped == "reference_changed"
    # GC: serving + rollback window kept, the rejected set's artefacts not
    async with create_session_factory(engine)() as db:
        keep = await protected_artefacts(db, 5)
        keep_none = await protected_artefacts(db, 0)
    hexes = {
        name: {v.split(":", 1)[1] for v in arts}
        for name, arts in (("first", arts1), ("second", arts2), ("noise", arts3))
    }
    assert hexes["first"] <= keep and hexes["second"] <= keep
    assert not (hexes["noise"] & keep)
    assert hexes["first"] <= keep_none and hexes["second"] <= keep_none  # pinned+active
    unpinned = await registry.unpin(first["set_version"])
    assert unpinned.status == "superseded"  # second is active
    assert (await registry.serving_version()) == ("active", second["set_version"])
    with pytest.raises(PinError, match="not pinned"):
        await registry.unpin(first["set_version"])
    async with create_session_factory(engine)() as db:
        assert not (hexes["first"] & await protected_artefacts(db, 0))
    # a set whose artefacts are gone cannot be pinned
    for version in arts1:
        cas.evict(Digest.parse(version))
    with pytest.raises(PinError) as caught:
        await registry.pin(first["set_version"])
    assert caught.value.details["reason"] == "artefact_missing"


async def test_restart_keeps_the_active_set_and_config_pins_are_checked(
    engine: Any, cas: CasStore, pg_settings: DatabaseSettings
) -> None:
    registry = ModelRegistry(engine, cas)
    doc, arts = _trained(31)
    await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
    noise, arts_n = _trained(32, "noise")
    await registry.store(
        noise, arts_n, reference_version=doc["set_version"], inputs=INPUTS
    )
    # "restart": a new engine, registry and source
    fresh = create_engine(pg_settings, pool_size=2)
    try:
        again = ModelRegistry(fresh, cas)
        source = ModelSource(again, "auto")
        await source.check()
        loaded = await source.refresh()
        assert loaded is not None and loaded.set_version == doc["set_version"]
        model = loaded.model("compute")
        assert model is not None
        table = table_from_rows(_rows(31))
        out = model.predict(table.X[:5])
        assert out.y_hat.shape == (5,)
        assert out.model_version == doc["members"][0]["version"]
        # configured pins: a rejected or unknown set refuses to start
        for bad in (noise["set_version"], "sha256:" + "9" * 64):
            with pytest.raises(ConfigError):
                await ModelSource(again, bad).check()
        pinned = ModelSource(again, doc["set_version"])
        await pinned.check()
        assert (await pinned.refresh()) is not None and pinned.kind == "config"
        off = ModelSource(again, "off")
        assert await off.refresh() is None
    finally:
        await fresh.dispose()


def _built(rows: list[dict[str, Any]]) -> export.BuiltDataset:
    ordered = sorted(rows, key=sort_key)
    data = parquet_bytes(ordered)
    dataset = Dataset(
        rows=tuple(ordered),
        counts={"rows": len(ordered)},
        parquet=data,
        digest="sha256:" + hashlib.sha256(data).hexdigest(),
    )
    return export.BuiltDataset(dataset=dataset, max_runs=1000, truncated=False)


def _planner(**over: Any) -> PlannerSettings:
    return PlannerSettings(ml_min_rows=100, **over)


async def test_retrain_task_end_to_end(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    built = _built(_rows(41))

    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return built

    monkeypatch.setattr(export, "build_dataset", fake_build)
    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")
    task = MlRetrainer(engine, registry, source, _planner())
    assert task.available
    first = await task.retrain_now("manual")
    assert first.error is None, first
    assert first.row is not None and first.row.status == "active", first
    assert source.current is not None
    assert source.current.set_version == first.row.set_version
    assert first.row.inputs["dataset"]["digest"] == built.dataset.digest
    assert "watermark" in first.row.inputs and "total_s" in first.seconds
    # nothing changed -> skipped, also for a new task (the stored watermark)
    assert (await task.retrain_now("scheduled")).skipped == "no_new_runs"
    other = MlRetrainer(engine, registry, source, _planner())
    assert (await other.retrain_now("scheduled")).skipped == "no_new_runs"
    # a new run record moves the watermark; the same data -> the same set
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO run_record (schema_version, attempt_id, job_id, "
                "attempt_no, node_id, strategy, placement_mode, attempt_state, "
                "failure_class, attempt_created_at) VALUES (3, 'att_x', 'job_x', 1, "
                "'node_x', 'mode1', 'auto', 'done', 'ok', now())"
            )
        )
    third = await task.retrain_now("scheduled")
    assert third.skipped == "same_set", third  # byte-identical set version
    # two requests at once: one runs, the other is told so
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE run_record SET recorded_at = now() + interval '1 s'")
        )
    a, b = await asyncio.gather(task.retrain_now(), task.retrain_now())
    assert {a.skipped, b.skipped} == {"same_set", "already_running"}


async def test_retrain_timeout_and_child_failure_store_nothing(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_build(_engine: Any, *, max_runs: int) -> export.BuiltDataset:
        return _built(_rows(51))

    monkeypatch.setattr(export, "build_dataset", fake_build)
    registry = ModelRegistry(engine, cas)
    source = ModelSource(registry, "auto")

    async def overrun(argv: Any, timeout_s: float) -> tuple[int, str]:
        raise TimeoutError

    async def crash(argv: Any, timeout_s: float) -> tuple[int, str]:
        return 1, "Traceback ...\nthe training failed (ValueError: boom)"

    timed = await MlRetrainer(
        engine, registry, source, _planner(), child=overrun
    ).retrain_now()
    assert timed.error == "timeout" and timed.row is None
    failed = await MlRetrainer(
        engine, registry, source, _planner(), child=crash
    ).retrain_now()
    assert failed.error == "child_failed" and "boom" in (failed.detail or "")
    assert await _statuses(engine) == {}


def _alive(pid_file: Path) -> bool:
    stat = Path(f"/proc/{int(pid_file.read_text())}/stat")
    return stat.exists() and stat.read_text().split()[2] != "Z"


async def test_run_child_kills_an_overrunning_child(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        await run_child(
            [
                sys.executable,
                "-c",
                f"import os, time; open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
                "time.sleep(60)",
            ],
            2.0,
        )
    assert time.monotonic() - started < 20
    assert not await asyncio.to_thread(_alive, pid_file)


async def test_without_the_ml_extra_the_task_never_trains(
    engine: Any, cas: CasStore
) -> None:
    registry = ModelRegistry(engine, cas)
    task = MlRetrainer(engine, registry, ModelSource(registry, "auto"), _planner())
    task.missing = ["sklearn"]
    await asyncio.wait_for(task.run(), 5)  # logs once and returns
    result = await task.retrain_now()
    assert result.skipped == "ml_unavailable"
    assert await _statuses(engine) == {}


class _P:
    def __init__(self, admin: bool) -> None:
        self.user_id = "usr_t"
        self.name = "t"
        self.is_admin = admin


async def test_the_api(
    engine: Any, cas: CasStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = ModelRegistry(engine, cas)
    doc, arts = _trained(61)
    await registry.store(doc, arts, reference_version=None, inputs=INPUTS)
    noise, arts_n = _trained(62, "noise")
    await registry.store(
        noise, arts_n, reference_version=doc["set_version"], inputs=INPUTS
    )
    source = ModelSource(registry, "auto")
    await source.refresh()
    task = MlRetrainer(engine, registry, source, _planner())
    task.missing = ["lightgbm"]
    principal: list[Any] = [_P(admin=True)]

    async def authenticate(_request: Any) -> Any:
        return principal[0]

    monkeypatch.setattr(auth, "authenticate", authenticate)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ml_model_routes.ml_models_router)
    setattr(
        app.state,
        ml_model_routes.ML_MODELS_STATE_KEY,
        ml_model_routes.MlModelsApi(registry=registry, source=source, retrainer=task),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        listed = (await client.get("/v1/planner/ml/models")).json()
        assert listed["serving_set"] == doc["set_version"]
        assert listed["in_use"]["in_use_set"] == doc["set_version"]
        assert [s["status"] for s in listed["sets"]] == ["rejected", "active"]
        assert listed["retrain"]["available"] is False
        detail = await client.get(f"/v1/planner/ml/models/{noise['set_version']}")
        assert detail.status_code == 200
        assert detail.json()["gate"]["rule"] == "not_better"
        missing = await client.get("/v1/planner/ml/models/sha256:" + "0" * 64)
        assert missing.status_code == 404
        malformed = await client.get("/v1/planner/ml/models/nope")
        assert malformed.status_code == 400  # the error handlers answer 400
        refused = await client.post(f"/v1/planner/ml/models/{noise['set_version']}/pin")
        assert refused.status_code == 409
        assert "rejected" in refused.json()["error"]["message"]
        train = await client.post("/v1/planner/ml/train")
        assert train.status_code == 409
        assert "ml extra" in train.json()["error"]["message"]
        pinned = await client.post(f"/v1/planner/ml/models/{doc['set_version']}/pin")
        assert pinned.status_code == 200 and pinned.json()["status"] == "pinned"
        assert source.kind == "pinned"
        principal[0] = _P(admin=False)
        forbidden = await client.post(
            f"/v1/planner/ml/models/{doc['set_version']}/unpin"
        )
        assert forbidden.status_code == 403
        assert (await client.get("/v1/planner/ml/train")).status_code == 200
        principal[0] = _P(admin=True)
        unpinned = await client.post(
            f"/v1/planner/ml/models/{doc['set_version']}/unpin"
        )
        assert unpinned.json()["status"] == "active"
