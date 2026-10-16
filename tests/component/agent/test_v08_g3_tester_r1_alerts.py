"""V0.8 G3 tester r1: ``GET /v1/alerts`` under paging churn, cookie sessions,
big pages and hostile cursors (derived from D180/D191 + brief 5c, not the code)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from http.cookies import SimpleCookie

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY, tolerant_liveness
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.controller.enrollment.invites import audit
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine, create_session_factory
from meshforge.controller.store.models.enrollment import AuditAction
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [
    pytest.mark.component,
    pytest.mark.docker,
    pytest.mark.asyncio,
    pytest.mark.security,
    pytest.mark.timeout(240),
]

PW = "admin-password-1"


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings,
        auth={**USERS_ONLY, "login_rate_per_minute": 600.0, "login_rate_burst": 100},
        **tolerant_liveness(),
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as client:
        yield client


@pytest_asyncio.fixture
async def tokens(
    engine: AsyncEngine, runtime: ControllerRuntime, anon: httpx.AsyncClient
) -> dict[str, str]:
    await create_user(engine, "admin", PW, admin=True)
    await create_user(engine, "alice", "alice-password-1")
    return {
        "admin": await login(anon, "admin", PW),
        "alice": await login(anon, "alice", "alice-password-1"),
    }


async def _add(engine: AsyncEngine, n: int, *, action: AuditAction | None = None) -> list[str]:
    sessions = create_session_factory(engine)
    ids: list[str] = []
    async with sessions() as db, db.begin():
        for i in range(n):
            row = await audit(
                db,
                action or AuditAction.ATTEMPT_WRONG_NODE,
                actor="node:n",
                node_id="node_x",
                details={"i": i},
            )
            await db.flush()
            ids.append(str(row.id))
    return ids


async def _cookie_session(
    anon: httpx.AsyncClient, runtime: ControllerRuntime, name: str, pw: str
) -> dict[str, str]:
    origin = f"https://127.0.0.1:{runtime.http_port}"
    r = await anon.post(
        "/v1/session", json={"name": name, "password": pw}, headers={"origin": origin}
    )
    assert r.status_code == 200, r.text
    jar: dict[str, str] = {}
    for raw in r.headers.get_list("set-cookie"):
        c: SimpleCookie = SimpleCookie()
        c.load(raw)
        jar.update({k: v.value for k, v in c.items()})
    return {"cookie": f"mf_session={jar['mf_session']}"}


async def test_inserts_between_pages_never_duplicate_or_skip(
    anon: httpx.AsyncClient, engine: AsyncEngine, tokens: dict[str, str]
) -> None:
    admin = bearer(tokens["admin"])
    original = await _add(engine, 7)
    first = (await anon.get("/v1/alerts", headers=admin, params={"limit": 3})).json()
    await _add(engine, 4)  # new alerts land while the operator pages
    seen = [a["id"] for a in first["alerts"]]
    cursor = first["next_cursor"]
    while cursor is not None:
        page = (
            await anon.get("/v1/alerts", headers=admin, params={"limit": 3, "before": cursor})
        ).json()
        seen += [a["id"] for a in page["alerts"]]
        cursor = page["next_cursor"]
    assert len(seen) == len(set(seen))
    assert seen == original[::-1]  # every original exactly once, in order


async def test_a_page_of_exactly_limit_has_no_cursor_and_200_is_the_cap(
    anon: httpx.AsyncClient, engine: AsyncEngine, tokens: dict[str, str]
) -> None:
    admin = bearer(tokens["admin"])
    ids = await _add(engine, 205)
    exact = (await anon.get("/v1/alerts", headers=admin, params={"limit": 205 - 5})).json()
    assert exact["count"] == 200 and exact["next_cursor"] == ids[5]
    tail = (
        await anon.get("/v1/alerts", headers=admin, params={"limit": 200, "before": ids[5]})
    ).json()
    assert tail["count"] == 5 and tail["next_cursor"] is None
    five = (await anon.get("/v1/alerts", headers=admin, params={"limit": 200, "before": ids[5]})).json()
    assert five["count"] == 5
    # exactly `limit` left -> no phantom next page
    last = (await anon.get("/v1/alerts", headers=admin, params={"limit": 5, "before": ids[5]})).json()
    assert last["count"] == 5 and last["next_cursor"] is None


async def test_non_alert_audit_rows_never_leak(
    anon: httpx.AsyncClient, engine: AsyncEngine, tokens: dict[str, str]
) -> None:
    await _add(engine, 3, action=AuditAction.SECRET_SET)
    await _add(engine, 2, action=AuditAction.LOGIN)
    body = (await anon.get("/v1/alerts", headers=bearer(tokens["admin"]))).json()
    assert body == {"alerts": [], "count": 0, "next_cursor": None}


async def test_cookie_sessions_admin_ok_user_403_no_csrf_needed_for_get(
    anon: httpx.AsyncClient,
    runtime: ControllerRuntime,
    engine: AsyncEngine,
    tokens: dict[str, str],
) -> None:
    await _add(engine, 2)
    admin = await _cookie_session(anon, runtime, "admin", PW)
    ok = await anon.get("/v1/alerts", headers=admin)
    assert ok.status_code == 200 and ok.json()["count"] == 2
    user = await _cookie_session(anon, runtime, "alice", "alice-password-1")
    assert (await anon.get("/v1/alerts", headers=user)).status_code == 403
    # a forged/garbage cookie and a bad bearer are 401, never a free list
    assert (
        await anon.get("/v1/alerts", headers={"cookie": "mf_session=nope"})
    ).status_code == 401
    assert (await anon.get("/v1/alerts", headers=bearer("garbage"))).status_code == 401
    # mutation verbs do not exist on the route
    for verb in ("post", "put", "delete", "patch"):
        r = await getattr(anon, verb)("/v1/alerts", headers=admin)
        assert r.status_code in (403, 404, 405), (verb, r.status_code)


@pytest.mark.parametrize(
    "before",
    ["٣", " 5", "5 ", "5\n", "0x5", "+5", "5.0", "5;DROP TABLE audit_event", "%00", "１２"],
)
async def test_hostile_cursors_are_400(
    anon: httpx.AsyncClient, tokens: dict[str, str], before: str
) -> None:
    r = await anon.get("/v1/alerts", headers=bearer(tokens["admin"]), params={"before": before})
    assert r.status_code == 400, (before, r.text)


async def test_a_cursor_past_the_newest_or_below_the_oldest_is_not_an_error(
    anon: httpx.AsyncClient, engine: AsyncEngine, tokens: dict[str, str]
) -> None:
    admin = bearer(tokens["admin"])
    ids = await _add(engine, 3)
    above = (await anon.get("/v1/alerts", headers=admin, params={"before": str(10**15)})).json()
    assert [a["id"] for a in above["alerts"]] == ids[::-1]
    below = (await anon.get("/v1/alerts", headers=admin, params={"before": ids[0]})).json()
    assert below["alerts"] == []


async def test_concurrent_readers_agree(
    anon: httpx.AsyncClient, engine: AsyncEngine, tokens: dict[str, str]
) -> None:
    admin = bearer(tokens["admin"])
    await _add(engine, 20)
    results = await asyncio.gather(
        *(anon.get("/v1/alerts", headers=admin, params={"limit": 10}) for _ in range(16))
    )
    assert {r.text for r in results} and len({r.text for r in results}) == 1
