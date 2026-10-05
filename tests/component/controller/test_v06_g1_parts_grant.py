"""V0.6 G1 / D114 over real HTTP: resumable part uploads under an upload grant.

Real uvicorn + PostgreSQL + the real blob store. Checked on the wire:

* an attempt's grant may use the parts API (status, parts, complete); the
  ``complete`` that assembles and verifies the blob credits it to the grant
  (``credited: true``), a repeat is free;
* parts sent under a grant the controller no longer knows (a restart) are kept;
  a new grant of the same attempt completes them and is credited (resume);
* a blob already stored before ``complete`` is NOT credited (``credited:
  false``): nothing proves this attempt sent it - a plain ``PUT`` under the
  grant then is (even when the grant had reserved it through a part before);
* the quota holds for parts: the whole blob is reserved on its first part
  (max accepted, max+1 refused, blob count too), a part declaring another blob
  size than the reservation is refused;
* a closed grant is 403, an unknown token / none / a fetch token 401.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of

from meshforge.common.cas import MIN_CHUNK_SIZE, chunk_count, chunk_span
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.asyncio

CS = MIN_CHUNK_SIZE
MIB = 1024 * 1024


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, blobs={"max_blob_bytes": 4 * MIB})  # type: ignore[arg-type]
    async with running_controller(settings) as rt:
        yield rt


async def _part(
    client: httpx.AsyncClient,
    data: bytes,
    index: int,
    *,
    size: int | None = None,
    digest: Digest | None = None,
) -> httpx.Response:
    size = len(data) if size is None else size
    digest = sha256_bytes(data) if digest is None else digest
    start, length = chunk_span(index, size, CS)
    body = data[start : start + length]
    return await client.put(
        f"/v1/blobs/{digest.hex}/parts/{index}",
        content=body,
        headers={
            "content-type": "application/octet-stream",
            "x-meshforge-blob-size": str(size),
            "x-meshforge-chunk-size": str(CS),
            "x-meshforge-chunk-sha256": hashlib.sha256(body).hexdigest(),
        },
    )


async def _parts(client: httpx.AsyncClient, data: bytes, indices: range) -> None:
    for index in indices:
        sent = await _part(client, data, index)
        assert sent.status_code in (200, 201), sent.text


async def _complete(client: httpx.AsyncClient, data: bytes) -> httpx.Response:
    return await client.post(f"/v1/blobs/{sha256_bytes(data).hex}/complete")


def _grant(runtime: ControllerRuntime, attempt: str, **quota: int) -> str:
    assert runtime.upload_grants is not None
    quota.setdefault("max_blobs", 4)
    quota.setdefault("max_bytes", 4 * MIB)
    return runtime.upload_grants.mint(attempt, ttl_s=60, **quota).get_secret_value()


async def test_a_grant_uploads_parts_and_complete_credits_it(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(3 * CS + 5)
    digest = sha256_bytes(data)
    token = _grant(runtime, "att_parts")
    async with api_client(runtime, token=token) as agent:
        assert (await agent.get(f"/v1/blobs/{digest.hex}/parts")).status_code == 404
        await _parts(agent, data, range(2))
        status = (await agent.get(f"/v1/blobs/{digest.hex}/parts")).json()
        assert status["received"] == [0, 1] and status["complete"] is False
        missing = await _complete(agent, data)
        assert missing.status_code == 409  # parts 2, 3 still missing
        await _parts(agent, data, range(2, chunk_count(len(data), CS)))
        done = await _complete(agent, data)
        assert done.status_code == 201, done.text
        assert done.json()["credited"] is True
        again = await _complete(agent, data)
        assert again.status_code == 200 and again.json()["credited"] is True
    assert runtime.upload_grants is not None
    assert runtime.upload_grants.uploaded("att_parts", digest)


async def test_parts_sent_under_a_forgotten_grant_are_completed_by_the_new_one(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(4 * CS)
    old = _grant(runtime, "att_restart")
    async with api_client(runtime, token=old) as agent:
        await _parts(agent, data, range(3))
    assert runtime.upload_grants is not None
    runtime.upload_grants.revoke("att_restart")  # what a restart does to it
    new = _grant(runtime, "att_restart")
    async with api_client(runtime, token=old) as stale:
        refused = await stale.get(f"/v1/blobs/{sha256_bytes(data).hex}/parts")
        assert refused.status_code == 401
    async with api_client(runtime, token=new) as agent:
        status = (await agent.get(f"/v1/blobs/{sha256_bytes(data).hex}/parts")).json()
        assert status["received"] == [0, 1, 2]
        await _parts(agent, data, range(3, 4))
        done = await _complete(agent, data)
        assert done.status_code == 201 and done.json()["credited"] is True
    assert runtime.upload_grants.uploaded("att_restart", sha256_bytes(data))


async def test_a_blob_stored_before_complete_is_not_credited_until_put(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(2 * CS + 1)
    digest = sha256_bytes(data)
    token = _grant(runtime, "att_dup")
    async with api_client(runtime, token=token) as agent:
        await _parts(agent, data, range(1))  # reserves the blob under the grant
        async with api_client(runtime, token=token_of(runtime)) as admin:
            stored = await admin.put(f"/v1/blobs/{digest.hex}", content=data)
            assert stored.status_code == 201
        status = (await agent.get(f"/v1/blobs/{digest.hex}/parts")).json()
        assert status["complete"] is True
        done = await _complete(agent, data)
        assert done.status_code == 200, done.text
        assert done.json()["credited"] is False
        assert runtime.upload_grants is not None
        assert not runtime.upload_grants.uploaded("att_dup", digest)
        # the proof: the bytes under the grant (the part's reservation was
        # given back, so this is not refused as a second reservation)
        proved = await agent.put(f"/v1/blobs/{digest.hex}", content=data)
        assert proved.status_code == 200, proved.text
        assert runtime.upload_grants.uploaded("att_dup", digest)


async def test_part_uploads_are_bounded_by_the_grant_quota(
    runtime: ControllerRuntime,
) -> None:
    exact = os.urandom(2 * CS)
    over = os.urandom(2 * CS + 1)
    token = _grant(runtime, "att_quota", max_bytes=2 * CS, max_blobs=1)
    async with api_client(runtime, token=token) as agent:
        refused = await _part(agent, over, 0)
        assert refused.status_code == 413, refused.text  # max + 1
        assert refused.json()["error"]["code"] == "invalid_input"
        await _parts(agent, exact, range(2))  # max accepted
        assert (await _complete(agent, exact)).json()["credited"] is True
        another = os.urandom(CS + 1)
        second = await _part(agent, another, 0)
        assert second.status_code == 413  # blob quota: 1
    assert runtime.blobs is not None
    assert runtime.blobs.size(sha256_bytes(over)) is None


async def test_a_part_declaring_another_size_than_reserved_is_refused(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(2 * CS)
    digest = sha256_bytes(data)
    token = _grant(runtime, "att_size", max_bytes=3 * CS)
    async with api_client(runtime, token=token) as agent:
        await _parts(agent, data, range(1))
        bigger = data + os.urandom(CS)  # same name, larger declared size
        refused = await _part(agent, bigger, 2, size=3 * CS, digest=digest)
        assert refused.status_code == 400, refused.text


async def test_closed_unknown_missing_and_fetch_tokens_are_refused(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(2 * CS)
    digest = sha256_bytes(data)
    token = _grant(runtime, "att_closed")
    assert runtime.upload_grants is not None
    runtime.upload_grants.close("att_closed", grace_s=60)
    assert runtime.fetch_tokens is not None
    fetch = runtime.fetch_tokens.mint("att_f", [digest], ttl_s=60).get_secret_value()
    async with api_client(runtime, token=token) as closed:
        assert (await _part(closed, data, 0)).status_code == 403
        assert (await _complete(closed, data)).status_code in (403, 404)
    for bad in (None, "not-a-grant-" + "x" * 32, fetch):
        async with api_client(runtime, token=bad) as client:
            assert (await _part(client, data, 0)).status_code == 401
            got = await client.get(f"/v1/blobs/{digest.hex}/parts")
            assert got.status_code == 401
    assert runtime.blobs is not None
    status = await runtime.blobs.part_status(digest)
    assert status is None  # nothing was written by any of them


async def test_the_fabric_token_still_uses_parts_without_a_grant(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(2 * CS + 7)
    async with api_client(runtime, token=token_of(runtime)) as admin:
        await _parts(admin, data, range(3))
        done = await _complete(admin, data)
        assert done.status_code == 201 and done.json()["credited"] is True
