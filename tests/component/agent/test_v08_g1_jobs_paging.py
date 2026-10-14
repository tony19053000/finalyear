"""V0.8 G1 deliverable 2 (D174): ``GET /v1/jobs`` keyset pages and filters -
real PostgreSQL, a real HTTPS controller with real users.

* walking ``next_cursor`` returns every visible job exactly once, newest
  first, also when many jobs share one ``created_at`` (the id breaks ties) and
  when new jobs are created while walking (pages never shift);
* filters: state (one or several), owner, node (an attempt there), created
  after/before; a user only ever sees their own jobs - a cursor or an
  ``owner=`` of someone else never widens that;
* a malformed cursor, a naive time or a bad id is 400, never "from the top".
"""

from __future__ import annotations

import datetime as dt
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

from meshforge.common.ids import (
    new_attempt_id,
    new_job_id,
    new_node_id,
    new_session_id,
)
from meshforge.common.states import JobState
from meshforge.controller.api.paging import encode_cursor
from meshforge.controller.jobs.execution_store import Dispatch, NodeView
from meshforge.controller.jobs.store import JobRecord
from meshforge.controller.liveness import NodeState
from meshforge.controller.registry import Registry
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.models.job import Job
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.timeout(240),
]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"
BOB_PW = "bob-password-123"
DIGEST = "sha256:" + "ab" * 32


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    # No dispatching: the jobs stay where the test puts them.
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


async def _job(runtime: ControllerRuntime, owner: str, node: str = "n") -> JobRecord:
    record, _ = await runtime.jobs.create_queued_job(
        job_id=str(new_job_id()),
        name="demo",
        manifest={"name": "demo"},
        bundle_digest=DIGEST,
        placement={"mode": "offload", "node_id": node},
        request_hash="h" * 64,
        idempotency_key=None,
        owner_user_id=owner,
    )
    return record


