"""V0.15 G1 (D333-D335) on a real PostgreSQL, end to end.

1. **Whole jobs** - real dispatch ticks (``ExecutionStore.dispatch_planned``
   + ``DispatchPlanner``) write ``plan_decision`` rows with replays; the
   attempts end ``done`` with measured phases. The dataset's rows equal a
   hand computation from the stored rows (``y = ln(actual / raw)`` per term),
   every skip is counted (the 0-byte transfer of a held bundle is exact by
   construction, D345; an output nobody recorded is predicted 0 s), a decision whose replay is gone
   is skipped once and counted (never imputed), and the dataset's runs are
   exactly the calibration's matched runs (one shared read, D333).
2. **A Mode 3 job** through the real controller (dispatcher, gRPC sessions,
   three fake lanes, the combine): its lane, ``chunk_rate`` and ``combine``
   rows equal a hand computation from the decision, the lane attempts' phases
   and the ``chunk_record`` rows; then **API == CLI == function**: the
   Parquet bytes of ``GET /v1/planner/ml/dataset``, of ``compute planner ml
   dataset --out`` (a real CLI process) and of :func:`build_dataset` are
   identical, and so are the summaries / the sidecar.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import math
import os
import sys
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_fabric import NodeChannels, ca_file
from mf_fake_lanes import FakeLane
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from mf_session_client import FakeAgent

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("pyarrow", reason="needs the controller extra")

from alembic import command
from sqlalchemy import text

from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.common.proto.meshforge.v1 import agent_pb2
from meshforge.common.states import JobState
from meshforge.common.tree import build_tree
from meshforge.controller.calibration_store import CalibrationStore
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.ml.export import build_dataset, summary
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import (
    JobApiSettings,
    LivenessSettings,
    PlannerSettings,
)
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(240)]

MANIFEST = {"name": "g1", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "cd" * 32
COMPUTE_S = 60.0
ENV_S = 30.0
GIB = 1024**3
HB_S = 0.2


# ======================================================== 1. whole jobs


@dataclass
class Live:
    node_id: str
    session_id: str
    state: NodeState = NodeState.ONLINE
    rtt_ewma_ms: float | None = 1.0
    policy: Any = None
    policy_slots: Any = None
    clock: Any = field(default_factory=lambda: SimpleNamespace(latest=None))


class Manager:
    def __init__(self, nodes: list[str]) -> None:
        self.lives = {n: Live(n, str(new_session_id())) for n in nodes}

    def get(self, node_id: str) -> Live | None:
        return self.lives.get(node_id)

    def get_lingering(self, node_id: str) -> None:
        return None


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[Any]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    eng = create_engine(pg_settings, pool_size=5)
    try:
        yield eng
    finally:
        await eng.dispose()


async def _sql(engine: Any, statement: str, **params: Any) -> list[Any]:
    async with engine.begin() as conn:
        result = await conn.execute(text(statement), params)
        return list(result.all()) if result.returns_rows else []


async def _node(engine: Any) -> str:
    node = str(new_node_id())
    await Registry(engine).upsert_node_on_hello(
        node_id=node,
        hostname="h",
        os="linux",
        platform="x86_64",
        hardware={"capabilities": ["exec", "exec-subprocess"]},
        session_epoch=1,
        session_id=str(new_session_id()),
        state=NodeState.ONLINE,
        hardware_scores=None,
        cpu_slots=8,
        gpu_slots=0,
    )
    return node


async def _dispatch_one(
    store: ExecutionStore, planner: DispatchPlanner, fingerprint: str
) -> str:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g1",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": None},
        request_hash="h" * 64,
        idempotency_key=None,
        scheduling=JobScheduling(fingerprint=fingerprint, input_bytes=0),
    )
    results = await store.dispatch_planned(
        planner.decide,
        limit=10,
        max_per_node=64,
        now=dt.datetime.now(dt.UTC),
        aging_s=600.0,
    )
    (result,) = [r for r in results if r.job.id == job.id]
    assert result.attempt is not None
    return result.attempt.id


async def _phases(engine: Any, attempt_id: str, phases: dict[str, int | None]) -> None:
    await _sql(engine, "DELETE FROM attempt_phase WHERE attempt_id = :a", a=attempt_id)
    for phase, ms in phases.items():
        await _sql(
            engine,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
            "skipped) VALUES (:a, :p, now(), :ms, :s)",
            a=attempt_id,
            p=phase,
            ms=0 if ms is None else ms,
            s=ms is None,
        )


async def _finish(
    engine: Any, attempt_id: str, at: int, phases: dict[str, int | None]
) -> None:
    await _sql(
        engine,
        "UPDATE attempt SET state = 'done', ended_at = timestamptz "
        "'2026-10-22 00:00:00+00' + make_interval(secs => :at) WHERE id = :a",
        a=attempt_id,
        at=at,
    )
    await _phases(engine, attempt_id, phases)


async def test_whole_jobs_rows_equal_the_hand_computed_residuals(engine: Any) -> None:
    store = ExecutionStore(engine)
    nodes = [await _node(engine) for _ in range(2)]
    planner = DispatchPlanner(
        manager=Manager(nodes),  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=COMPUTE_S, default_env_s=ENV_S),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )
    measured = [(12_000, 90_000), (45_000, 30_000), (30_000, 61_000), (5_000, 120_000)]
    attempts: list[str] = []
    for i, (env_ms, run_ms) in enumerate(measured):
        attempt = await _dispatch_one(store, planner, f"{i:064x}")
        await _finish(
            engine,
            attempt,
            i,
            {"stage": 4_000, "env": env_ms, "run": run_ms, "collect": 200},
        )
        attempts.append(attempt)
    # a fifth run whose replay is gone (a pre-V0.14 decision, an over-cap replay)
    lost = await _dispatch_one(store, planner, f"{99:064x}")
    await _finish(engine, lost, 10, {"stage": 1, "env": 1, "run": 1, "collect": 1})
    await _sql(
        engine,
        "UPDATE plan_decision SET replay = NULL, replay_version = NULL "
        "WHERE attempt_id = :a",
        a=lost,
    )

    built = await build_dataset(engine)
    data = built.dataset
    got = {(r["attempt_id"], r["term"]): r for r in data.rows}

    # the hand computation, from the stored rows
    expected: dict[tuple[str, str], float] = {}
    for attempt, (env_ms, run_ms) in zip(attempts, measured, strict=True):
        (stored,) = await _sql(
            engine,
            "SELECT candidates, chosen_node_id FROM plan_decision WHERE attempt_id = :a",
            a=attempt,
        )
        row = next(
            c
            for c in stored.candidates
            if c["rank"] == 1 and c["node_id"] == stored.chosen_node_id
        )
        terms = row["estimate"]["terms"]
        assert terms["transfer"]["detail"]["missing_bytes"] == 0  # nothing to move
        assert terms["output"]["seconds"] == 0.0  # no output history
        # a fresh fingerprint each: compute is the assumed default; env is the
        # assumed default first, then the env-build history of the earlier runs
        assert terms["compute"]["seconds"] == COMPUTE_S
        assert terms["env"]["source"] in ("assumed", "history")
        env_raw = terms["env"]["seconds"]
        assert env_raw > 0.05
        expected[(attempt, "env")] = math.log(env_ms / 1000.0 / env_raw)
        expected[(attempt, "compute")] = math.log(run_ms / 1000.0 / COMPUTE_S)
    first = await _sql(
        engine,
        "SELECT candidates FROM plan_decision WHERE attempt_id = :a",
        a=attempts[0],
    )
    first_env = next(c for c in first[0].candidates if c["rank"] == 1)
    assert first_env["estimate"]["terms"]["env"]["seconds"] == ENV_S
    assert set(got) == set(expected)
    for key, y in expected.items():
        assert got[key]["y"] == y, key
        assert got[key]["group"] == f"{attempts.index(key[0]):064x}"
        assert got[key]["family"] is None  # no run record family
    counts = data.counts
    assert counts["runs"] == 5 and counts["runs_with_rows"] == 4
    assert counts["skipped"]["missing_replay"] == 1
    assert counts["skipped"]["exact_by_construction"] == 4  # each 0-byte transfer
    assert counts["skipped"]["raw_not_positive"] == 4  # each unknown output
    assert built.truncated is False

    # D333: the dataset reads the calibration's own matched runs
    matched = await CalibrationStore(engine).matched_runs(CalibrationConfig())
    pairs = {(m.decision_id, m.attempt_id) for m in matched}
    in_rows = {(r["decision_id"], r["attempt_id"]) for r in data.rows}
    # the same five pairs; the one without a replay is the only one with no row
    assert len(pairs) == 5
    assert pairs - in_rows == {
        (m.decision_id, lost) for m in matched if m.attempt_id == lost
    }
    assert in_rows <= pairs

    # the cap: one run read -> truncated, the newest one only
    small = await build_dataset(engine, max_runs=1)
    assert small.truncated is True and small.dataset.counts["runs"] == 1


# ======================================================== 2. Mode 3 + API/CLI


@pytest_asyncio.fixture
async def runtime(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(
        pg_settings,
        jobs={"dispatch_interval_s": 0.1},
        split={"visibility_timeout_s": 5.0, "wait_retry_s": 0.2},
        planner={"default_env_s": ENV_S},
        liveness=LivenessSettings(
            heartbeat_interval_s=HB_S, miss_allowance_s=0.1, lease_multiplier=3
        ),
    )
    async with running_controller(settings) as rt:
        yield rt


@contextlib.asynccontextmanager
async def _lanes(
    runtime: ControllerRuntime, speeds: Sequence[float]
) -> AsyncIterator[list[FakeLane]]:
    async with NodeChannels(f"127.0.0.1:{runtime.grpc_port}") as channel:
        lanes: list[FakeLane] = []
        try:
            for speed in speeds:
                fake = FakeAgent(channel)
                await fake.connect(
                    hardware=agent_pb2.HardwareSummary(
                        cpu_model="test",
                        cpu_cores=4,
                        cpu_threads=8,
                        ram_bytes=16 * GIB,
                        capabilities=["exec", "exec-subprocess", "exec-split"],
                    ),
                    max_cpu_jobs=2,
                )
                fake.start_heartbeats(HB_S / 2)
                lane = FakeLane(fake, runtime, speed_s=speed, measured=True)
                lane.start()
                lanes.append(lane)
            yield lanes
        finally:
            for lane in lanes:
                await lane.close()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(lane.agent.close(), 10)


async def _submit(runtime: ControllerRuntime, tmp_path: Path) -> str:
    root = tmp_path / "p"
    root.mkdir()
    (root / "main.py").write_bytes(b"print(1)\n")
    built = build_tree(root)
    assert runtime.blobs is not None
    runtime.blobs.cas.put_bytes(built.manifest, built.tree.tree_id)
    for path in built.sources.values():
        runtime.blobs.cas.put_file(path)
    async with api_client(runtime, token=token_of(runtime)) as http:
        got: httpx.Response = await http.post(
            "/v1/jobs",
            json={
                "manifest": {
                    "name": "s",
                    "entrypoint": "python main.py {items_file} {out}",
                    "split": {
                        "over": {"range": {"stop": 300}},
                        "chunk": {"items": 10},
                        "speculative": False,
                    },
                },
                "tree_id": str(built.tree.tree_id),
                "placement": {},
            },
        )
    assert got.status_code == 201, got.text
    return str(got.json()["job_id"])


async def _wait_done(runtime: ControllerRuntime, job_id: str) -> None:
    assert runtime.jobs is not None
    for _ in range(2400):
        job = await runtime.jobs.get_job(job_id)
        assert job is not None
        if job.state in (JobState.SUCCEEDED, JobState.FAILED):
            break
        await asyncio.sleep(0.05)
    assert job.state is JobState.SUCCEEDED, (job.failure_type, job.failure_detail)


async def _settled(engine: Any, job_id: str) -> None:
    """Every terminal attempt of the job has its run record (the dataset
    reads family / fingerprint through it: built before the recorder wrote
    them, two builds could differ)."""
    for _ in range(400):
        rows = await _sql(
            engine,
            "SELECT count(*) AS n, count(r.attempt_id) AS rec FROM attempt a "
            "LEFT JOIN run_record r ON r.attempt_id = a.id WHERE a.job_id = :j",
            j=job_id,
        )
        if rows[0].n == rows[0].rec:
            return
        await asyncio.sleep(0.05)
    raise AssertionError("run records were never written")


def _cli_env(runtime: ControllerRuntime, tmp_path: Path) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.upper().startswith("MESHFORGE_")
    }
    env.update(
        {
            "MESHFORGE_CLI__CONTROLLER_URL": f"https://127.0.0.1:{runtime.http_port}",
            "MESHFORGE_CLI__CA_FILE": str(ca_file()),
            "MESHFORGE_CLI__TOKEN": token_of(runtime),
            "MESHFORGE_DATA_DIR": str(tmp_path / "no-agent-here"),
            "XDG_CONFIG_HOME": str(tmp_path / "cli-config"),
        }
    )
    return env


async def test_mode3_rows_and_api_equals_cli_equals_function(
    runtime: ControllerRuntime, tmp_path: Path
) -> None:
    async with _lanes(runtime, [0.005, 0.02, 0.02]):
        job_id = await _submit(runtime, tmp_path)
        await _wait_done(runtime, job_id)
    engine = runtime._engine
    assert engine is not None
    await _settled(engine, job_id)
    (decision,) = await _sql(
        engine,
        "SELECT id, candidates, chosen_node_id FROM plan_decision "
        "WHERE job_id = :j AND outcome = 'dispatched'",
        j=job_id,
    )
    row = next(
        c
        for c in decision.candidates
        if c["rank"] == 1 and c["node_id"] == decision.chosen_node_id
    )
    assert row["mode"] == "split", "the planner did not choose Mode 3"
    lanes = await _sql(
        engine,
        "SELECT id, node_id FROM attempt WHERE job_id = :j AND role = 'lane' "
        "AND state = 'done' ORDER BY fencing_token",
        j=job_id,
    )
    (combine,) = await _sql(
        engine,
        "SELECT id, node_id FROM attempt WHERE job_id = :j AND role = 'combine' "
        "AND state = 'done'",
        j=job_id,
    )
    # measured phases we choose (the fake lanes report only run time)
    lane_phases = {
        lane.id: {"stage": 1_000 + 250 * i, "env": 20_000 + 5_000 * i, "run": 9_000}
        for i, lane in enumerate(lanes)
    }
    for lane in lanes:
        await _phases(engine, lane.id, {**lane_phases[lane.id], "collect": 10})
    await _phases(
        engine, combine.id, {"stage": 300, "env": None, "run": 700, "collect": 5}
    )

    built = await build_dataset(engine)
    got = {
        (r["term"], r["node_id"]): r
        for r in built.dataset.rows
        if r["decision_id"] == decision.id
    }

    # ---- the hand computation, from the stored rows
    expected: dict[tuple[str, str], float] = {}
    for lane in lanes:
        lone = next(
            c
            for c in decision.candidates
            if c["mode"] != "split"
            and c["node_id"] == lane.node_id
            and c.get("node_set") == [lane.node_id]
        )
        terms = lone["estimate"]["terms"]
        for name, ml_name in (("transfer", "lane_stage"), ("env", "lane_env")):
            term = terms[name]
            raw = term["detail"].get("calibration", {}).get("raw_s", term["seconds"])
            phase = "stage" if name == "transfer" else "env"
            # D345: exact only when nothing moves / the env is cached
            exact = (
                term["detail"].get("missing_bytes") == 0
                if name == "transfer"
                else term["detail"].get("env_cached") is True
            )
            if not exact and raw >= 0.05:
                expected[(ml_name, lane.node_id)] = math.log(
                    lane_phases[lane.id][phase] / 1000.0 / raw
                )
        chunks = await _sql(
            engine,
            "SELECT items, run_ms FROM chunk_record WHERE lane_attempt_id = :l "
            "AND outcome = 'committed' AND run_ms IS NOT NULL",
            l=lane.id,
        )
        items = sum(c.items for c in chunks)
        if items:
            spi_actual = sum(c.run_ms for c in chunks) / 1000.0 / items
            spi_raw = 1.0 / lone["rate_items_s"]
            if spi_actual > 0 and spi_raw * items >= 0.05:
                expected[("chunk_rate", lane.node_id)] = math.log(spi_actual / spi_raw)
    combine_term = row["estimate"]["terms"]["combine"]
    assert combine_term["detail"]["combine_node_id"] == combine.node_id
    expected[("combine", combine.node_id)] = math.log(1.0 / combine_term["seconds"])

    assert set(got) == set(expected)
    for key, y in expected.items():
        assert got[key]["y"] == pytest.approx(y, rel=1e-12, abs=1e-12), key
    terms_seen = {term for term, _ in got}
    assert {"lane_env", "chunk_rate", "combine"} <= terms_seen
    assert sum(1 for t, _ in got if t == "chunk_rate") == len(lanes) == 3

    # ---- API == CLI == function
    async with api_client(runtime, token=token_of(runtime)) as http:
        file_resp = await http.get("/v1/planner/ml/dataset", timeout=60)
        summary_resp = await http.get(
            "/v1/planner/ml/dataset", params={"format": "summary"}, timeout=60
        )
    assert file_resp.status_code == 200, file_resp.text
    assert file_resp.content == built.dataset.parquet
    assert file_resp.headers["x-meshforge-dataset-digest"] == built.dataset.digest
    assert summary_resp.status_code == 200, summary_resp.text
    assert summary_resp.json() == json.loads(json.dumps(summary(built)))

    out = tmp_path / "ds.parquet"
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "meshforge.cli",
        "planner",
        "ml",
        "dataset",
        "--out",
        str(out),
        "--json",
        env=_cli_env(runtime, tmp_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), 120)
    assert proc.returncode == 0, stderr.decode()
    assert out.read_bytes() == built.dataset.parquet
    sidecar = json.loads((tmp_path / "ds.parquet.json").read_text(encoding="utf-8"))
    assert sidecar == summary_resp.json()
    assert json.loads(stdout)["digest"] == built.dataset.digest


# ======================================================== 3. the route's edges


class _Principal:
    def __init__(self, *, admin: bool) -> None:
        self.user_id = "usr_test"
        self.name = "t"
        self.is_admin = admin


def _app(engine: Any, *, admin: bool = True) -> Any:
    from fastapi import FastAPI

    from meshforge.controller.api import ml_routes
    from meshforge.controller.api.auth import current_user, forbidden, require_admin
    from meshforge.controller.api.errors import install_error_handlers

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(ml_routes.ml_router)
    user = _Principal(admin=admin)
    app.dependency_overrides[current_user] = lambda: user

    def admin_only() -> Any:
        if not admin:
            raise forbidden()
        return user

    app.dependency_overrides[require_admin] = admin_only
    setattr(
        app.state, ml_routes.ML_STATE_KEY, ml_routes.MlDatasetApi(engine, max_runs=10)
    )
    return app


async def _call(app: Any, path: str) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get(path)


async def test_the_route_is_admin_only_and_an_empty_fabric_is_an_empty_dataset(
    engine: Any,
) -> None:
    refused = await _call(_app(engine, admin=False), "/v1/planner/ml/dataset")
    assert refused.status_code == 403
    doc = (await _call(_app(engine), "/v1/planner/ml/dataset?format=summary")).json()
    assert doc["rows"] == 0 and doc["counts"]["runs"] == 0
    assert doc["max_runs"] == 10 and doc["truncated"] is False
    empty = await _call(_app(engine), "/v1/planner/ml/dataset")
    assert empty.status_code == 200
    assert empty.headers["content-type"] == "application/vnd.apache.parquet"
    assert empty.headers["x-meshforge-dataset-digest"] == doc["digest"]
    bad = await _call(_app(engine), "/v1/planner/ml/dataset?format=csv")
    assert bad.status_code == 400


async def test_the_database_down_is_503() -> None:
    broken = DatabaseSettings(
        host="127.0.0.1", port=1, name="x", user="u", connect_timeout_s=2.0
    )
    eng = create_engine(broken)
    try:
        answer = await _call(_app(eng), "/v1/planner/ml/dataset")
        assert answer.status_code == 503, answer.text
        assert answer.json()["error"]["code"] == "network_error"
        assert answer.headers.get("retry-after") == "2"
    finally:
        await eng.dispose()
