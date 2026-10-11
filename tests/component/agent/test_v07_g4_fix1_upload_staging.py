"""D157 T-2: resumable upload parts are bound to the uploader.

Real PostgreSQL + a real controller (HTTPS). Alice and Bob upload the same
digest through ``/v1/blobs/{sha}/parts``: each has an own staging, so Bob can
neither plant a part in Alice's upload nor see, complete or reset it.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY
from mf_users import bearer, create_user, login
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.hashing import Digest
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(120)]

CHUNK = 64 * 1024
DATA = b"".join(bytes([i % 251]) * CHUNK for i in range(3))
DIGEST = hashlib.sha256(DATA).hexdigest()
PARTS = [DATA[i * CHUNK : (i + 1) * CHUNK] for i in range(3)]


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(pg_settings, auth=USERS_ONLY)


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def clients(
    runtime: ControllerRuntime, engine: AsyncEngine
) -> AsyncIterator[tuple[httpx.AsyncClient, httpx.AsyncClient]]:
    await create_user(engine, "alice", "alice-password-1")
    await create_user(engine, "bob", "bob-password-123")
    base = f"https://127.0.0.1:{runtime.http_port}"
    async with httpx.AsyncClient(base_url=base, timeout=30) as anon:
        alice_token = await login(anon, "alice", "alice-password-1")
        bob_token = await login(anon, "bob", "bob-password-123")
    async with (
        httpx.AsyncClient(base_url=base, headers=bearer(alice_token), timeout=30) as a,
        httpx.AsyncClient(base_url=base, headers=bearer(bob_token), timeout=30) as b,
    ):
        yield a, b


async def _put(client: httpx.AsyncClient, index: int, body: bytes) -> httpx.Response:
    return await client.put(
        f"/v1/blobs/{DIGEST}/parts/{index}",
        content=body,
        headers={
            "x-meshforge-blob-size": str(len(DATA)),
            "x-meshforge-chunk-size": str(CHUNK),
            "x-meshforge-chunk-sha256": hashlib.sha256(body).hexdigest(),
        },
    )


async def test_another_users_parts_status_and_complete_never_touch_your_upload(
    runtime: ControllerRuntime,
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    alice, bob = clients
    for index in (0, 1):
        assert (await _put(alice, index, PARTS[index])).status_code == 201
    # Bob sees no upload in progress and cannot complete Alice's.
    assert (await bob.get(f"/v1/blobs/{DIGEST}/parts")).status_code == 404
    assert (await bob.post(f"/v1/blobs/{DIGEST}/complete")).status_code == 404
    # Bob's planted part (honest chunk digest of garbage) lands in Bob's staging.
    garbage = b"\x66" * CHUNK
    assert (await _put(bob, 2, garbage)).status_code == 201
    status = (await alice.get(f"/v1/blobs/{DIGEST}/parts")).json()
    assert status["received"] == [0, 1] and status["complete"] is False
    bobs = (await bob.get(f"/v1/blobs/{DIGEST}/parts")).json()
    assert bobs["received"] == [2]
    # Bob's complete fails on his own parts only (409: his 0 and 1 are missing).
    assert (await bob.post(f"/v1/blobs/{DIGEST}/complete")).status_code == 409
    # Alice finishes: 201, the blob is stored and correct.
    assert (await _put(alice, 2, PARTS[2])).status_code == 201
    done = await alice.post(f"/v1/blobs/{DIGEST}/complete")
    assert done.status_code == 201, done.text
    assert runtime.blobs is not None
    assert runtime.blobs.size(Digest(DIGEST)) == len(DATA)
    assert (
        await alice.get(f"/v1/blobs/{DIGEST}")
    ).status_code == 404  # not hers to read


async def test_a_completed_blob_drops_the_other_uploaders_staging(
    runtime: ControllerRuntime,
    clients: tuple[httpx.AsyncClient, httpx.AsyncClient],
) -> None:
    alice, bob = clients
    assert (await _put(bob, 0, PARTS[0])).status_code == 201
    for index in range(3):
        assert (await _put(alice, index, PARTS[index])).status_code == 201
    assert (await alice.post(f"/v1/blobs/{DIGEST}/complete")).status_code == 201
    assert runtime.blobs is not None
    assert runtime.blobs.cas.resumes_in_progress() == 1  # Bob's, until he asks
    # Stored now: complete for everyone (existence is public, CB-E3) ...
    assert (await bob.get(f"/v1/blobs/{DIGEST}/parts")).json()["complete"] is True
    # ... and Bob's complete answers 200 and drops his own staging.
    assert (await bob.post(f"/v1/blobs/{DIGEST}/complete")).status_code == 200
    assert runtime.blobs.cas.resumes_in_progress() == 0
