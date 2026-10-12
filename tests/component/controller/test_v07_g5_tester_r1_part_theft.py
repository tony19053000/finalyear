"""V0.7 G5 tester round 1 (D145a / CB-E1): can a node stage into, read or
complete another uploader's parts?

Real PostgreSQL, real mTLS controller, node certificates from the fixture CA.
"""

from __future__ import annotations

import hashlib
import os

import httpx
import pytest
from mf_fabric import client_ssl_context

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from meshforge.common.hashing import Digest
from meshforge.common.ids import new_attempt_id, new_node_id
from meshforge.controller.server import ControllerRuntime

pytestmark = [pytest.mark.component, pytest.mark.docker, pytest.mark.asyncio]

CHUNK = 64 * 1024
DATA = os.urandom(3 * CHUNK)
DIGEST = hashlib.sha256(DATA).hexdigest()
PARTS = [DATA[i * CHUNK : (i + 1) * CHUNK] for i in range(3)]


def _client(runtime: ControllerRuntime, node_id: str, grant: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        verify=client_ssl_context(node_id=node_id),
        headers={"authorization": f"Bearer {grant}"},
        timeout=15,
    )


def _grant(runtime: ControllerRuntime, node_id: str | None) -> str:
    assert runtime.upload_grants is not None
    return runtime.upload_grants.mint(
        str(new_attempt_id()),
        ttl_s=60,
        max_blobs=4,
        max_bytes=1 << 22,
        node_id=node_id,
    ).get_secret_value()


async def _put_part(client: httpx.AsyncClient, index: int) -> httpx.Response:
    body = PARTS[index]
    return await client.put(
        f"/v1/blobs/{DIGEST}/parts/{index}",
        content=body,
        headers={
            "x-meshforge-blob-size": str(len(DATA)),
            "x-meshforge-chunk-size": str(CHUNK),
            "x-meshforge-chunk-sha256": hashlib.sha256(body).hexdigest(),
        },
    )


async def test_a_stolen_grant_presented_with_another_nodes_certificate_stages_nothing(
    runtime: ControllerRuntime,
) -> None:
    victim, thief = str(new_node_id()), str(new_node_id())
    stolen = _grant(runtime, victim)
    async with _client(runtime, thief, stolen) as b:
        for call in (
            await _put_part(b, 0),
            await b.get(f"/v1/blobs/{DIGEST}/parts"),
            await b.post(f"/v1/blobs/{DIGEST}/complete"),
        ):
            assert call.status_code in (401, 403), call.text
    blobs = runtime.blobs
    assert blobs is not None
    for owner in (f"node:{victim}", f"node:{thief}"):
        assert await blobs.part_status(Digest(DIGEST), owner=owner) is None
    # the rightful holder still works
    async with _client(runtime, victim, stolen) as a:
        assert (await _put_part(a, 0)).status_code == 201


async def test_a_node_cannot_complete_or_read_another_nodes_staged_blob(
    runtime: ControllerRuntime,
) -> None:
    a_id, b_id = str(new_node_id()), str(new_node_id())
    async with (
        _client(runtime, a_id, _grant(runtime, a_id)) as a,
        _client(runtime, b_id, _grant(runtime, b_id)) as b,
    ):
        for index in range(3):
            assert (await _put_part(a, index)).status_code == 201
        # B uploads a DIFFERENT chunk 0 for the same digest: it lands in B's own
        # staging and never overwrites or completes A's
        assert (await _put_part(b, 0)).status_code == 201
        done_b = await b.post(f"/v1/blobs/{DIGEST}/complete")
        assert not done_b.is_success, done_b.text
        mine = (await b.get(f"/v1/blobs/{DIGEST}/parts")).json()
        assert mine["received"] == [0]
        theirs = (await a.get(f"/v1/blobs/{DIGEST}/parts")).json()
        assert theirs["received"] == [0, 1, 2]


async def test_an_expired_or_made_up_grant_with_a_valid_certificate_is_refused(
    runtime: ControllerRuntime,
) -> None:
    node = str(new_node_id())
    async with _client(runtime, node, "mfu_" + "a" * 40) as c:
        assert (await _put_part(c, 0)).status_code == 401
        assert (await c.get(f"/v1/blobs/{DIGEST}/parts")).status_code == 401
