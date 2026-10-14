"""V0.7 G4 tester round 2, T-2: try to break per-uploader staging of part uploads.

Derived from D157 ("resumable upload parts bound to the uploader; other users'
PUT/complete on that staging -> 404, honest retry still works") and CB-E3.
Real PostgreSQL + a real controller (HTTPS) + the real blob store; agents are
upload grants bound to a node (``owner = node:<id>``), users are real logins.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

from mf_agent_harness import make_controller_settings
from mf_controller_harness import USERS_ONLY
from mf_jobs_harness import HARNESS_NODE_ID, api_client
from mf_users import bearer, create_user, login
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from meshforge.common.cas import (
    DEFAULT_MAX_RESUMABLE,
    DEFAULT_MAX_RESUMABLE_PER_OWNER,
    MIN_CHUNK_SIZE,
    chunk_span,
)
from meshforge.common.hashing import Digest
from meshforge.common.ids import new_node_id
from meshforge.controller.server import ControllerRuntime
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.store.engine import create_engine
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = [pytest.mark.asyncio, pytest.mark.timeout(180)]

CS = MIN_CHUNK_SIZE
MIB = 1024 * 1024
OTHER_NODE = str(new_node_id())


@pytest.fixture
def settings(pg_settings: DatabaseSettings) -> ControllerSettings:
    return make_controller_settings(
        pg_settings, auth=USERS_ONLY, blobs={"max_blob_bytes": 8 * MIB}
    )


@pytest_asyncio.fixture
async def engine(pg_settings: DatabaseSettings) -> AsyncIterator[AsyncEngine]:
    made = create_engine(pg_settings)
    yield made
    await made.dispose()


@pytest_asyncio.fixture
async def tokens(runtime: ControllerRuntime, engine: AsyncEngine) -> dict[str, str]:
    pw = {
        "alice": "alice-password-1",
        "bob": "bob-password-123",
        "carol": "carol-pass-1234",
    }
    for name, password in pw.items():
        await create_user(engine, name, password)
    out: dict[str, str] = {}
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as anon:
        for name, password in pw.items():
            out[name] = await login(anon, name, password)
    return out


def _blob(parts: int = 3, tail: int = 17) -> tuple[bytes, str]:
    data = os.urandom((parts - 1) * CS + tail)
    return data, hashlib.sha256(data).hexdigest()


def _span(data: bytes, index: int) -> bytes:
    start, length = chunk_span(index, len(data), CS)
    return data[start : start + length]


async def _put(
    client: httpx.AsyncClient,
    digest: str,
    index: int,
    body: bytes,
    *,
    size: int,
    chunk_hash: str | None = None,
) -> httpx.Response:
    return await client.put(
        f"/v1/blobs/{digest}/parts/{index}",
        content=body,
        headers={
            "content-type": "application/octet-stream",
            "x-meshforge-blob-size": str(size),
            "x-meshforge-chunk-size": str(CS),
            "x-meshforge-chunk-sha256": chunk_hash or hashlib.sha256(body).hexdigest(),
        },
    )


async def _send(
    client: httpx.AsyncClient, data: bytes, digest: str, indices: range | list[int]
) -> None:
    for i in indices:
        r = await _put(client, digest, i, _span(data, i), size=len(data))
        assert r.status_code in (200, 201), r.text


def _count(data: bytes) -> int:
    return -(-len(data) // CS)


def _grant(runtime: ControllerRuntime, attempt: str, node_id: str) -> str:
    assert runtime.upload_grants is not None
    return runtime.upload_grants.mint(
        attempt, ttl_s=60, max_blobs=8, max_bytes=8 * MIB, node_id=node_id
    ).get_secret_value()


def _http(runtime: ControllerRuntime, token: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}",
        headers=bearer(token),
        timeout=30,
    )


async def test_agent_and_user_stagings_are_disjoint_in_both_directions(
    runtime: ControllerRuntime, tokens: dict[str, str]
) -> None:
    data, digest = _blob()
    grant = _grant(runtime, "att_a1", HARNESS_NODE_ID)
    other = _grant(runtime, "att_o1", OTHER_NODE)
    async with (
        api_client(runtime, token=grant) as node,
        api_client(runtime, token=other, node_id=OTHER_NODE) as other_node,
        _http(runtime, tokens["alice"]) as alice,
    ):
        await _send(node, data, digest, [0, 1])
        # Alice, another node: nothing in progress, cannot complete, cannot see.
        for foreign in (alice, other_node):
            assert (await foreign.get(f"/v1/blobs/{digest}/parts")).status_code == 404
            assert (
                await foreign.post(f"/v1/blobs/{digest}/complete")
            ).status_code == 404
        # The node's staging is untouched by their probes.
        got = (await node.get(f"/v1/blobs/{digest}/parts")).json()
        assert got["received"] == [0, 1]
        # Alice stages her own; the node does not see her parts.
        await _send(alice, data, digest, [2])
        assert (await alice.get(f"/v1/blobs/{digest}/parts")).json()["received"] == [2]
        assert (await node.get(f"/v1/blobs/{digest}/parts")).json()["received"] == [
            0,
            1,
        ]
        # Each completes only what it sent: both are 409 (parts missing).
        assert (await alice.post(f"/v1/blobs/{digest}/complete")).status_code == 409
        assert (await node.post(f"/v1/blobs/{digest}/complete")).status_code == 409
        await _send(node, data, digest, [2])
        done = await node.post(f"/v1/blobs/{digest}/complete")
        assert done.status_code == 201, done.text
    assert runtime.blobs is not None
    assert runtime.blobs.size(Digest(digest)) == len(data)


async def test_an_honest_retry_by_a_new_attempt_of_the_same_node_resumes(
    runtime: ControllerRuntime,
) -> None:
    """Attempt 1 dies after 2 parts; attempt 2 on the same node resumes them."""
    data, digest = _blob(4)
    first = _grant(runtime, "att_r1", HARNESS_NODE_ID)
    async with api_client(runtime, token=first) as a:
        await _send(a, data, digest, [0, 1])
    assert runtime.upload_grants is not None
    runtime.upload_grants.revoke("att_r1")
    second = _grant(runtime, "att_r2", HARNESS_NODE_ID)
    async with api_client(runtime, token=second) as b:
        assert (await b.get(f"/v1/blobs/{digest}/parts")).json()["received"] == [0, 1]
        await _send(b, data, digest, range(2, _count(data)))
        assert (await b.post(f"/v1/blobs/{digest}/complete")).status_code == 201
    assert runtime.blobs is not None and runtime.blobs.cas.resumes_in_progress() == 0


async def test_a_squatter_with_a_wrong_size_cannot_block_the_real_uploader(
    runtime: ControllerRuntime, tokens: dict[str, str]
) -> None:
    data, digest = _blob(3)
    async with (
        _http(runtime, tokens["bob"]) as bob,
        _http(runtime, tokens["alice"]) as alice,
    ):
        # Bob declares a different size (and junk parts) first.
        junk = os.urandom(CS)
        for i in (0, 1):
            r = await _put(bob, digest, i, junk, size=2 * CS)
            assert r.status_code in (200, 201), r.text
        await _send(alice, data, digest, range(_count(data)))
        assert (await alice.post(f"/v1/blobs/{digest}/complete")).status_code == 201
        # Bob's junk fails his own integrity only if the blob were not stored;
        # now it is: his complete is the idempotent 200 and drops his staging.
        assert (await bob.post(f"/v1/blobs/{digest}/complete")).status_code == 200
    assert runtime.blobs is not None and runtime.blobs.cas.resumes_in_progress() == 0


async def test_one_uploaders_integrity_failure_leaves_the_others_staging_intact(
    runtime: ControllerRuntime, tokens: dict[str, str]
) -> None:
    data, digest = _blob(3)
    async with (
        _http(runtime, tokens["bob"]) as bob,
        _http(runtime, tokens["alice"]) as alice,
    ):
        await _send(alice, data, digest, [0, 1])
        for i in range(_count(data)):  # Bob: honest chunk hashes of garbage
            r = await _put(
                bob, digest, i, os.urandom(len(_span(data, i))), size=len(data)
            )
            assert r.status_code in (200, 201), r.text
        bad = await bob.post(f"/v1/blobs/{digest}/complete")
        assert bad.status_code == 400 and "integrity" in bad.text.lower(), bad.text
        # Bob's upload "starts over" - his only; Alice's two parts are still there.
        assert (await bob.get(f"/v1/blobs/{digest}/parts")).status_code == 404
        assert (await alice.get(f"/v1/blobs/{digest}/parts")).json()["received"] == [
            0,
            1,
        ]
        await _send(alice, data, digest, [2])
        assert (await alice.post(f"/v1/blobs/{digest}/complete")).status_code == 201
    assert runtime.blobs is not None and runtime.blobs.size(Digest(digest)) == len(data)


@pytest.mark.parametrize("round_", range(4))
async def test_two_owners_racing_every_part_and_complete_on_one_digest(
    runtime: ControllerRuntime, tokens: dict[str, str], round_: int
) -> None:
    data, digest = _blob(5, tail=round_ + 1)
    async with (
        _http(runtime, tokens["alice"]) as alice,
        _http(runtime, tokens["bob"]) as bob,
        _http(runtime, tokens["carol"]) as carol,
    ):
        # three owners push the parts in parallel, then complete in parallel
        async def push(c: httpx.AsyncClient) -> None:
            await asyncio.gather(
                *[
                    _put(c, digest, i, _span(data, i), size=len(data))
                    for i in range(_count(data))
                ]
            )

        await asyncio.gather(push(alice), push(bob), push(carol))
        done = await asyncio.gather(
            *[c.post(f"/v1/blobs/{digest}/complete") for c in (alice, bob, carol)]
        )
    codes = sorted(r.status_code for r in done)
    assert codes == [200, 200, 201], [r.text for r in done]
    assert runtime.blobs is not None
    assert runtime.blobs.size(Digest(digest)) == len(data)
    assert runtime.blobs.cas.resumes_in_progress() == 0  # nobody's staging left over


async def test_complete_of_a_stored_blob_creates_and_leaves_nothing(
    runtime: ControllerRuntime, tokens: dict[str, str]
) -> None:
    data, digest = _blob(2)
    async with (
        _http(runtime, tokens["alice"]) as alice,
        _http(runtime, tokens["carol"]) as carol,
    ):
        await _send(alice, data, digest, range(_count(data)))
        assert (await alice.post(f"/v1/blobs/{digest}/complete")).status_code == 201
        assert runtime.blobs is not None
        before = runtime.blobs.cas.resumes_in_progress()
        assert (await carol.post(f"/v1/blobs/{digest}/complete")).status_code == 200
        # A part for an already stored digest is "already here", no new staging.
        r = await _put(carol, digest, 0, _span(data, 0), size=len(data))
        assert r.status_code == 200, r.text
        assert runtime.blobs.cas.resumes_in_progress() == before == 0


async def test_honest_retry_is_idempotent_and_a_lying_chunk_is_refused(
    runtime: ControllerRuntime, tokens: dict[str, str]
) -> None:
    data, digest = _blob(3)
    async with (
        _http(runtime, tokens["alice"]) as alice,
        _http(runtime, tokens["bob"]) as bob,
    ):
        assert (
            await _put(alice, digest, 0, _span(data, 0), size=len(data))
        ).status_code == 201
        assert (
            await _put(alice, digest, 0, _span(data, 0), size=len(data))
        ).status_code == 200
        # Bob plants a lying chunk in HIS staging (declared hash != body).
        lie = await _put(
            bob, digest, 1, os.urandom(CS), size=len(data), chunk_hash="0" * 64
        )
        assert lie.status_code >= 400
        await _send(alice, data, digest, [1, 2])
        assert (await alice.post(f"/v1/blobs/{digest}/complete")).status_code == 201
        assert (await alice.post(f"/v1/blobs/{digest}/complete")).status_code == 200


async def test_part_routes_check_credentials_before_ownership(
    runtime: ControllerRuntime, tokens: dict[str, str], engine: AsyncEngine
) -> None:
    data, digest = _blob(2)
    alice_id = ""
    async with _http(runtime, tokens["alice"]) as alice:
        await _send(alice, data, digest, [0])
        alice_id = str((await alice.get("/v1/auth/whoami")).json()["token_id"])
    async with engine.begin() as db:
        await db.execute(
            text("UPDATE api_token SET revoked_at = now() WHERE id = :i"),
            {"i": alice_id},
        )
    async with (
        _http(runtime, tokens["alice"]) as dead,
        _http(runtime, "mfat_" + "A" * 43) as junk,
    ):
        for c in (dead, junk):
            assert (await c.get(f"/v1/blobs/{digest}/parts")).status_code in (401, 403)
            assert (await c.post(f"/v1/blobs/{digest}/complete")).status_code in (
                401,
                403,
            )
            r = await _put(c, digest, 1, _span(data, 1), size=len(data))
            assert r.status_code in (401, 403)
    async with httpx.AsyncClient(
        base_url=f"https://127.0.0.1:{runtime.http_port}", timeout=30
    ) as anon:
        assert (await anon.get(f"/v1/blobs/{digest}/parts")).status_code in (401, 403)
        assert (await anon.post(f"/v1/blobs/{digest}/complete")).status_code in (
            401,
            403,
        )


async def test_owner_keys_do_not_collide_across_many_owners_and_digests(
    runtime: ControllerRuntime, tokens: dict[str, str]
) -> None:
    """Three owners x 8 digests: every (owner, digest) is its own staging."""
    async with (
        _http(runtime, tokens["alice"]) as alice,
        _http(runtime, tokens["bob"]) as bob,
        api_client(runtime, token=_grant(runtime, "att_k", HARNESS_NODE_ID)) as node,
    ):
        blobs = [_blob(2, tail=i + 1) for i in range(8)]
        for who, c in enumerate((alice, bob, node)):
            for data, digest in blobs:
                await _send(c, data, digest, [who % 2])  # part 0 or 1
        assert runtime.blobs is not None
        assert runtime.blobs.cas.resumes_in_progress() == 24
        for _data, digest in blobs:
            got = [
                (await c.get(f"/v1/blobs/{digest}/parts")).json()["received"]
                for c in (alice, bob, node)
            ]
            assert got == [[0], [1], [0]]


@pytest.mark.security
async def test_one_user_cannot_starve_the_resumable_cap_for_everyone_else(
    runtime: ControllerRuntime, tokens: dict[str, str]
) -> None:
    """D158 T-4 (was a strict xfail): one uploader stops at its per-owner
    sub-cap (``resource_busy``, 503) and another user still starts."""
    sub_cap = DEFAULT_MAX_RESUMABLE_PER_OWNER
    async with (
        _http(runtime, tokens["bob"]) as bob,
        _http(runtime, tokens["alice"]) as alice,
    ):
        refused = 0
        for i in range(DEFAULT_MAX_RESUMABLE):
            body = os.urandom(CS)
            r = await _put(bob, os.urandom(32).hex(), 0, body, size=2 * CS)
            if i < sub_cap:
                assert r.status_code in (200, 201), r.text
            else:
                assert r.status_code == 503, (r.status_code, r.text)
                assert "resource_busy" in r.text
                refused += 1
        assert refused == DEFAULT_MAX_RESUMABLE - sub_cap
        data, digest = _blob(2)
        r = await _put(alice, digest, 0, _span(data, 0), size=len(data))
        assert r.status_code in (200, 201), (r.status_code, r.text)
