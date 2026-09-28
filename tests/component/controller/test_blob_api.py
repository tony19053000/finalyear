"""``/v1/blobs`` over real HTTP (D48): verify-then-rename, no partial blobs, auth.

Real controller process pieces (uvicorn + PostgreSQL); the interrupted-upload test
closes a raw TCP socket half-way through a body - the failure happens for real.
"""

from __future__ import annotations

import asyncio
import hashlib
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


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


def _blob_root(runtime: ControllerRuntime) -> Path:
    assert runtime.blobs is not None
    return runtime.blobs.root


async def test_put_head_get_round_trip_and_idempotency(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(200_000)
    digest = sha256_bytes(data)
    async with api_client(runtime, token=token_of(runtime)) as client:
        head = await client.head(f"/v1/blobs/{digest.hex}")
        assert head.status_code == 404
        put = await client.put(f"/v1/blobs/{digest.hex}", content=data)
        assert put.status_code == 201, put.text
        assert put.json() == {"digest": str(digest), "size": len(data), "created": True}
        again = await client.put(f"/v1/blobs/{digest.hex}", content=data)
        assert again.status_code == 200
        assert again.json()["created"] is False
        head = await client.head(f"/v1/blobs/{digest.hex}")
        assert head.status_code == 200
        assert int(head.headers["content-length"]) == len(data)
        got = await client.get(f"/v1/blobs/{digest.hex}")
        assert got.status_code == 200
        assert got.content == data
        assert got.headers["etag"] == f'"{digest}"'


async def test_wrong_digest_is_integrity_error_and_nothing_is_kept(
    runtime: ControllerRuntime,
) -> None:
    claimed = sha256_bytes(b"the name")
    async with api_client(runtime, token=token_of(runtime)) as client:
        put = await client.put(f"/v1/blobs/{claimed.hex}", content=b"other bytes")
        assert put.status_code == 400
        assert put.json()["error"]["code"] == "integrity_error"
        assert (await client.head(f"/v1/blobs/{claimed.hex}")).status_code == 404
    assert _files(_blob_root(runtime)) == []


async def test_size_cap_max_ok_and_max_plus_one_refused(
    runtime: ControllerRuntime,
) -> None:
    exact = b"a" * MIB
    over = b"b" * (MIB + 1)
    async with api_client(runtime, token=token_of(runtime)) as client:
        ok = await client.put(f"/v1/blobs/{sha256_bytes(exact).hex}", content=exact)
        assert ok.status_code == 201
        big = await client.put(f"/v1/blobs/{sha256_bytes(over).hex}", content=over)
        assert big.status_code == 413
        assert big.json()["error"]["code"] == "invalid_input"

        async def chunked() -> AsyncIterator[bytes]:  # no Content-Length at all
            for i in range(0, len(over), 64 * 1024):
                yield over[i : i + 64 * 1024]

        streamed = await client.put(
            f"/v1/blobs/{sha256_bytes(over).hex}", content=chunked()
        )
        assert streamed.status_code == 413
    stored = _files(_blob_root(runtime) / "sha256")
    assert len(stored) == 1
    assert list((_blob_root(runtime) / "tmp").iterdir()) == []


async def test_interrupted_upload_leaves_no_partial_blob(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(900_000)
    digest = hashlib.sha256(data).hexdigest()
    reader, writer = await asyncio.open_connection("127.0.0.1", runtime.http_port)
    head = (
        f"PUT /v1/blobs/{digest} HTTP/1.1\r\nHost: t\r\n"
        f"Authorization: Bearer {token_of(runtime)}\r\n"
        f"Content-Length: {len(data)}\r\n\r\n"
    ).encode()
    writer.write(head + data[:300_000])
    await writer.drain()
    tmp = _blob_root(runtime) / "tmp"
    for _ in range(100):  # the server really started writing a part file
        if list(tmp.iterdir()):
            break
        await asyncio.sleep(0.02)
    assert list(tmp.iterdir()), "upload never reached the store"
    writer.close()  # the client dies mid-body
    await writer.wait_closed()
    for _ in range(250):
        if not list(tmp.iterdir()):
            break
        await asyncio.sleep(0.02)
    assert list(tmp.iterdir()) == []
    assert _files(_blob_root(runtime)) == []
    async with api_client(runtime, token=token_of(runtime)) as client:
        assert (await client.head(f"/v1/blobs/{digest}")).status_code == 404
    del reader


async def test_every_blob_call_needs_a_credential(runtime: ControllerRuntime) -> None:
    data = b"guarded"
    digest = sha256_bytes(data)
    async with api_client(runtime) as anonymous:
        for method in ("PUT", "HEAD", "GET"):
            response = await anonymous.request(
                method,
                f"/v1/blobs/{digest.hex}",
                content=data if method == "PUT" else None,
            )
            assert response.status_code == 401, method
    async with api_client(runtime, token="x" * 43) as wrong:
        assert (
            await wrong.put(f"/v1/blobs/{digest.hex}", content=data)
        ).status_code == 401
    assert _files(_blob_root(runtime)) == []


async def test_fetch_token_is_scoped_to_its_digests(runtime: ControllerRuntime) -> None:
    mine, other = b"bundle for the attempt", b"somebody else's bundle"
    async with api_client(runtime, token=token_of(runtime)) as admin:
        for data in (mine, other):
            put = await admin.put(f"/v1/blobs/{sha256_bytes(data).hex}", content=data)
            assert put.status_code == 201
    assert runtime.fetch_tokens is not None
    fetch = runtime.fetch_tokens.mint("att_x", [sha256_bytes(mine)], ttl_s=60)
    async with api_client(runtime, token=fetch.get_secret_value()) as agent:
        assert (await agent.get(f"/v1/blobs/{sha256_bytes(mine).hex}")).content == mine
        assert (
            await agent.get(f"/v1/blobs/{sha256_bytes(other).hex}")
        ).status_code == 401
        up = await agent.put(f"/v1/blobs/{sha256_bytes(b'out').hex}", content=b"out")
        assert up.status_code == 401  # read-only token
    runtime.fetch_tokens.revoke("att_x")
    async with api_client(runtime, token=fetch.get_secret_value()) as agent:
        assert (
            await agent.get(f"/v1/blobs/{sha256_bytes(mine).hex}")
        ).status_code == 401


@pytest.mark.parametrize("bad", ["abc", "A" * 64, "g" * 64, "0" * 65])
async def test_malformed_digest_is_400(runtime: ControllerRuntime, bad: str) -> None:
    async with api_client(runtime, token=token_of(runtime)) as client:
        response = await client.get(f"/v1/blobs/{bad}")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "invalid_input"
        assert bad not in response.text
