"""V0.15 G2 on a real PostgreSQL (D353-D355).

1. **Route cache (D355)** - two concurrent admin requests to ``GET
   /v1/planner/ml/dataset`` build the dataset **once** (single flight) and
   get the same bytes; a request with nothing new reuses the build; a new
   matched run rebuilds; an attempt that ends node-caused (``lost``) after
   that rebuilds too and is counted in ``counts.node_failures`` (D354, the
   D296 predicate).
2. **End to end** - real dispatch ticks -> ``done`` attempts -> the G1
   dataset (``build_dataset`` + ``write_files``: Parquet + sidecar) ->
   ``compute planner ml compare`` as a real CLI process, which runs the real
   ``python -m meshforge.controller.ml.train`` child: exit 0, the report
   names the dataset's digest, reads the sidecar (``matched``,
   ``node_failures`` from the database), and a second run gives a
   byte-identical ``report.json``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text
from test_v015_g1_ml_dataset_pg import Manager, _dispatch_one, _finish, _node

from meshforge.controller.api import ml_routes
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.ml import export
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

PHASES = {"stage": 4_000, "env": 20_000, "run": 75_000, "collect": 200}


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


def _planner(nodes: list[str]) -> DispatchPlanner:
    return DispatchPlanner(
        manager=Manager(nodes),  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=60.0, default_env_s=30.0),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


class _Admin:
    user_id = "usr_admin"
    name = "admin"
    is_admin = True


def _app(engine: Any) -> Any:
    from fastapi import FastAPI

    from meshforge.controller.api.auth import current_user, require_admin
    from meshforge.controller.api.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ml_routes.ml_router)
    app.dependency_overrides[current_user] = _Admin
    app.dependency_overrides[require_admin] = _Admin
    setattr(app.state, ml_routes.ML_STATE_KEY, ml_routes.MlDatasetApi(engine, 100))
    return app


async def test_route_cache_single_flight_reuse_and_rebuild(
    engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ExecutionStore(engine)
    nodes = [await _node(engine) for _ in range(2)]
    planner = _planner(nodes)
    for i in range(3):
        await _finish(
            engine, await _dispatch_one(store, planner, f"{i:064x}"), i, PHASES
        )

    real = export.build_dataset
    builds: list[int] = []

    async def counted(eng: Any, *, max_runs: int) -> export.BuiltDataset:
        builds.append(max_runs)
        await asyncio.sleep(0.3)  # hold the build so the second request overlaps
        return await real(eng, max_runs=max_runs)

    monkeypatch.setattr(export, "build_dataset", counted)
    app = _app(engine)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        url = "/v1/planner/ml/dataset"
        a, b = await asyncio.gather(client.get(url), client.get(url))
        assert a.status_code == b.status_code == 200
        assert len(builds) == 1  # single flight: the second waited and reused
        assert a.content == b.content
        first = a.headers[ml_routes.DIGEST_HEADER]

        again = await client.get(url + "?format=summary")
        assert len(builds) == 1 and again.json()["digest"] == first
        assert again.json()["counts"]["node_failures"] == 0

        # a new matched run -> rebuilt, another digest
        await _finish(
            engine, await _dispatch_one(store, planner, f"{7:064x}"), 7, PHASES
        )
        newer = await client.get(url + "?format=summary")
        assert len(builds) == 2 and newer.json()["digest"] != first
        assert newer.json()["counts"]["runs"] == 4

        # a node-caused end changes no matched run, but the watermark and
        # node_failures (D354: the risk term's own definition)
        lost = await _dispatch_one(store, planner, f"{8:064x}")
        async with engine.begin() as conn:
            await conn.execute(
                text(
                    "UPDATE attempt SET state = 'lost', failure_type = 'node_lost', "
                    "ended_at = now() WHERE id = :a"
                ),
                {"a": lost},
            )
        failed = await client.get(url + "?format=summary")
        assert len(builds) == 3
        assert failed.json()["counts"]["node_failures"] == 1
        assert failed.json()["digest"] == newer.json()["digest"]  # same rows


def _cli_env(tmp_path: Path) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env["XDG_CONFIG_HOME"] = str(tmp_path / "cli-config")
    env["COLUMNS"] = "200"  # one table row per term
    env["MESHFORGE_DATA_DIR"] = str(tmp_path / "no-agent-here")
    return env


async def _compare(dataset: Path, out: Path, tmp_path: Path) -> str:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meshforge.cli",
        "planner",
        "ml",
        "compare",
        "--dataset",
        str(dataset),
        "--min-rows",
        "1",
        "--min-families",
        "1",
        "--out",
        str(out),
        env=_cli_env(tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 180)
    assert proc.returncode == 0, stderr.decode() + stdout.decode()
    return stdout.decode()


async def test_dataset_to_compare_end_to_end(engine: Any, tmp_path: Path) -> None:
    pytest.importorskip("sklearn", reason="needs the ml extra")
    store = ExecutionStore(engine)
    nodes = [await _node(engine) for _ in range(2)]
    planner = _planner(nodes)
    for i in range(12):
        phases = {**PHASES, "env": 10_000 + 3_000 * i, "run": 40_000 + 9_000 * i}
        await _finish(
            engine, await _dispatch_one(store, planner, f"{i:064x}"), i, phases
        )
    lost = await _dispatch_one(store, planner, f"{99:064x}")
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE attempt SET state = 'lost', ended_at = now() WHERE id = :a"),
            {"a": lost},
        )
    built = await export.build_dataset(engine)
    assert built.dataset.counts["node_failures"] == 1
    path, _sidecar = export.write_files(built, tmp_path / "ds.parquet")

    out = tmp_path / "report"
    printed = await _compare(path, out, tmp_path)
    doc = json.loads((out / "report.json").read_text(encoding="utf-8"))
    assert doc["dataset"]["digest"] == built.dataset.digest
    assert doc["dataset"]["rows"] == built.dataset.counts["rows"]
    assert doc["dataset"]["sidecar"] == "matched"
    assert doc["failure_classifier"] == {
        "min_events": 50,
        "node_failures": 1,
        "status": "not_trained",
    }
    by_term = {t["term"]: t for t in doc["terms"]}
    assert by_term["compute"]["rows"] == 12
    assert by_term["compute"]["status"] in ("trained", "no_learned_model")
    assert by_term["transfer"]["status"] == "insufficient_data"  # exact by construction
    assert "compute" in printed and "failure classifier: not trained" in printed

    again = tmp_path / "report2"
    await _compare(path, again, tmp_path)
    assert (again / "report.json").read_bytes() == (out / "report.json").read_bytes()
