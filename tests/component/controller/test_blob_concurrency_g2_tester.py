"""Tester round 1 (V0.4 G2): concurrent PUT of the same digest (D48).

Not covered by the implementer's suite: many callers racing to upload the exact
same bytes under the same digest at once. `controller/blobs.py`'s own module
docstring documents that this is *content-idempotent, not winner-take-all*:
"the rename is atomic, which makes concurrent uploads of one digest safe (both
write identical bytes; the second rename replaces the first with the same
content)" -- so more than one racer legitimately reporting ``created=True`` is
the documented, intended behaviour (each one raced past the "does it already
exist" check before any of them had committed), not a bug. What the contract
does promise, and what this test actually checks: every response reports the
correct digest/size, the store ends up with exactly one file for that digest
containing the exact bytes, and no partial (``*.part``) files are left behind.
"""

from __future__ import annotations

import asyncio
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
    settings = g2_settings(pg_settings, blobs={"max_blob_bytes": 8 * MIB})  # type: ignore[arg-type]
    async with running_controller(settings) as rt:
        yield rt


def _blob_root(runtime: ControllerRuntime) -> Path:
    assert runtime.blobs is not None
    return runtime.blobs.root


async def test_concurrent_put_of_the_same_digest_leaves_exactly_one_clean_file(
    runtime: ControllerRuntime,
) -> None:
    data = os.urandom(300_000)
    digest = sha256_bytes(data)
    async with api_client(runtime, token=token_of(runtime)) as client:
        responses = await asyncio.gather(
            *[client.put(f"/v1/blobs/{digest.hex}", content=data) for _ in range(8)]
        )
        for r in responses:
            assert r.status_code in (200, 201), r.text
            assert r.json()["digest"] == str(digest)
            assert r.json()["size"] == len(data)
            # status code must agree with the reported `created` flag
            assert r.status_code == (201 if r.json()["created"] else 200)
        # at least one racer had to actually write the bytes; content-idempotent
        # rename means more than one MAY report created=True (see module note
        # above) -- that is not asserted either way.
        created = [r for r in responses if r.json()["created"] is True]
        assert created, [r.json() for r in responses]
        got = await client.get(f"/v1/blobs/{digest.hex}")
        assert got.status_code == 200
        assert got.content == data
    root = _blob_root(runtime)
    stored = sorted(p for p in (root / "sha256").rglob("*") if p.is_file())
    assert len(stored) == 1
    assert stored[0].read_bytes() == data
    assert list((root / "tmp").iterdir()) == []


async def test_high_fanout_concurrent_put_never_leaves_partial_files(
    runtime: ControllerRuntime,
) -> None:
    """32-way fan-out (heavier than the 8-way above) is still safe: whatever
    interleaving of "check exists" / "write" / "rename" the event loop picks,
    the tmp dir ends up empty and exactly one correct file exists."""
    data = os.urandom(50_000)
    digest = sha256_bytes(data)
    async with api_client(runtime, token=token_of(runtime)) as client:
        responses = await asyncio.gather(
            *[client.put(f"/v1/blobs/{digest.hex}", content=data) for _ in range(32)]
        )
        assert all(r.status_code in (200, 201) for r in responses), [
            r.status_code for r in responses
        ]
        assert all(r.json()["size"] == len(data) for r in responses)
    root = _blob_root(runtime)
    stored = sorted(p for p in (root / "sha256").rglob("*") if p.is_file())
    assert len(stored) == 1
    assert stored[0].read_bytes() == data
    assert list((root / "tmp").iterdir()) == []