async def _walk(
    anon: httpx.AsyncClient, token: str, **params: Any
) -> tuple[list[str], int]:
    seen: list[str] = []
    pages = 0
    cursor: str | None = None
    while True:
        query = dict(params)
        if cursor is not None:
            query["cursor"] = cursor
        response = await anon.get("/v1/jobs", params=query, headers=bearer(token))
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["count"] == len(body["jobs"])
        seen += [j["job_id"] for j in body["jobs"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            return seen, pages
        assert pages < 1000


async def test_walking_the_cursor_sees_every_job_once_newest_first(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
    alice = [await _job(runtime, users["alice"]) for _ in range(53)]
    bob = [await _job(runtime, users["bob"]) for _ in range(12)]
    # 20 of alice's jobs share one created_at: the id must break the tie.
    tie = dt.datetime(2026, 10, 1, 12, 0, tzinfo=dt.UTC)
    async with engine.begin() as db:
        await db.execute(
            update(Job)
            .where(Job.id.in_([j.id for j in alice[10:30]]))
            .values(created_at=tie)
        )
    admin = await login(anon, "admin", ADMIN_PW)
    everyone, pages = await _walk(anon, admin, limit=7)
    assert sorted(everyone) == sorted(j.id for j in alice + bob)
    assert len(everyone) == len(set(everyone)) == 65
    assert pages == 10  # ceil(65 / 7)
    records = {j.id: j for j in await runtime.jobs.list_jobs(limit=1000)}
    keys = [(records[i].created_at, i) for i in everyone]
    assert keys == sorted(keys, reverse=True)
    # Default page size 50, and the last page has no cursor.
    first = (await anon.get("/v1/jobs", headers=bearer(admin))).json()
    assert first["count"] == 50 and first["next_cursor"] is not None

    # A user walks only their own jobs - whatever the filter says.
    token = await login(anon, "alice", ALICE_PW)
    mine, _ = await _walk(anon, token, limit=9)
    assert sorted(mine) == sorted(j.id for j in alice)
    other, _ = await _walk(anon, token, owner=users["bob"])
    assert other == []
    # A cursor taken from bob's newest job does not show bob's jobs to alice.
    newest_bob = records[bob[-1].id]
    cursor = encode_cursor(newest_bob.created_at, newest_bob.id)
    page = await anon.get("/v1/jobs", params={"cursor": cursor}, headers=bearer(token))
    assert {j["job_id"] for j in page.json()["jobs"]} <= {j.id for j in alice}


async def test_new_jobs_do_not_shift_the_pages(
    runtime: ControllerRuntime, anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    first_batch = [await _job(runtime, users["alice"]) for _ in range(10)]
    token = await login(anon, "alice", ALICE_PW)
    page1 = (
        await anon.get("/v1/jobs", params={"limit": 4}, headers=bearer(token))
    ).json()
    for _ in range(5):  # created while the user reads page 1
        await _job(runtime, users["alice"])
    rest, _ = await _walk(anon, token, limit=4, cursor=page1["next_cursor"])
    walked = [j["job_id"] for j in page1["jobs"]] + rest
    assert walked == [j.id for j in reversed(first_batch)]


async def test_filters_state_owner_node_and_time(
    runtime: ControllerRuntime,
    anon: httpx.AsyncClient,
    users: dict[str, str],
    engine: AsyncEngine,
) -> None:
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
    )
    jobs = [await _job(runtime, users["alice"], node) for _ in range(6)]
    bobs = [await _job(runtime, users["bob"], node) for _ in range(2)]
    # Two of alice's jobs get an attempt on the node.
    placed: list[str] = []

    def place_two(job: JobRecord, view: NodeView | None) -> Dispatch | None:
        if view is None or job.id not in {jobs[1].id, jobs[3].id}:
            return None
        placed.append(job.id)
        return Dispatch(attempt_id=str(new_attempt_id()), start_snapshot={"k": 1})

    await runtime.jobs.dispatch_queued(place_two, limit=100, max_per_node=10)
    assert sorted(placed) == sorted([jobs[1].id, jobs[3].id])
    admin = await login(anon, "admin", ADMIN_PW)

    async def ids(**params: Any) -> set[str]:
        got, _ = await _walk(anon, admin, **params)
        return set(got)

    assert await ids(node=node) == {jobs[1].id, jobs[3].id}
    assert await ids(state="preparing") == {jobs[1].id, jobs[3].id}
    queued = {j.id for j in jobs + bobs} - {jobs[1].id, jobs[3].id}
    assert await ids(state="queued") == queued
    both = await anon.get(
        "/v1/jobs?state=queued&state=preparing", headers=bearer(admin)
    )
    assert {j["job_id"] for j in both.json()["jobs"]} == {j.id for j in jobs + bobs}
    assert await ids(owner=users["bob"]) == {j.id for j in bobs}
    # Time window: created_at moved to known instants.
    base = dt.datetime(2026, 9, 1, tzinfo=dt.UTC)
    async with engine.begin() as db:
        for i, job in enumerate(jobs):
            await db.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(created_at=base + dt.timedelta(days=i))
            )
    window = await ids(
        created_after=(base + dt.timedelta(days=2)).isoformat(),
        created_before=(base + dt.timedelta(days=4)).isoformat(),
    )
    assert window == {jobs[2].id, jobs[3].id}  # after inclusive, before exclusive


@pytest.mark.parametrize(
    "params",
    [
        {"cursor": "not-a-cursor"},
        {"cursor": encode_cursor(dt.datetime(2026, 1, 1, tzinfo=dt.UTC), "x")},
        {"cursor": "djJ8MjAyNi0wMS0wMXxqb2Jf"},  # v2|... wrong version
        {"cursor": "x" * 161},
        {"created_after": "2026-10-01T00:00:00"},  # no zone
        {"owner": "bob"},
        {"node": "node_nope"},
        {"state": "flying"},
        {"limit": "0"},
        {"limit": "501"},
    ],
)
async def test_bad_paging_input_is_400(
    anon: httpx.AsyncClient, users: dict[str, str], params: dict[str, str]
) -> None:
    admin = await login(anon, "admin", ADMIN_PW)
    response = await anon.get("/v1/jobs", params=params, headers=bearer(admin))
    assert response.status_code in (400, 422), (params, response.text)
    assert response.json()["error"]["code"] == "invalid_input"


async def test_state_filter_values_are_job_states(
    anon: httpx.AsyncClient, users: dict[str, str]
) -> None:
    admin = await login(anon, "admin", ADMIN_PW)
    for state in JobState:
        response = await anon.get(
            "/v1/jobs", params={"state": state.value}, headers=bearer(admin)
        )
        assert response.status_code == 200
