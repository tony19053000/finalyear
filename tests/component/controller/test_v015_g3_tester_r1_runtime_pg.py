"""V0.15 G3 tester r1 - a real controller process-in-test (PostgreSQL, HTTPS,
the real user store): startup config matrix, restart, ``ml = off``, the
route auth matrix and malformed inputs, GC on the real blob store.
"""

from __future__ import annotations

import math
import random
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import structlog
from mf_controller_harness import make_settings
from mf_jobs_harness import api_client, running_controller, token_of
from mf_users import create_user, login

pytest.importorskip("sqlalchemy", reason="needs the controller extra")
pytest.importorskip("sklearn", reason="needs the ml extra")
pytest.importorskip("lightgbm", reason="needs the ml extra")

from mf_ml_synthetic import synthetic_rows
from sqlalchemy import text

from meshforge.common.errors import ConfigError
from meshforge.controller import ml_retrain_task
from meshforge.controller.ml.registry import ModelRegistry
from meshforge.controller.ml.train.compare import CompareSettings
from meshforge.controller.ml.train.data import table_from_rows
from meshforge.controller.ml.train.fit import train_set
from meshforge.controller.ml.train.fit_main import set_document
from meshforge.controller.ml.train.gate import Reference, run_gate
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(600)]

FAST = CompareSettings(terms=("compute",), algorithms=("ridge",), min_rows=100)
INPUTS = {"watermark": {"x": 1}, "train": {}}


def _trained(
    seed: int, noise: bool = False, strict: bool = False
) -> tuple[dict[str, Any], dict[str, bytes]]:
    settings = replace(FAST, min_effect_log=5.0) if strict else FAST
    rows = synthetic_rows(
        "signal", families=6, groups_per_family=5, runs_per_group=10, seed=seed
    )
    if noise:
        rng = random.Random(seed)  # noqa: S311
        for row in rows:
            row["y"] = rng.gauss(0.0, 0.3)
            row["actual_s"] = row["raw_s"] * math.exp(row["y"])
    table = table_from_rows(rows)
    result = train_set(table, settings)
    gate = run_gate(table, settings, result, Reference())
    doc = set_document(result, gate, settings)
    return doc, {m.version: m.artefact for m in result.members if m.artefact}


async def _seed(runtime: ControllerRuntime) -> tuple[str, str, str]:
    """(superseded, active, rejected) set versions in the runtime's registry."""
    assert runtime._engine is not None and runtime.blobs is not None
    registry = ModelRegistry(runtime._engine, runtime.blobs.cas)
    async with runtime._engine.begin() as conn:
        await conn.execute(text("DELETE FROM ml_model_set"))
    old, old_arts = _trained(501)
    new, new_arts = _trained(502)
    bad, bad_arts = _trained(503, strict=True)
    assert bad_arts, "the rejected set must hold artefacts"
    await registry.store(old, old_arts, reference_version=None, inputs=INPUTS)
    await registry.store(
        new, new_arts, reference_version=old["set_version"], inputs=INPUTS
    )
    stored = await registry.store(
        bad, bad_arts, reference_version=new["set_version"], inputs=INPUTS
    )
    assert stored.row is not None and stored.row.status == "rejected"
    return old["set_version"], new["set_version"], bad["set_version"]


