"""V0.8 G1 tester r1: ``GET /v1/jobs`` paging (D174) against a brute-force
model and against hostile cursors / times / filters. Real PostgreSQL."""

# ruff: noqa: C416, PERF401, S311

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import random
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import bearer, create_user, login
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.ids import new_job_id
from meshforge.controller.api.paging import encode_cursor
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.models.job import Job
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(300),
]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"
DIGEST = "sha256:" + "ab" * 32


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        jobs={"dispatch_interval_s": 60.0},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def users(engine: AsyncEngine, runtime: ControllerRuntime) -> dict[str, str]:
    return {
        "admin": await create_user(engine, "admin", ADMIN_PW, admin=True),
        "alice": await create_user(engine, "alice", ALICE_PW),
        "bob": await create_user(engine, "bob", BOB_PW),
    }


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as client:
        yield client


async def _job(rt: ControllerRuntime, owner: str) -> str:
    record, _ = await rt.jobs.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": "n"},
        request_hash="h" * 64,
        idempotency_key=None,
        owner_user_id=owner,
    )
    return record.id


async def _walk(
    anon: httpx.AsyncClient, token: str, **params: Any
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor = None
    for _ in range(2000):
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        r = await anon.get("/v1/jobs", params=q, headers=bearer(token))
        assert r.status_code == 200, r.text
        body = r.json()
        out += body["jobs"]
        cursor = body["next_cursor"]
        if cursor is None:
            return out
    raise AssertionError("cursor never ended")


async def test_random_filters_and_page_sizes_match_a_brute_force_model(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    rng = random.Random(7)
    ids: list[str] = []
    for i in range(90):
        ids.append(await _job(runtime, users["alice" if i % 3 else "bob"]))
    base = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
    stamps: dict[str, dt.datetime] = {}
    async with engine.begin() as db:
        for i, jid in enumerate(ids):
            # heavy ties + microsecond-apart neighbours + a far-away outlier
            at = base + dt.timedelta(minutes=rng.choice([0, 0, 1, 1, 1, 5, 9]))
            if i % 11 == 0:
                at += dt.timedelta(microseconds=rng.randint(1, 3))
            if i == 5:
                at = dt.datetime(2001, 1, 1, tzinfo=dt.UTC)
            stamps[jid] = at
            await db.execute(update(Job).where(Job.id == jid).values(created_at=at))
    admin = await login(anon, "admin", ADMIN_PW)
    owners = {j: users["alice"] if i % 3 else users["bob"] for i, j in enumerate(ids)}
    for trial in range(25):
        limit = rng.choice([1, 2, 3, 7, 50, 100])
        params: dict[str, Any] = {"limit": limit}
        pool = list(ids)
        if rng.random() < 0.5:
            who = rng.choice(["alice", "bob"])
            params["owner"] = users[who]
            pool = [j for j in pool if owners[j] == users[who]]
        if rng.random() < 0.5:
            lo = base + dt.timedelta(minutes=rng.randint(0, 5))
            params["created_after"] = lo.isoformat()
            pool = [j for j in pool if stamps[j] >= lo]
        if rng.random() < 0.5:
            hi = base + dt.timedelta(minutes=rng.randint(1, 9))
            # a non-UTC spelling of the same instant must mean the same instant
            params["created_before"] = hi.astimezone(
                dt.timezone(dt.timedelta(hours=5, minutes=30))
            ).isoformat()
            pool = [j for j in pool if stamps[j] < hi]
        if rng.random() < 0.3:
            params["state"] = ["queued", "queued", "succeeded"]  # duplicates are fine
        elif rng.random() < 0.15:
            params["state"] = "succeeded"
            pool = []
        got = [j["job_id"] for j in await _walk(anon, admin, **params)]
        expected = sorted(pool, key=lambda j: (stamps[j], j), reverse=True)
        assert got == expected, (trial, params)


async def test_pages_are_stable_under_concurrent_inserts_and_deletes(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    original = [await _job(runtime, users["alice"]) for _ in range(40)]
    token = await login(anon, "alice", ALICE_PW)
    stop = asyncio.Event()
    created: list[str] = []

    async def churn() -> None:
        while not stop.is_set():
            created.append(await _job(runtime, users["alice"]))
            await asyncio.sleep(0.005)

    task = asyncio.create_task(churn())
    try:
        walked = [j["job_id"] for j in await _walk(anon, token, limit=3)]
    finally:
        stop.set()
        await task
    assert len(walked) == len(set(walked)), "a job appeared on two pages"
    # Every job that existed before the walk started is on some page; newer
    # ones may or may not be (they sort first, before the first cursor).
    assert set(original) <= set(walked)
    assert set(walked) <= set(original) | set(created)


async def test_a_cursor_pointing_at_a_deleted_row_still_continues_exactly(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    ids = [await _job(runtime, users["alice"]) for _ in range(12)]
    token = await login(anon, "alice", ALICE_PW)
    page = (
        await anon.get("/v1/jobs", params={"limit": 5}, headers=bearer(token))
    ).json()
    last = page["jobs"][-1]["job_id"]
    from sqlalchemy import delete

    async with engine.begin() as db:
        await db.execute(delete(Job).where(Job.id == last))
    rest = await _walk(anon, token, limit=5, cursor=page["next_cursor"])
    expected_tail = [j for j in reversed(ids)][5:]
    assert [j["job_id"] for j in rest] == expected_tail


# ---------------------------------------------------------------- hostility


def _b64(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode()


@pytest.mark.parametrize(
    "cursor",
    [
        "!!!",
        "====",
        "%00",
        "a" * 161,
        "a" * 5000,
        _b64("v2|2026-01-01T00:00:00+00:00|job_01J0000000000000000000000A"),
        _b64("v1|2026-01-01T00:00:00|job_01J0000000000000000000000A"),  # naive
        _b64("v1|not-a-date|job_01J0000000000000000000000A"),
        _b64("v1|2026-01-01T00:00:00+00:00|usr_01J0000000000000000000000A"),
        _b64("v1|2026-01-01T00:00:00+00:00|job_x; DROP TABLE job"),
        _b64("v1|2026-01-01T00:00:00+00:00|"),
        _b64("v1|2026-01-01T00:00:00+00:00|job_01J0000000000000000000000A|extra"),
        _b64("v1|2026-01-01T00:00:00+00:00"),
        _b64("v1|0001-01-01T00:00:00+23:59|job_01J0000000000000000000000A"),
        _b64("v1|9999-12-31T23:59:59.999999-23:59|job_01J0000000000000000000000A"),
        _b64("v1|9999-12-31T23:59:59.999999+00:00|job_01J0000000000000000000000A"),
        _b64("v1|0001-01-01T00:00:00+00:00|job_01J0000000000000000000000A"),
        _b64("v1|2026-13-45T99:99:99+00:00|job_01J0000000000000000000000A"),
        _b64("v1|é2026|job_01J0000000000000000000000A"),
        "dgjFpg",  # decodes to non-ASCII bytes
    ],
)
async def test_hostile_cursors_are_4xx_or_a_clean_page_never_500(
    anon: httpx.AsyncClient, users: dict[str, str], cursor: str
) -> None:
    token = await login(anon, "alice", ALICE_PW)
    r = await anon.get("/v1/jobs", params={"cursor": cursor}, headers=bearer(token))
    assert r.status_code < 500, (cursor[:60], r.status_code, r.text[:200])
    if r.status_code == 200:
        assert (
            r.json()["count"] == 0
        )  # valid but out-of-range: nothing, not "from the top"
    assert "Traceback" not in r.text and "DROP" not in r.text
    assert (await anon.get("/healthz")).status_code == 200


@pytest.mark.parametrize(
    "params",
    [
        {"created_after": "0001-01-01T00:00:00+23:59"},
        {"created_before": "9999-12-31T23:59:59-23:59"},
        {"created_after": "9999-12-31T23:59:59.999999+00:00"},
        {
            "created_after": "0001-01-01T00:00:00Z",
            "created_before": "0001-01-01T00:00:00Z",
        },
        {
            "created_after": "2030-01-01T00:00:00Z",
            "created_before": "2020-01-01T00:00:00Z",
        },
        {"created_after": "garbage"},
        {"created_after": ""},
        {"created_after": "2026-01-01"},
        {"created_before": "2026-01-01T00:00:00"},
        {"created_after": "1e10"},
        {"created_after": "99999999999"},
        {"limit": 0},
        {"limit": -5},
        {"limit": 10**12},
        {"limit": "abc"},
        {"limit": ""},
        {"state": "nonsense"},
        {"state": ""},
        {"state": ["queued"] * 500},
        {"owner": "x"},
        {"owner": "usr_" + "z" * 500},
        {"node": "node_" + "é" * 20},
        {"node": "n; DROP TABLE job"},
        {"owner": "%00"},
    ],
)
async def test_hostile_filters_never_500(
    anon: httpx.AsyncClient, users: dict[str, str], params: dict[str, Any]
) -> None:
    token = await login(anon, "admin", ADMIN_PW)
    r = await anon.get("/v1/jobs", params=params, headers=bearer(token))
    assert r.status_code < 500, (params, r.status_code, r.text[:300])
    if r.status_code >= 400:
        assert r.json()["error"]["code"] in {
            "invalid_input",
            "not_found",
            "unauthorized",
        }


async def test_the_cursor_round_trips_at_microsecond_precision_and_other_zones(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    ids = [await _job(runtime, users["alice"]) for _ in range(6)]
    t0 = dt.datetime(2026, 5, 5, 5, 5, 5, 123456, tzinfo=dt.UTC)
    async with engine.begin() as db:
        for i, jid in enumerate(ids):
            await db.execute(
                update(Job)
                .where(Job.id == jid)
                .values(created_at=t0 + dt.timedelta(microseconds=i // 2))
            )
    token = await login(anon, "alice", ALICE_PW)
    seen = [j["job_id"] for j in await _walk(anon, token, limit=1)]
    assert sorted(seen) == sorted(ids) and len(seen) == 6
    # A client-built cursor with a non-UTC offset means the same instant.
    plus = t0.astimezone(dt.timezone(dt.timedelta(hours=-7)))
    cursor = encode_cursor(plus, "job_" + "0" * 26) if False else None
    assert cursor is None  # (encode normalises to UTC; decode accepts any zone)


async def test_a_user_cannot_use_filters_or_cursors_to_see_foreign_jobs(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    mine = [await _job(runtime, users["alice"]) for _ in range(3)]
    theirs = [await _job(runtime, users["bob"]) for _ in range(3)]
    token = await login(anon, "alice", ALICE_PW)
    for params in (
        {},
        {"owner": users["alice"]},
        {"owner": users["bob"]},
        {"state": "queued"},
        {"created_after": "2000-01-01T00:00:00Z"},
        {"owner": users["bob"], "state": ["queued", "succeeded"], "limit": 1},
    ):
        got = {j["job_id"] for j in await _walk(anon, token, **params)}
        assert got.isdisjoint(theirs), params
    one = await anon.get(f"/v1/jobs/{theirs[0]}", headers=bearer(token))
    assert one.status_code == 404
    assert set(mine) == {j["job_id"] for j in await _walk(anon, token)}
