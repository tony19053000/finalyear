"""V0.6 G1 tester round (D114): adversarial HTTP tests of grant-scoped part uploads.

Derived from the criteria (quota holds, a credited blob was really sent under
the grant, a restart-resumed upload stays within the same quota), not from the
implementation. Real uvicorn + PostgreSQL + blob store.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from mf_jobs_harness import api_client, g2_settings, running_controller, token_of
from test_v06_g1_parts_grant import _complete, _grant, _part, _parts

from meshforge.common.cas import MIN_CHUNK_SIZE, chunk_count
from meshforge.common.hashing import sha256_bytes
from meshforge.controller.server import ControllerRuntime

pytestmark = pytest.mark.asyncio

CS = MIN_CHUNK_SIZE
MIB = 1024 * 1024


@pytest_asyncio.fixture
async def runtime(pg_settings: object) -> AsyncIterator[ControllerRuntime]:
    settings = g2_settings(pg_settings, blobs={"max_blob_bytes": 4 * MIB})  # type: ignore[arg-type]
    async with running_controller(settings) as rt:
        yield rt


async def test_concurrent_completes_of_one_grant_credit_once_and_leak_no_reservation(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(3 * CS + 11)
    digest = sha256_bytes(data)
    token = _grant(runtime, "att_conc", max_blobs=1, max_bytes=len(data))
    async with api_client(runtime, token=token) as agent:
        await _parts(agent, data, range(chunk_count(len(data), CS)))
        results = await asyncio.gather(*[_complete(agent, data) for _ in range(12)])
    assert all(r.status_code in (200, 201) for r in results), [r.text for r in results]
    assert sum(r.status_code == 201 for r in results) == 1
    # a loser of the race may answer credited:false (it only released); the
    # winner is credited and every later complete is credited and free.
    assert any(r.json()["credited"] is True for r in results)
    grants = runtime.upload_grants
    assert grants is not None and grants.uploaded("att_conc", digest)
    async with api_client(runtime, token=token) as agent:
        assert (await _complete(agent, data)).json()["credited"] is True
    grant = next(g for g in grants._grants.values() if g.attempt_id == "att_conc")
    assert grant.reserved_bytes == 0 and not grant.pending
    assert grant.used_bytes == len(data)


async def test_two_attempts_racing_to_complete_one_blob_credit_exactly_one(
    runtime: ControllerRuntime,
) -> None:
    """Same digest, two attempts: only the call that assembled the blob is
    credited; the other must prove itself with a PUT (credited false)."""
    data = os.urandom(2 * CS + 3)
    digest = sha256_bytes(data)
    first = _grant(runtime, "att_r1")
    second = _grant(runtime, "att_r2")
    async with (
        api_client(runtime, token=first) as a,
        api_client(runtime, token=second) as b,
    ):
        await _parts(a, data, range(chunk_count(len(data), CS)))
        got = await asyncio.gather(_complete(a, data), _complete(b, data))
        credited = [r.json()["credited"] for r in got]
        assert sorted(credited) == [False, True], [r.text for r in got]
        loser = b if got[0].json()["credited"] else a
        proved = await loser.put(f"/v1/blobs/{digest.hex}", content=data)
        assert proved.status_code in (200, 201), proved.text
    grants = runtime.upload_grants
    assert grants is not None
    assert grants.uploaded("att_r1", digest) and grants.uploaded("att_r2", digest)


async def test_credit_requires_the_bytes_not_just_the_name(
    runtime: ControllerRuntime,
) -> None:
    """A grant must not be credited for a digest whose parts never assembled:
    complete with parts missing is 409 and credits nothing; a bogus part
    (wrong bytes for the declared chunk digest) is refused."""
    data = os.urandom(3 * CS)
    digest = sha256_bytes(data)
    token = _grant(runtime, "att_name")
    async with api_client(runtime, token=token) as agent:
        await _parts(agent, data, range(2))
        assert (await _complete(agent, data)).status_code == 409
        grants = runtime.upload_grants
        assert grants is not None and not grants.uploaded("att_name", digest)
        # a lying part: bytes differ from the chunk digest it declares
        bad = await agent.put(
            f"/v1/blobs/{digest.hex}/parts/2",
            content=os.urandom(CS),
            headers={
                "x-meshforge-blob-size": str(len(data)),
                "x-meshforge-chunk-size": str(CS),
                "x-meshforge-chunk-sha256": "0" * 64,
            },
        )
        assert bad.status_code >= 400
        assert not grants.uploaded("att_name", digest)


async def test_assembling_junk_for_a_digest_fails_integrity_and_credits_nothing(
    runtime: ControllerRuntime,
) -> None:
    real = os.urandom(2 * CS)
    digest = sha256_bytes(real)
    junk = os.urandom(2 * CS)
    token = _grant(runtime, "att_junk")
    async with api_client(runtime, token=token) as agent:
        for i in range(2):
            r = await _part(agent, junk, i, digest=digest)  # chunk digests honest
            assert r.status_code in (200, 201), r.text
        done = await agent.post(f"/v1/blobs/{digest.hex}/complete")
        assert done.status_code >= 400
    grants = runtime.upload_grants
    assert grants is not None and not grants.uploaded("att_junk", digest)
    assert runtime.blobs is not None and runtime.blobs.size(digest) is None


async def test_junk_parts_cannot_block_the_real_uploader_of_the_same_digest(
    runtime: ControllerRuntime,
) -> None:
    """A malicious/buggy grant holder declares a wrong blob size for someone
    else's digest; the real upload (correct size) must still be able to finish."""
    real = os.urandom(3 * CS)
    digest = sha256_bytes(real)
    attacker = _grant(runtime, "att_evil")
    victim = _grant(runtime, "att_good")
    async with api_client(runtime, token=attacker) as evil:
        r = await _part(evil, os.urandom(CS), 0, size=CS, digest=digest)
        assert r.status_code in (200, 201, 400, 409), r.text
    async with api_client(runtime, token=victim) as good:
        sent = [await _part(good, real, i) for i in range(3)]
        assert all(s.status_code in (200, 201) for s in sent), [s.text for s in sent]
        done = await _complete(good, real)
        assert done.status_code in (200, 201), done.text
    grants = runtime.upload_grants
    assert grants is not None and grants.uploaded("att_good", digest)


async def test_quota_counts_the_stored_bytes_after_a_remint_mid_upload(
    runtime: ControllerRuntime,
) -> None:
    """Restart-resume must not give a fresh quota for what was stored: after a
    re-mint the attempt cannot exceed max_bytes in total."""
    a = os.urandom(2 * CS)
    b = os.urandom(2 * CS)
    old = _grant(runtime, "att_remint", max_blobs=4, max_bytes=3 * CS)
    async with api_client(runtime, token=old) as agent:
        await _parts(agent, a, range(2))
        assert (await _complete(agent, a)).json()["credited"] is True
    new = _grant(runtime, "att_remint", max_blobs=4, max_bytes=3 * CS)
    async with api_client(runtime, token=new) as agent:
        refused = await _part(agent, b, 0)  # 2*CS stored + 2*CS > 3*CS
        assert refused.status_code == 413, refused.text


async def test_fabric_token_complete_does_not_touch_grant_accounting(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(2 * CS)
    token = _grant(runtime, "att_fab")
    async with api_client(runtime, token=token) as agent:
        await _parts(agent, data, range(2))
    async with api_client(runtime, token=token_of(runtime)) as admin:
        assert (await _complete(admin, data)).status_code == 201
    grants = runtime.upload_grants
    assert grants is not None
    assert not grants.uploaded("att_fab", sha256_bytes(data))