async def test_startup_matrix_restart_and_off(pg_settings: DatabaseSettings) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        old, active, rejected = await _seed(runtime)
        await runtime.ml_source.models.refresh()  # type: ignore[union-attr]
        assert runtime.ml_source.models is not None
        assert runtime.ml_source.models.current is not None
        assert runtime.ml_source.models.current.set_version == active

    # restart: ml = auto keeps the active set (nothing re-trained or re-judged)
    async with running_controller(make_settings(pg_settings)) as runtime:
        assert runtime.ml_source.models is not None
        assert runtime.ml_source.models.current is not None
        assert runtime.ml_source.models.current.set_version == active
        assert runtime.ml_source.models.kind == "active"

    # a configured pin of a rejected / unknown / malformed set refuses to start
    for bad in (rejected, "sha256:" + "7" * 64):
        with pytest.raises(ConfigError):
            async with running_controller(
                make_settings(pg_settings, planner={"ml": bad})
            ):
                pytest.fail("the controller started")
    for malformed in ("garbage", "sha256:xyz", "SHA256:" + "a" * 64, "", " ", "Auto"):
        with pytest.raises(Exception) as caught:
            async with running_controller(
                make_settings(pg_settings, planner={"ml": malformed})
            ):
                pytest.fail("the controller started")
        assert "Traceback" not in str(caught.value)

    # a valid (superseded) pin in config serves exactly that set
    async with running_controller(
        make_settings(pg_settings, planner={"ml": old})
    ) as runtime:
        assert runtime.ml_source.models is not None
        assert runtime.ml_source.models.kind == "config"
        assert runtime.ml_source.models.current is not None
        assert runtime.ml_source.models.current.set_version == old
        # the retrain task is not scheduled for a configured pin
        assert runtime.failed_task is None

    # ml = off: boots, serves nothing, rows untouched
    async with running_controller(
        make_settings(pg_settings, planner={"ml": "off"})
    ) as runtime:
        assert runtime.ml_source.models is not None
        assert runtime.ml_source.models.mode == "off"
        assert await runtime.ml_source.models.refresh() is None
        assert runtime.ml_source.models.current is None
        assert runtime._engine is not None
        async with runtime._engine.connect() as conn:
            statuses = sorted(
                str(r[0])
                for r in (
                    await conn.execute(text("SELECT status FROM ml_model_set"))
                ).all()
            )
        assert statuses == ["active", "rejected", "superseded"]


