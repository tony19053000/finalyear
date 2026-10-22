"""V0.14 G4 (D298) on a real controller: ``oracle`` is evaluation-only.

``POST /v1/jobs`` with ``placement.policy: oracle`` or a manifest
``policy: oracle`` is refused (``400 invalid_input`` - the API's answer for
any refused body, as for an unknown policy or objective) and writes no job.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from mf_jobs_harness import g2_settings, running_controller, token_of

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from sqlalchemy import text

from meshforge.common.hashing import sha256_bytes
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.component, pytest.mark.asyncio, pytest.mark.timeout(120)]

BUNDLE = os.urandom(20_000)


@pytest_asyncio.fixture
async def http(pg_settings: DatabaseSettings) -> AsyncIterator[tuple[Any, str]]:
    settings = g2_settings(pg_settings, jobs={"dispatch_interval_s": 0.05})
    async with (
        running_controller(settings) as runtime,
        httpx.AsyncClient(
            base_url=f"https://127.0.0.1:{runtime.http_port}",
            headers={"authorization": f"Bearer {token_of(runtime)}"},
            timeout=10,
        ) as client,
    ):
        digest = sha256_bytes(BUNDLE)
        put = await client.put(f"/v1/blobs/{digest.hex}", content=BUNDLE)
        assert put.status_code == 201
        yield client, str(digest)


async def _jobs(pg: DatabaseSettings) -> int:
    engine = create_engine(pg)
    try:
        async with engine.connect() as conn:
            return int(
                (await conn.execute(text("SELECT count(*) FROM job"))).scalar_one()
            )
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    "where", ["placement", "manifest"], ids=["request", "manifest"]
)
async def test_oracle_is_refused_at_submit(
    http: tuple[Any, str], pg_settings: DatabaseSettings, where: str
) -> None:
    client, bundle = http
    manifest: dict[str, Any] = {"name": "demo", "entrypoint": "python main.py"}
    body: dict[str, Any] = {"manifest": manifest, "bundle_digest": bundle}
    if where == "placement":
        body["placement"] = {"policy": "oracle"}
    else:
        manifest["policy"] = "oracle"
    refused = await client.post("/v1/jobs", json=body)
    assert refused.status_code == 400, refused.text
    error = refused.json()["error"]
    assert error["code"] == "invalid_input" and "policy" in error["message"]
    assert await _jobs(pg_settings) == 0
    # the evaluator's API exists on the same controller (admin) and lists nothing yet
    listed = await client.get("/v1/planner/eval-reports")
    assert listed.status_code == 200 and listed.json() == {"reports": []}
