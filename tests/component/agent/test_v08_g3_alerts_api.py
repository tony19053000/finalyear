"""V0.8 G3 item 1 (D180): ``GET /v1/alerts`` - admin only, newest first,
keyset pages by ``before``, 400 on a bad cursor, nothing but alert rows.

Real PostgreSQL and a real HTTPS controller; the audit rows are written with
the controller's own ``audit()`` helper (a WRONG_NODE refusal end to end is
``tests/component/controller/test_v08_g3_alert_log.py``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

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
    pytest.mark.timeout(180),
]

ADMIN_PW = "admin-password-1"
ALICE_PW = "alice-password-1"


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
async def tokens(
    engine: AsyncEngine, runtime: ControllerRuntime, anon: httpx.AsyncClient
) -> dict[str, str]:
    await create_user(engine, "admin", ADMIN_PW, admin=True)
    await create_user(engine, "alice", ALICE_PW)
    return {
        "admin": await login(anon, "admin", ADMIN_PW),
        "alice": await login(anon, "alice", ALICE_PW),
    }


@pytest_asyncio.fixture
async def anon(runtime: ControllerRuntime) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as client:
        yield client


async def _alerts(engine: AsyncEngine, n: int) -> list[str]:
    """``n`` wrong-node audit rows (oldest first), each after a non-alert row."""
    sessions = create_session_factory(engine)
    ids: list[str] = []
    for i in range(n):
        async with sessions() as db, db.begin():
            await audit(db, AuditAction.SECRET_SET, actor="user:x", details={"i": i})
            row = await audit(
                db,
                AuditAction.ATTEMPT_WRONG_NODE,
                actor="node:node_b",
                node_id="node_b",
                details={"attempt_id": f"att_{i}", "reported": "done"},
            )
            await db.flush()
            ids.append(str(row.id))
    return ids


async def test_admin_pages_alerts_newest_first_with_nothing_else(
    anon: httpx.AsyncClient, engine: AsyncEngine, tokens: dict[str, str]
) -> None:
    admin = bearer(tokens["admin"])
    empty = await anon.get("/v1/alerts", headers=admin)
    assert empty.status_code == 200
    assert empty.json() == {"alerts": [], "count": 0, "next_cursor": None}

    ids = await _alerts(engine, 5)
    seen: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        params = {"limit": "2"} | ({} if cursor is None else {"before": cursor})
        page = await anon.get("/v1/alerts", headers=admin, params=params)
        assert page.status_code == 200, page.text
        body = page.json()
        assert body["count"] == len(body["alerts"]) <= 2
        for alert in body["alerts"]:
            assert alert["kind"] == "wrong_node"
            assert alert["node_id"] == "node_b" and alert["actor"] == "node:node_b"
            assert alert["at"].endswith("Z") or alert["at"].endswith("+00:00")
        seen += [a["id"] for a in body["alerts"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert seen == ids[::-1]  # newest first, each once, no SECRET_SET row
    assert pages == 3
    one = (await anon.get("/v1/alerts", headers=admin, params={"limit": 1})).json()
    assert one["alerts"][0]["details"] == {"attempt_id": "att_4", "reported": "done"}


async def test_a_user_is_refused_and_anonymous_is_401(
    anon: httpx.AsyncClient, tokens: dict[str, str]
) -> None:
    assert (await anon.get("/v1/alerts")).status_code == 401
    user = await anon.get("/v1/alerts", headers=bearer(tokens["alice"]))
    assert user.status_code == 403


@pytest.mark.parametrize(
    "params",
    [
        {"before": "abc"},
        {"before": "0"},
        {"before": "-1"},
        {"before": "1e3"},
        {"before": chr(0xFF19)},  # a full-width digit: str.isdigit(), not ASCII
        {"before": "9" * 20},
        {"before": str(1 << 63)},
        {"before": ""},
        {"limit": "0"},
        {"limit": "201"},
    ],
)
async def test_bad_cursor_or_limit_is_400(
    anon: httpx.AsyncClient, tokens: dict[str, str], params: dict[str, str]
) -> None:
    response = await anon.get(
        "/v1/alerts", headers=bearer(tokens["admin"]), params=params
    )
    assert response.status_code == 400, (params, response.text)
    assert response.json()["error"]["code"] == "invalid_input"
