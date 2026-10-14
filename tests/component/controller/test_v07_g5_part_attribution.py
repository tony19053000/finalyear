"""V0.7 G5 part 2 (D145a, THREAT_MODEL CB-E1): uploaded parts are attributed to
the authenticated node.

Real PostgreSQL and the real ``ControllerRuntime`` (HTTPS, node certificates
from the fixture CA):

* the parts a node uploads under its grant are kept in **that node's** staging
  (``node:<id>``, the node its certificate proves) - another node uploading the
  same digest under its own grant sees none of them and cannot complete them;
* a grant bound to no node (tests only; dispatch always binds one) is
  attributed to the node whose certificate presents it, never to the attempt.
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


async def _put_part(
    client: httpx.AsyncClient, index: int, digest: str = DIGEST
) -> httpx.Response:
    body = PARTS[index]
    return await client.put(
        f"/v1/blobs/{digest}/parts/{index}",
        content=body,
        headers={
            "x-meshforge-blob-size": str(len(DATA)),
            "x-meshforge-chunk-size": str(CHUNK),
            "x-meshforge-chunk-sha256": hashlib.sha256(body).hexdigest(),
        },
    )


@pytest.mark.security
async def test_parts_are_kept_against_the_proven_node(
    runtime: ControllerRuntime,
) -> None:
    node_a, node_b = str(new_node_id()), str(new_node_id())
    async with (
        _client(runtime, node_a, _grant(runtime, node_a)) as a,
        _client(runtime, node_b, _grant(runtime, node_b)) as b,
    ):
        for index in (0, 1):
            assert (await _put_part(a, index)).status_code == 201
        # node B (its own grant, its own certificate) sees none of A's parts
        assert (await b.get(f"/v1/blobs/{DIGEST}/parts")).status_code == 404
        assert (await b.post(f"/v1/blobs/{DIGEST}/complete")).status_code == 404
        status = (await a.get(f"/v1/blobs/{DIGEST}/parts")).json()
        assert status["received"] == [0, 1]
    blobs = runtime.blobs
    assert blobs is not None
    held = await blobs.part_status(Digest(DIGEST), owner=f"node:{node_a}")
    assert held is not None and list(held.received) == [0, 1]
    assert await blobs.part_status(Digest(DIGEST), owner=f"node:{node_b}") is None


@pytest.mark.security
async def test_a_grant_bound_to_no_node_is_kept_against_the_certificates_node(
    runtime: ControllerRuntime,
) -> None:
    """A grant minted without a node (only tests do that; dispatch always binds
    one) is still attributed: to the node whose certificate presented it, not
    to the attempt - so two nodes using one unbound grant never share parts."""
    node_a, node_b = str(new_node_id()), str(new_node_id())
    unbound = _grant(runtime, None)
    async with _client(runtime, node_a, unbound) as a:
        assert (await _put_part(a, 0)).status_code == 201
    async with _client(runtime, node_b, unbound) as b:
        assert (await b.get(f"/v1/blobs/{DIGEST}/parts")).status_code == 404
    blobs = runtime.blobs
    assert blobs is not None
    held = await blobs.part_status(Digest(DIGEST), owner=f"node:{node_a}")
    assert held is not None and list(held.received) == [0]
