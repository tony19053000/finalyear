"""V0.4 G5 D73 over real HTTP: who may write to the blob store.

Real uvicorn + PostgreSQL. Checked on the wire: a fetch token (download-only)
cannot PUT; an upload grant can, within its blob and byte quota (max accepted,
max+1 refused), never after its attempt ended, never without Content-Length; the
fabric token stays unlimited; a refused upload leaves nothing behind.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of

from meshforge.common.hashing import sha256_bytes
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.asyncio

MIB = 1024 * 1024


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, blobs={"max_blob_bytes": MIB})  # type: ignore[arg-type]
    async with running_controller(settings) as rt:
        yield rt


def _stored(runtime: ControllerRuntime) -> list[Path]:
    assert runtime.blobs is not None
    root = runtime.blobs.root / "sha256"
    return sorted(p for p in root.rglob("*") if p.is_file())


async def test_a_fetch_token_can_read_but_never_upload(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(1000)
    digest = sha256_bytes(data)
    async with api_client(runtime, token=token_of(runtime)) as admin:
        assert (await admin.put(f"/v1/blobs/{digest.hex}", content=data)).is_success
    assert runtime.fetch_tokens is not None
    token = runtime.fetch_tokens.mint("att_x", [digest], ttl_s=60)
    other = os.urandom(10)
    async with api_client(runtime, token=token.get_secret_value()) as agent:
        assert (await agent.get(f"/v1/blobs/{digest.hex}")).content == data
        refused = await agent.put(f"/v1/blobs/{sha256_bytes(other).hex}", content=other)
        assert refused.status_code == 401
        same = await agent.put(f"/v1/blobs/{digest.hex}", content=data)
        assert same.status_code == 401  # even its own digest
    assert len(_stored(runtime)) == 1


async def test_an_upload_grant_is_bounded_by_its_quota(
    runtime: ControllerRuntime,
) -> None:
    assert runtime.upload_grants is not None
    grant = runtime.upload_grants.mint("att_q", ttl_s=60, max_blobs=2, max_bytes=3000)
    a, b, c = os.urandom(1000), os.urandom(2000), os.urandom(1)
    async with api_client(runtime, token=grant.get_secret_value()) as agent:
        over = os.urandom(2001)
        assert (
            await agent.put(f"/v1/blobs/{sha256_bytes(a).hex}", content=a)
        ).is_success
        too_big = await agent.put(f"/v1/blobs/{sha256_bytes(over).hex}", content=over)
        assert too_big.status_code == 413, too_big.text  # 1000 + 2001 > 3000
        assert too_big.json()["error"]["code"] == "invalid_input"
        ok = await agent.put(f"/v1/blobs/{sha256_bytes(b).hex}", content=b)
        assert ok.status_code == 201  # exactly 3000
        third = await agent.put(f"/v1/blobs/{sha256_bytes(c).hex}", content=c)
        assert third.status_code == 413  # blob quota (and bytes)
        retry = await agent.put(f"/v1/blobs/{sha256_bytes(a).hex}", content=a)
        assert retry.status_code == 200  # a retry of a stored digest is free
        # the grant cannot read what it wrote (download needs a fetch token)
        assert (await agent.get(f"/v1/blobs/{sha256_bytes(a).hex}")).status_code == 401
    assert runtime.upload_grants.uploaded("att_q", sha256_bytes(a))
    assert runtime.upload_grants.uploaded("att_q", sha256_bytes(b))
    assert len(_stored(runtime)) == 2


async def test_no_upload_after_the_attempt_ended_nor_without_length(
    runtime: ControllerRuntime,
) -> None:
    assert runtime.upload_grants is not None
    grant = runtime.upload_grants.mint("att_e", ttl_s=60, max_blobs=3, max_bytes=MIB)
    data = os.urandom(100)
    url = f"/v1/blobs/{sha256_bytes(data).hex}"
    async with api_client(runtime, token=grant.get_secret_value()) as agent:

        async def chunked() -> AsyncIterator[bytes]:
            yield data

        no_length = await agent.put(url, content=chunked())
        assert no_length.status_code == 411, no_length.text
        runtime.upload_grants.close("att_e", grace_s=30)
        late = await agent.put(url, content=data)
        assert late.status_code == 403, late.text
        assert late.json()["error"]["code"] == "unauthorized"
    assert _stored(runtime) == []


async def test_the_fabric_token_is_not_limited_by_grants(
    runtime: ControllerRuntime,
) -> None:
    async with api_client(runtime, token=token_of(runtime)) as admin:
        for _ in range(5):
            data = os.urandom(1000)
            put = await admin.put(f"/v1/blobs/{sha256_bytes(data).hex}", content=data)
            assert put.status_code == 201
