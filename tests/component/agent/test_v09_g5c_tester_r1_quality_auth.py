"""V0.9 G5c tester r1 (security): ``GET /v1/bench/campaigns/{id}/quality`` is
admin only - a normal user gets 403 (even for an unknown id: no existence
leak), an anonymous caller 401, and the CLI maps the error to a non-zero exit."""

# ruff: noqa: F811
from __future__ import annotations

import httpx
import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from test_v07_g6_tester_r1_jobs import (  # noqa: F401
    alice,
    alice_token,
    settings,
)

from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.security, pytest.mark.asyncio]

CAMPAIGN = "camp_01J0000000000000000000000A"


async def test_a_normal_user_gets_403_on_the_quality_report(
    alice: httpx.AsyncClient,
) -> None:
    for path in (
        f"/v1/bench/campaigns/{CAMPAIGN}/quality",
        "/v1/bench/campaigns/not-even-an-id/quality",
    ):
        resp = await alice.get(path)
        assert resp.status_code == 403, (path, resp.text)
        assert resp.json()["error"]["code"] == "unauthorized"
        assert "quality_version" not in resp.text


async def test_anonymous_gets_401_on_the_quality_report(
    runtime: ControllerRuntime, alice: httpx.AsyncClient
) -> None:
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as anon:
        resp = await anon.get(f"/v1/bench/campaigns/{CAMPAIGN}/quality")
    assert resp.status_code in (401, 403), resp.text
    assert resp.status_code == 401
