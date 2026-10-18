"""V0.9 G3 lane B item 5 (security): every ``/v1/bench`` route is admin only -
a campaign is job submission at scale on every owner's laptop (THREAT_MODEL)."""

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

pytestmark = [pytest.mark.component, pytest.mark.security, pytest.mark.asyncio]

CAMPAIGN = "camp_01J0000000000000000000000A"


async def test_a_normal_user_gets_403_on_every_bench_route(
    alice: httpx.AsyncClient,
) -> None:
    calls = [
        ("POST", "/v1/bench/campaigns", {"json": {"spec": "name: x\n"}}),
        ("GET", "/v1/bench/campaigns", {}),
        ("GET", f"/v1/bench/campaigns/{CAMPAIGN}", {}),
        ("GET", f"/v1/bench/campaigns/{CAMPAIGN}/runs", {}),
        ("POST", f"/v1/bench/campaigns/{CAMPAIGN}/submit", {"params": {"count": 1}}),
    ]
    for method, path, kwargs in calls:
        resp = await alice.request(method, path, **kwargs)
        assert resp.status_code == 403, (path, resp.text)
    # the API's 403 code is "unauthorized" (typed failure_type, D141)
    assert resp.json()["error"]["code"] == "unauthorized"
