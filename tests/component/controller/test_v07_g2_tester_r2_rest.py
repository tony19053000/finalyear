"""V0.7 G2 tester round 2: D152 auth-before-body on the invite endpoints, and the
new empty-JSON-body 400 (real controller runtime, real PostgreSQL)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_controller_harness import make_settings
from mf_fabric import runtime_verify
from sqlalchemy import text

from meshforge.common.fabric_token import read_token_file
from meshforge.common.ids import new_invite_id
from meshforge.controller.fabric_ca import create_fabric
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

JSON = {"content-type": "application/json"}


@pytest_asyncio.fixture
async def rt(pg_settings: DatabaseSettings) -> AsyncIterator[ControllerRuntime]:
    settings = make_settings(pg_settings, fabric=False, enroll={"bind": "127.0.0.1:0"})
    create_fabric(settings.ca_dir)
    async with ControllerRuntime.start(settings) as runtime:
        yield runtime


async def _count(pg: DatabaseSettings) -> int:
    engine = create_engine(pg)
    try:
        async with engine.begin() as conn:
            return int(
                (
                    await conn.execute(text("SELECT count(*) FROM join_token"))
                ).scalar_one()
            )
    finally:
        await engine.dispose()


async def test_401_comes_first_for_every_body_and_path_shape(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    bodies = [
        b"",
        b"{",
        b"null",
        b"[]",
        b'{"ttl_s": 59}',
        b'{"replace_node_id": 5}',
        b"\xff\xfe",
        b"x" * 2_000_000,
        b"{" * 100_000,
    ]
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{rt.http_port}", verify=runtime_verify(rt)
    ) as http:
        for body in bodies:
            for headers in (
                JSON,
                {},
                {"content-type": "text/plain"},
                {"content-type": "application/x-www-form-urlencoded"},
            ):
                r = await http.post("/v1/fabric/invites", content=body, headers=headers)
                assert r.status_code == 401, (
                    body[:10],
                    headers,
                    r.status_code,
                    r.text[:120],
                )
                assert "ttl" not in r.text.lower()
        for bad in ("x" * 65, "x" * 5000, "inv_%00", str(new_invite_id())):
            r = await http.post(
                f"/v1/fabric/invites/{bad}/revoke", content=b"{", headers=JSON
            )
            assert r.status_code == 401, (bad[:10], r.status_code)
    assert await _count(pg_settings) == 0


async def test_authenticated_body_rules_after_the_change(
    rt: ControllerRuntime, pg_settings: DatabaseSettings
) -> None:
    token = read_token_file(rt.settings.token_path).get_secret_value()
    auth = {"authorization": f"Bearer {token}"}
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{rt.http_port}", verify=runtime_verify(rt)
    ) as http:
        # no body, no content type -> defaults (compat with plain curl -X POST)
        assert (await http.post("/v1/fabric/invites", headers=auth)).status_code == 201
        # {} -> defaults (what `compute fabric invite` sends)
        assert (
            await http.post("/v1/fabric/invites", json={}, headers=auth)
        ).status_code == 201
        # declared JSON but empty / whitespace -> the new 400
        for content in (b"", b"  \n"):
            r = await http.post(
                "/v1/fabric/invites", content=content, headers={**auth, **JSON}
            )
            assert r.status_code == 400, r.text
        # non-JSON content type with an empty body is still "defaults"
        r = await http.post(
            "/v1/fabric/invites",
            content=b"",
            headers={**auth, "content-type": "text/plain"},
        )
        assert r.status_code == 201
        before = await _count(pg_settings)
        for content, expect in (
            (b"null", 400), (b"[]", 400), (b'"x"', 400), (b"{", 400), (b'{"ttl_s": 59}', 400),
            (b'{"ttl_s": 86401}', 400), (b'{"ttl_s": true}', 400), (b'{"replace_node_id": 5}', 400),
            (b'{"ttl_s": 60, "extra": 1}', None), (b"x" * 4097, 400), (b" " * 5000 + b"{}", 400),
            (b'{"ttl_s": 60}', 201), (b'{"ttl_s": 86400}', 201), (b'{"ttl_s": 1e2}', 201),
        ):  # fmt: skip
            r = await http.post(
                "/v1/fabric/invites", content=content, headers={**auth, **JSON}
            )
            assert r.status_code < 500, (content[:20], r.status_code)
            if expect is not None:
                assert r.status_code == expect, (
                    content[:20],
                    r.status_code,
                    r.text[:150],
                )
        created = await _count(pg_settings) - before
        assert created >= 3
        # revoke: long id -> 400 (not 5xx, not 404-with-echo); unknown -> 404
        r = await http.post("/v1/fabric/invites/" + "x" * 65 + "/revoke", headers=auth)
        assert r.status_code == 400 and "x" * 65 not in r.text
        r = await http.post(
            f"/v1/fabric/invites/{new_invite_id()}/revoke", headers=auth
        )
        assert r.status_code == 404