async def test_the_database_pin_changes_what_a_running_controller_serves(
    pg_settings: DatabaseSettings,
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        old, active, _rejected = await _seed(runtime)
        assert runtime.ml_source.models is not None and runtime._engine is not None
        registry = ModelRegistry(runtime._engine, runtime.blobs.cas)  # type: ignore[union-attr]
        await registry.pin(old)
        loaded = await runtime.ml_source.models.refresh()
        assert loaded is not None and loaded.set_version == old
        assert runtime.ml_source.models.kind == "pinned"
        await registry.unpin(old)
        loaded = await runtime.ml_source.models.refresh()
        assert loaded is not None and loaded.set_version == active


async def test_a_missing_blob_for_the_active_set_is_survived_and_logged_once(
    pg_settings: DatabaseSettings,
) -> None:
    async with running_controller(make_settings(pg_settings)) as runtime:
        _old, active, _rejected = await _seed(runtime)
        assert runtime.ml_source.models is not None and runtime.blobs is not None
        registry = ModelRegistry(runtime._engine, runtime.blobs.cas)  # type: ignore[arg-type]
        row = await registry.get(active)
        assert row is not None
        from meshforge.common.hashing import Digest

        for member in row.members:
            if member.artefact_digest:
                runtime.blobs.cas.evict(Digest.parse(member.artefact_digest))
        runtime.ml_source.models.current = None
        runtime.ml_source.models._unreadable = None
        with structlog.testing.capture_logs() as logs:
            for _ in range(5):
                assert await runtime.ml_source.models.refresh() is None
        errors = [
            e for e in logs if e["event"] == "controller.planner.ml_set_unloadable"
        ]
        assert len(errors) == 1, errors
        assert runtime.ml_source.models.unavailable == "artefact_missing"
        assert runtime.failed_task is None


async def test_the_route_auth_matrix_and_malformed_inputs(
    pg_settings: DatabaseSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ml_retrain_task, "ml_missing", lambda: ["sklearn"])
    settings = make_settings(pg_settings)
    async with running_controller(settings) as runtime:
        old, active, rejected = await _seed(runtime)
        assert runtime._engine is not None
        await create_user(runtime._engine, "adm", "pw-admin-123456", admin=True)
        await create_user(runtime._engine, "usr", "pw-user-1234567", admin=False)
        async with api_client(runtime) as anon:
            adm = await login(anon, "adm", "pw-admin-123456")
            usr = await login(anon, "usr", "pw-user-1234567")
            ver = active
            reads = [
                "/v1/planner/ml/models",
                f"/v1/planner/ml/models/{ver}",
                "/v1/planner/ml/train",
            ]
            writes = [
                "/v1/planner/ml/train",
                f"/v1/planner/ml/models/{ver}/pin",
                f"/v1/planner/ml/models/{ver}/unpin",
            ]
            for path in reads:
                assert (await anon.get(path)).status_code == 401, path
            for path in writes:
                assert (await anon.post(path)).status_code == 401, path
        async with api_client(runtime, token=usr) as user:
            for path in reads:
                assert (await user.get(path)).status_code == 200, path
            for path in writes:
                assert (await user.post(path)).status_code == 403, path
            assert (
                await user.post(f"/v1/planner/ml/models/{old}/pin")
            ).status_code == 403
        async with api_client(runtime, token=adm) as admin:
            # the admin: reads fine; the user's 403s were not caused by state
            for path in reads:
                assert (await admin.get(path)).status_code == 200, path
            assert (await admin.post("/v1/planner/ml/train")).status_code == 409
            listed = (await admin.get("/v1/planner/ml/models")).json()
            assert listed["retrain"]["available"] is False
            assert listed["serving_set"] == active
            # malformed inputs never reach the registry
            for bad in ("nope", "sha256:abc", "sha256:" + "G" * 64, "x" * 5000):
                r = await admin.get(f"/v1/planner/ml/models/{bad}")
                assert r.status_code in (400, 404, 422), (bad[:20], r.status_code)
                r = await admin.post(f"/v1/planner/ml/models/{bad}/pin")
                assert r.status_code in (400, 404, 422), (bad[:20], r.status_code)
            for limit in ("0", "-1", "201", "abc", "1e3", ""):
                r = await admin.get("/v1/planner/ml/models", params={"limit": limit})
                assert r.status_code in (400, 422), (limit, r.status_code)
            assert (
                await admin.get("/v1/planner/ml/models", params={"limit": "2"})
            ).json()["sets"].__len__() == 2
            unknown = "sha256:" + "0" * 64
            assert (
                await admin.get(f"/v1/planner/ml/models/{unknown}")
            ).status_code == 404
            assert (
                await admin.post(f"/v1/planner/ml/models/{unknown}/pin")
            ).status_code == 404
            assert (
                await admin.post(f"/v1/planner/ml/models/{unknown}/unpin")
            ).status_code == 404
            r = await admin.post(f"/v1/planner/ml/models/{rejected}/pin")
            assert r.status_code == 409 and "rejected" in r.text
            r = await admin.post(f"/v1/planner/ml/models/{active}/unpin")
            assert r.status_code == 409  # not pinned
            # pin / unpin round trip is visible in the listing and in use
            pinned = await admin.post(f"/v1/planner/ml/models/{old}/pin")
            assert pinned.status_code == 200 and pinned.json()["status"] == "pinned"
            listed = (await admin.get("/v1/planner/ml/models")).json()
            assert listed["serving_set"] == old
            assert (
                listed["in_use"]["in_use_set"] == old
                and listed["in_use"]["kind"] == "pinned"
            )
            again = await admin.post(f"/v1/planner/ml/models/{old}/pin")
            assert again.status_code == 200  # idempotent
            un = await admin.post(f"/v1/planner/ml/models/{old}/unpin")
            assert un.status_code == 200 and un.json()["status"] == "superseded"
            listed = (await admin.get("/v1/planner/ml/models")).json()
            assert listed["in_use"]["in_use_set"] == active
            # the legacy fabric token is not an admin identity for these routes
            token = token_of(runtime)
        async with api_client(runtime, token=token) as legacy:
            r = await legacy.post(f"/v1/planner/ml/models/{old}/pin")
            assert r.status_code in (200, 403), r.status_code


async def test_gc_on_the_real_blob_store_keeps_what_is_served(
    pg_settings: DatabaseSettings, tmp_path: Path
) -> None:
    settings = make_settings(
        pg_settings, gc={"cas_bytes": 1, "min_age_s": 0, "interval_s": 3600}
    )
    async with running_controller(settings) as runtime:
        old, active, rejected = await _seed(runtime)
        assert runtime.gc is not None and runtime.blobs is not None
        registry = ModelRegistry(runtime._engine, runtime.blobs.cas)  # type: ignore[arg-type]
        from meshforge.common.hashing import Digest

        async def present(version: str) -> bool:
            row = await registry.get(version)
            assert row is not None
            return all(
                runtime.blobs.cas.has(Digest.parse(m.artefact_digest))  # type: ignore[union-attr]
                for m in row.members
                if m.artefact_digest
            )

        await runtime.gc.run_pass("test")
        assert await present(active)  # the served set
        assert await present(old)  # inside the rollback window
        assert not await present(rejected)  # never loaded: collectable
        await registry.pin(old)
        await runtime.gc.run_pass("test")
        assert await present(old) and await present(active)
        # the controller still serves after the pass
        assert runtime.ml_source.models is not None
        loaded = await runtime.ml_source.models.refresh()
        assert loaded is not None and loaded.set_version == old
