"""V0.14 G2 tester round 1 - calibration on a real PostgreSQL, adversarial.

Criteria (brief 5b / D293 / D301 / D305-D308):

* holdout safety gate: a clearly worse refit is stored ``rejected`` with its
  reason; the active version is untouched and still what the source serves;
* too few / non-counting runs (failed attempts, no phases) write nothing;
* concurrency: many simultaneous refreshes (separate engines, also the very
  first activation) leave exactly one ``active`` row and no IntegrityError;
* pin: a missing pinned version is a ConfigError at startup; a pin ignores
  newer active versions; ``off`` plans uncalibrated and writes NULL;
* failure: an unreachable database keeps the version in use, never raises into
  dispatch; a corrupt active row neither crashes the source nor the refresh;
* forced (pinned-node) runs count, per-node factors differ per node;
* the refresh only reads the newest ``max_runs`` outcomes;
* REST: unauthenticated 401, user GET 200 / POST 403, admin POST 200, version
  query validation, with the real ``guard_requests`` + ``current_user``.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio

pytest.importorskip("alembic", reason="needs the controller extra")
pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from alembic import command
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from meshforge.common.api_token import API_TOKEN_PREFIX
from meshforge.common.errors import ConfigError
from meshforge.common.ids import new_job_id, new_node_id, new_session_id
from meshforge.controller.api import planner_routes as routes
from meshforge.controller.api.auth import (
    AUTH_STATE_KEY,
    ApiAuth,
    guard_requests,
)
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.auth.principal import Principal
from meshforge.controller.calibration_store import (
    CalibrationSource,
    CalibrationStore,
    CalibrationStoreError,
)
from meshforge.controller.jobs.execution_store import ExecutionStore
from meshforge.controller.jobs.planning import DispatchPlanner
from meshforge.controller.jobs.store import JobScheduling
from meshforge.controller.liveness import NodeState
from meshforge.controller.planner.calibration import CalibrationConfig
from meshforge.controller.planner_calibration_task import CalibrationRefresher
from meshforge.controller.registry import Registry
from meshforge.controller.settings import JobApiSettings, PlannerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.migrate import alembic_config
from meshforge.controller.store.models.auth import UserRole
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio]

MANIFEST = {"name": "g2t", "entrypoint": ["python", "main.py"]}
DIGEST = "sha256:" + "cd" * 32
CFG = CalibrationConfig()
GUESS = 60.0


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
async def store(pg_settings: DatabaseSettings) -> AsyncIterator[ExecutionStore]:
    await asyncio.to_thread(command.upgrade, alembic_config(pg_settings), "head")
    engine = create_engine(pg_settings, pool_size=5)
    try:
        yield ExecutionStore(engine)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def calibrations(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> AsyncIterator[CalibrationStore]:
    engine = create_engine(pg_settings, pool_size=3)
    try:
        yield CalibrationStore(engine)
    finally:
        await engine.dispose()


async def sql(pg: DatabaseSettings, statement: str, **params: Any) -> list[Any]:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            result = await conn.execute(text(statement), params)
            return list(result.all()) if result.returns_rows else []
    finally:
        await engine.dispose()


async def new_node(pg: DatabaseSettings) -> str:
    engine = create_engine(pg)
    try:
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
    finally:
        await engine.dispose()


def planner_for(manager: Manager) -> DispatchPlanner:
    return DispatchPlanner(
        manager=manager,  # type: ignore[arg-type]
        settings=JobApiSettings(max_attempts_per_node=64),
        planner=PlannerSettings(default_compute_s=GUESS),
        clock=lambda: 1_000_000.0,
        wall_clock=lambda: dt.datetime.now(dt.UTC),
        started_at=0.0,
        cache_live=lambda _n, _s: True,
    )


async def dispatch_one(
    store: ExecutionStore,
    planner: DispatchPlanner,
    *,
    fingerprint: str,
    pin: str | None = None,
) -> str:
    job, _ = await store.create_queued_job(
        job_id=str(new_job_id()),
        name="g2t",
        manifest=MANIFEST,
        bundle_digest=DIGEST,
        placement={"mode": "auto", "node_id": pin},
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


async def finish(
    pg: DatabaseSettings,
    attempt_id: str,
    *,
    run_s: float | None,
    at: int,
    state: str = "done",
) -> None:
    await sql(
        pg,
        "UPDATE attempt SET state = :st, "
        "ended_at = timestamptz '2026-10-01 00:00:00+00' + make_interval(secs => :at) "
        "WHERE id = :a",
        a=attempt_id,
        at=at,
        st=state,
    )
    for phase, ms, skipped in (
        ("stage", 0, True),
        ("env", 0, True),
        ("run", 0 if run_s is None else round(run_s * 1000), run_s is None),
        ("collect", 0, True),
    ):
        await sql(
            pg,
            "INSERT INTO attempt_phase (attempt_id, phase, started_at, duration_ms, "
            "skipped) VALUES (:a, :p, now(), :ms, :s)",
            a=attempt_id,
            p=phase,
            ms=ms,
            s=skipped,
        )


_FP = iter(range(10**9))


async def seed(
    pg: DatabaseSettings,
    store: ExecutionStore,
    planner: DispatchPlanner,
    run_seconds: list[float | None],
    *,
    start: int = 0,
    pin: str | None = None,
    state: str = "done",
) -> list[str]:
    ids = []
    for i, run_s in enumerate(run_seconds):
        fp = f"{next(_FP):064x}"
        a = await dispatch_one(store, planner, fingerprint=fp, pin=pin)
        await finish(pg, a, run_s=run_s, at=start + i, state=state)
        ids.append(a)
    return ids


async def statuses(pg: DatabaseSettings) -> list[tuple[int, str]]:
    rows = await sql(
        pg, "SELECT version, status FROM planner_calibration ORDER BY version"
    )
    return [(int(r.version), str(r.status)) for r in rows]


# ---------------------------------------------------------------- tests


async def test_failed_or_phaseless_attempts_never_count(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 6, state="failed")
    await seed(pg_settings, store, planner, [None] * 6, start=10)  # run phase skipped
    r = await calibrations.refresh(CFG)
    assert r.row is None and r.skipped == "too_few_runs" and r.matched_runs == 0
    assert await statuses(pg_settings) == []
    # exactly one good run is still too few
    await seed(pg_settings, store, planner, [120.0], start=30)
    r = await calibrations.refresh(CFG)
    assert r.row is None and r.matched_runs == 1
    assert await statuses(pg_settings) == []
    # the second good run makes a version
    await seed(pg_settings, store, planner, [120.0], start=31)
    r = await calibrations.refresh(CFG)
    assert r.row is not None and r.row.status == "active" and r.matched_runs == 2


async def test_a_clearly_worse_fit_is_rejected_active_untouched(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 10)
    first = await calibrations.refresh(CFG)
    assert first.row is not None and first.row.status == "active"
    v1 = first.row.version
    # a long regime of 5x then the newest runs are 2x again: new fit (5x-ish)
    # is far worse than v1 on the newest 20 %
    await seed(pg_settings, store, planner, [300.0] * 40, start=10)
    await seed(pg_settings, store, planner, [120.0] * 10, start=50)
    second = await calibrations.refresh(CFG)
    assert second.row is not None
    assert second.row.status == "rejected"
    assert second.row.reason and "holdout_error" in second.row.reason
    assert second.row.holdout_error_new is not None
    assert second.row.holdout_error_active is not None
    assert second.row.holdout_error_new > second.row.holdout_error_active * 1.05
    assert second.superseded is None
    assert await statuses(pg_settings) == [
        (v1, "active"),
        (second.row.version, "rejected"),
    ]
    src = CalibrationSource(calibrations, "auto")
    used = await src.refresh()
    assert used is not None and used.version == v1
    # repeated refreshes of the same bad data keep rejecting, never activate
    for _ in range(3):
        again = await calibrations.refresh(CFG)
        assert again.row is not None and again.row.status == "rejected"
    assert (await calibrations.active_version()) == v1


async def test_first_version_gate_uses_uncalibrated_baseline(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    """Pure noise around the guess: nothing to learn but the fit must not make
    things > 5 % worse than 'no calibration' on held-out runs."""
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    # old runs 3x, newest two exactly the guess -> fitted factor ~2.5 is worse
    await seed(pg_settings, store, planner, [180.0] * 8 + [60.0] * 2)
    r = await calibrations.refresh(CFG)
    assert r.row is not None
    assert r.row.status == "rejected", (
        r.row.holdout_error_new,
        r.row.holdout_error_active,
    )
    assert await calibrations.active_version() is None
    src = CalibrationSource(calibrations, "auto")
    assert await src.refresh() is None


@pytest.mark.parametrize("first_ever", [True, False])
async def test_many_concurrent_refreshes_one_active(
    pg_settings: DatabaseSettings,
    store: ExecutionStore,
    calibrations: CalibrationStore,
    first_ever: bool,
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 12)
    if not first_ever:
        assert (await calibrations.refresh(CFG)).row is not None
    engines = [create_engine(pg_settings, pool_size=2) for _ in range(8)]
    try:
        stores = [CalibrationStore(e) for e in engines]
        for _round in range(3):
            results = await asyncio.gather(
                *(s.refresh(CFG) for s in stores), return_exceptions=True
            )
            errors = [r for r in results if isinstance(r, BaseException)]
            assert errors == []
            active = await sql(
                pg_settings,
                "SELECT count(*) AS n FROM planner_calibration WHERE status = 'active'",
            )
            assert active[0].n == 1
    finally:
        for e in engines:
            await e.dispose()
    rows = await statuses(pg_settings)
    assert [s for _v, s in rows].count("active") == 1
    assert [s for _v, s in rows][-1] == "active"  # newest accepted one is the active
    assert len(rows) == (24 if first_ever else 25)
    assert all(s in ("active", "superseded", "rejected") for _v, s in rows)
    # versions strictly increasing and unique
    vs = [v for v, _s in rows]
    assert vs == sorted(set(vs))


async def test_concurrent_refresh_during_dispatch_reads(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 10)
    src = CalibrationSource(calibrations, "auto")
    stop = False

    async def reader() -> int:
        n = 0
        last = 0
        while not stop:
            c = await src.refresh()
            if c is not None:
                assert c.version >= last
                last = c.version
            n += 1
            await asyncio.sleep(0)
        return n

    task = asyncio.create_task(reader())
    for _ in range(6):
        await calibrations.refresh(CFG)
    stop = True
    assert await task > 0
    assert src.failures == 0


async def test_pin_missing_version_is_config_error_at_startup(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    src = CalibrationSource(calibrations, "99")
    with pytest.raises(ConfigError) as exc:
        await src.check()
    assert "99" in str(exc.value)
    assert src.current is None
    # auto/off never raise at startup
    await CalibrationSource(calibrations, "auto").check()
    await CalibrationSource(calibrations, "off").check()


async def test_pin_to_corrupt_row_is_config_error_and_pin_survives_new_active(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    (bad,) = await sql(
        pg_settings,
        "INSERT INTO planner_calibration (status, factors, fit_inputs) "
        "VALUES ('superseded', '{\"format\": 9}'::jsonb, '{}'::jsonb) RETURNING version",
    )
    with pytest.raises(ConfigError):
        await CalibrationSource(calibrations, str(bad.version)).check()

    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 10)
    first = await calibrations.refresh(CFG)
    assert first.row is not None
    pin = CalibrationSource(calibrations, str(first.row.version))
    await pin.check()
    await seed(pg_settings, store, planner, [120.0] * 6, start=20)
    second = await calibrations.refresh(CFG)
    assert second.row is not None and second.row.status == "active"
    got = await pin.refresh()
    assert got is not None and got.version == first.row.version
    auto = CalibrationSource(calibrations, "auto")
    got2 = await auto.refresh()
    assert got2 is not None and got2.version == second.row.version


async def test_pinning_a_rejected_version_is_accepted_by_check(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    """Observation (see report): the model docstring says a rejected row is
    'never used', but an operator pin on it passes the startup check."""
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [180.0] * 8 + [60.0] * 2)
    r = await calibrations.refresh(CFG)
    assert r.row is not None and r.row.status == "rejected"
    src = CalibrationSource(calibrations, str(r.row.version))
    try:
        await src.check()
    except ConfigError:
        return  # a stricter implementation is fine too
    assert src.current is not None and src.current.version == r.row.version


async def test_modes_write_the_right_calibration_version_on_decisions(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 10)
    first = await calibrations.refresh(CFG)
    assert first.row is not None
    v = first.row.version

    async def decision(mode: str) -> Any:
        src = CalibrationSource(calibrations, mode)
        if mode == str(v):
            await src.check()
        planner.calibration = await src.refresh()
        a = await dispatch_one(store, planner, fingerprint=f"{next(_FP):064x}")
        (d,) = await sql(
            pg_settings,
            "SELECT calibration_version AS cv, candidates, replay FROM plan_decision WHERE attempt_id = :a",  # noqa: E501
            a=a,
        )
        return d

    off = await decision("off")
    assert off.cv is None
    assert "calibration" not in json.dumps(off.candidates)
    assert "calibration" not in (
        off.replay if isinstance(off.replay, dict) else json.loads(off.replay)
    )
    auto = await decision("auto")
    assert auto.cv == v
    pinned = await decision(str(v))
    assert pinned.cv == v
    replay = (
        pinned.replay if isinstance(pinned.replay, dict) else json.loads(pinned.replay)
    )
    assert replay["calibration"]["version"] == v


async def test_unreachable_database_keeps_version_in_use_and_never_raises(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 10)
    first = await calibrations.refresh(CFG)
    assert first.row is not None
    dead_settings = pg_settings.model_copy(update={"port": 1})
    dead_engine = create_engine(dead_settings, pool_size=1)
    try:
        dead = CalibrationStore(dead_engine)
        src = CalibrationSource(dead, "auto")
        src.current = first.row.calibration  # in use before the outage
        for _ in range(3):
            got = await asyncio.wait_for(src.refresh(), 30)
            assert got is not None and got.version == first.row.version
        assert src.failures == 3
        with pytest.raises(CalibrationStoreError):
            await dead.refresh(CFG)
        # nothing partially written
        assert await statuses(pg_settings) == [(first.row.version, "active")]
        refresher = CalibrationRefresher(dead, PlannerSettings())
        with pytest.raises(CalibrationStoreError):
            await refresher.refresh_now()
    finally:
        await dead_engine.dispose()


async def test_corrupt_active_row_is_survivable(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 10)
    first = await calibrations.refresh(CFG)
    assert first.row is not None
    await sql(
        pg_settings,
        "UPDATE planner_calibration SET factors = '{\"format\": 9}'::jsonb WHERE version = :v",
        v=first.row.version,
    )
    src = CalibrationSource(calibrations, "auto")
    assert await src.refresh() is None  # planned uncalibrated, no exception
    row = await calibrations.row(first.row.version)
    assert row is not None and row.calibration is None
    again = await calibrations.refresh(CFG)  # does not crash
    assert again.row is not None
    assert (
        await sql(
            pg_settings,
            "SELECT count(*) AS n FROM planner_calibration WHERE status='active'",
        )
    )[0].n == 1


async def test_forced_node_runs_count_and_factors_are_per_node(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    n1 = await new_node(pg_settings)
    n2 = await new_node(pg_settings)
    planner = planner_for(Manager([n1, n2]))
    await seed(pg_settings, store, planner, [120.0] * 10, pin=n1)  # n1 is 2x slower
    await seed(
        pg_settings, store, planner, [30.0] * 10, pin=n2, start=100
    )  # n2 2x faster
    r = await calibrations.refresh(CFG)
    assert r.matched_runs == 20
    assert r.row is not None
    cal = r.row.calibration
    assert cal is not None
    f1 = cal.lookup("compute", n1, None)
    f2 = cal.lookup("compute", n2, None)
    assert f1 is not None and f2 is not None
    assert f1[1] == "node" and f2[1] == "node"
    assert f1[0].factor > 1.0 > f2[0].factor
    g = cal.lookup("compute", "node_unseen", None)
    assert g is not None and g[1] == "global"


async def test_refresh_reads_only_the_newest_max_runs(
    pg_settings: DatabaseSettings, store: ExecutionStore, calibrations: CalibrationStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [600.0] * 10)  # old, 10x
    await seed(pg_settings, store, planner, [120.0] * 10, start=100)  # new, 2x
    r = await calibrations.refresh(CFG, max_runs=10)
    assert r.matched_runs == 10
    assert r.row is not None and r.row.calibration is not None
    f = r.row.calibration.lookup("compute", node, None)
    assert f is not None and f[0].median == 2.0


async def test_db_constraints_refuse_garbage_rows(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    base = (
        "INSERT INTO planner_calibration (status, factors, fit_inputs{c}) VALUES ({v})"
    )
    bad: list[tuple[str, dict[str, Any]]] = [
        (base.format(c="", v="'weird', '{}'::jsonb, '{}'::jsonb"), {}),
        (base.format(c="", v="'superseded', '[]'::jsonb, '{}'::jsonb"), {}),
        (base.format(c="", v="'superseded', '{}'::jsonb, '[]'::jsonb"), {}),
        (
            base.format(
                c=", holdout_error_new",
                v="'superseded', '{}'::jsonb, '{}'::jsonb, -0.1",
            ),
            {},
        ),
        (
            base.format(
                c=", holdout_error_active",
                v="'superseded', '{}'::jsonb, '{}'::jsonb, -0.1",
            ),
            {},
        ),
        (
            base.format(c=", reason", v="'superseded', '{}'::jsonb, '{}'::jsonb, :r"),
            {"r": "x" * 513},
        ),
        (base.format(c="", v="'rejected', '{}'::jsonb, '{}'::jsonb"), {}),
    ]
    for stmt, params in bad:
        with pytest.raises((IntegrityError, DBAPIError)):
            await sql(pg_settings, stmt, **params)
    assert await sql(pg_settings, "SELECT 1 FROM planner_calibration") == []


# ------------------------------------------------------------------ REST


class _Users:
    def __init__(self, table: dict[str, Principal]) -> None:
        self.table = table

    async def authenticate(self, token: str, *, kind: Any = None) -> Principal | None:
        return self.table.get(token)

    async def note_legacy_use(self, source: str | None) -> None:
        return None

    async def owns_output(self, user_id: str, digest_hex: str) -> bool:
        return False


ADMIN_TOKEN = API_TOKEN_PREFIX + "A" * 43
USER_TOKEN = API_TOKEN_PREFIX + "B" * 43
BAD_TOKEN = API_TOKEN_PREFIX + "C" * 43


def secured_app(engine: Any) -> tuple[FastAPI, CalibrationRefresher]:
    store = CalibrationStore(engine)
    app = FastAPI(dependencies=[Depends(guard_requests)])
    install_error_handlers(app)
    app.include_router(routes.planner_router)
    users = _Users(
        {
            ADMIN_TOKEN: Principal("usr_admin", "root", UserRole.ADMIN, "tok_a"),
            USER_TOKEN: Principal("usr_user", "bob", UserRole.USER, "tok_b"),
        }
    )
    setattr(app.state, AUTH_STATE_KEY, ApiAuth(verifier=None, users=users))  # type: ignore[arg-type]
    refresher = CalibrationRefresher(store, PlannerSettings())
    setattr(
        app.state,
        routes.PLANNER_STATE_KEY,
        routes.PlannerApi(
            store=store, source=CalibrationSource(store, "auto"), refresher=refresher
        ),
    )
    return app, refresher


async def call(
    app: FastAPI, method: str, path: str, token: str | None
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        return await c.request(method, path, headers=headers)


async def test_rest_auth_matrix_and_query_validation(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    engine = create_engine(pg_settings, pool_size=3)
    try:
        app, refresher = secured_app(engine)
        for method, path in (
            ("GET", "/v1/planner/calibration"),
            ("POST", "/v1/planner/calibrate"),
        ):
            for token in (None, BAD_TOKEN, "not-a-token"):
                r = await call(app, method, path, token)
                assert r.status_code == 401, (method, path, token, r.text)
        assert refresher.refreshes == 0
        assert (
            await call(app, "GET", "/v1/planner/calibration", USER_TOKEN)
        ).status_code == 200
        assert (
            await call(app, "GET", "/v1/planner/calibration", ADMIN_TOKEN)
        ).status_code == 200
        forbidden = await call(app, "POST", "/v1/planner/calibrate", USER_TOKEN)
        assert forbidden.status_code == 403
        assert refresher.refreshes == 0  # the refresh never ran for a non-admin
        ok = await call(app, "POST", "/v1/planner/calibrate", ADMIN_TOKEN)
        assert ok.status_code == 200
        body = ok.json()
        assert body["created"] is None and body["skipped"] == "too_few_runs"
        assert refresher.refreshes == 1
        # wrong methods
        assert (
            await call(app, "GET", "/v1/planner/calibrate", ADMIN_TOKEN)
        ).status_code in (404, 405)
        assert (
            await call(app, "POST", "/v1/planner/calibration", ADMIN_TOKEN)
        ).status_code in (404, 405)
        # version query validation
        for q, code in (
            ("0", 400),
            ("-1", 400),
            ("abc", 400),
            ("1.5", 400),
            (str(2**63), 400),
            ("77", 404),
        ):
            r = await call(
                app, "GET", f"/v1/planner/calibration?version={q}", USER_TOKEN
            )
            assert r.status_code == code, (q, r.status_code, r.text)
        assert await sql(pg_settings, "SELECT 1 FROM planner_calibration") == []
    finally:
        await engine.dispose()


async def test_rest_admin_calibrate_end_to_end_and_concurrent_posts(
    pg_settings: DatabaseSettings, store: ExecutionStore
) -> None:
    node = await new_node(pg_settings)
    planner = planner_for(Manager([node]))
    await seed(pg_settings, store, planner, [120.0] * 12)
    engine = create_engine(pg_settings, pool_size=4)
    try:
        app, _ = secured_app(engine)
        rs = await asyncio.gather(
            *(call(app, "POST", "/v1/planner/calibrate", ADMIN_TOKEN) for _ in range(5))
        )
        assert [r.status_code for r in rs] == [200] * 5
        rows = await statuses(pg_settings)
        assert [s for _v, s in rows].count("active") == 1
        got = (await call(app, "GET", "/v1/planner/calibration", USER_TOKEN)).json()
        assert got["active_version"] == rows[-1][0]
        assert got["calibration"]["status"] == "active"
        compute = [f for f in got["calibration"]["factors"] if f["term"] == "compute"]
        assert compute and all(f["factor"] > 1.0 for f in compute)
        assert all(
            f["n"] >= 1 and f["key_level"] in ("global", "node", "node_family")
            for f in compute
        )
        # an old version is viewable too
        old = (
            await call(
                app, "GET", f"/v1/planner/calibration?version={rows[0][0]}", USER_TOKEN
            )
        ).json()
        assert old["calibration"]["version"] == rows[0][0]
    finally:
        await engine.dispose()
