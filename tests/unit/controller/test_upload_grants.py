"""V0.4 G5 D73: upload grants (quota, scope, lifetime) and blob-store admission.

Every bound is tested at its maximum (accepted) and one past it (refused).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.controller.blobs import BlobError, BlobStore
from meshforge.controller.upload_grants import GrantRefusal, UploadGrants

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _digest(i: int) -> Digest:
    return sha256_bytes(bytes([i]) * 3)


def _grants(max_blobs: int = 3, max_bytes: int = 100) -> tuple[UploadGrants, str]:
    grants = UploadGrants(clock=Clock())
    token = grants.mint("att_1", ttl_s=60, max_blobs=max_blobs, max_bytes=max_bytes)
    return grants, token.get_secret_value()


def _refusal(grants: UploadGrants, token: str, digest: Digest, size: int | None) -> str:
    with pytest.raises(GrantRefusal) as info:
        grants.begin(token, digest, size)
    return info.value.reason


def test_byte_quota_max_accepted_and_max_plus_one_refused() -> None:
    grants, token = _grants(max_bytes=100)
    ticket = grants.begin(token, _digest(1), 60)
    grants.finish(ticket, stored=True, size=60)
    assert _refusal(grants, token, _digest(2), 41) == "byte_quota"  # 101
    ok = grants.begin(token, _digest(2), 40)  # exactly 100
    grants.finish(ok, stored=True, size=40)
    assert grants.uploaded("att_1", _digest(1)) and grants.uploaded("att_1", _digest(2))


def test_blob_quota_max_accepted_and_max_plus_one_refused() -> None:
    grants, token = _grants(max_blobs=3, max_bytes=10**6)
    for i in range(3):
        grants.finish(grants.begin(token, _digest(i), 1), stored=True, size=1)
    assert _refusal(grants, token, _digest(9), 1) == "blob_quota"


def test_reservations_count_before_the_upload_finishes() -> None:
    """Two concurrent uploads cannot overshoot the byte quota together."""
    grants, token = _grants(max_bytes=100)
    first = grants.begin(token, _digest(1), 70)
    assert _refusal(grants, token, _digest(2), 31) == "byte_quota"
    assert _refusal(grants, token, _digest(1), 1) == "blob_quota"  # same, in flight
    grants.finish(first, stored=False)  # failed upload: reservation returned
    assert not grants.uploaded("att_1", _digest(1))
    grants.finish(grants.begin(token, _digest(2), 100), stored=True, size=100)


def test_re_upload_of_a_stored_digest_is_free() -> None:
    grants, token = _grants(max_blobs=1, max_bytes=10)
    grants.finish(grants.begin(token, _digest(1), 10), stored=True, size=10)
    again = grants.begin(token, _digest(1), 10)  # a retry: no quota burnt
    assert again.free
    grants.finish(again, stored=True, size=10)


def test_length_is_required_and_closed_or_expired_or_unknown_refuse() -> None:
    clock = Clock()
    grants = UploadGrants(clock=clock)
    token = grants.mint("att_1", ttl_s=10, max_blobs=3, max_bytes=100)
    secret = token.get_secret_value()
    assert _refusal(grants, secret, _digest(1), None) == "length_required"
    assert _refusal(grants, secret + "x", _digest(1), 1) == "unknown"
    grants.finish(grants.begin(secret, _digest(1), 1), stored=True, size=1)
    grants.close("att_1", grace_s=5)
    assert _refusal(grants, secret, _digest(2), 1) == "closed"
    assert grants.uploaded("att_1", _digest(1))  # still answerable in the grace
    clock.now += 6
    assert _refusal(grants, secret, _digest(2), 1) == "expired"
    assert not grants.uploaded("att_1", _digest(1))
    other = grants.mint("att_2", ttl_s=10, max_blobs=1, max_bytes=1)
    clock.now += 11
    assert _refusal(grants, other.get_secret_value(), _digest(3), 1) == "expired"


def test_a_re_minted_grant_keeps_what_was_stored_and_the_old_token_dies() -> None:
    grants, token = _grants(max_blobs=2, max_bytes=100)
    grants.finish(grants.begin(token, _digest(1), 50), stored=True, size=50)
    new = grants.mint("att_1", ttl_s=60, max_blobs=2, max_bytes=100)
    assert not grants.owns(token)
    assert grants.uploaded("att_1", _digest(1))
    assert _refusal(grants, new.get_secret_value(), _digest(2), 51) == "byte_quota"


def test_uploaded_is_per_attempt() -> None:
    grants, token = _grants()
    grants.mint("att_2", ttl_s=60, max_blobs=3, max_bytes=100)
    grants.finish(grants.begin(token, _digest(1), 1), stored=True, size=1)
    assert grants.uploaded("att_1", str(_digest(1)))
    assert not grants.uploaded("att_2", _digest(1))
    assert not grants.uploaded("att_3", _digest(1))


# ------------------------------------------------------------- blob store


async def _body(data: bytes) -> AsyncIterator[bytes]:
    yield data


@pytest.mark.asyncio
async def test_free_disk_floor_refuses_at_floor_plus_one(tmp_path: Path) -> None:
    free = {"bytes": 1000}
    store = BlobStore(
        tmp_path,
        max_blob_bytes=10**6,
        free_disk_floor_bytes=900,
        disk_free=lambda _p: free["bytes"],
    )
    store.prepare()
    ok = b"x" * 100  # leaves exactly the floor
    await store.put(sha256_bytes(ok), _body(ok), declared_size=len(ok))
    too_much = b"y" * 101
    with pytest.raises(BlobError) as info:
        await store.put(
            sha256_bytes(too_much), _body(too_much), declared_size=len(too_much)
        )
    assert info.value.failure_type is FailureType.DISK_FULL
    assert info.value.details["reason"] == "insufficient_storage"
    assert store.uploads_refused_disk == 1
    assert list((tmp_path / "tmp").iterdir()) == []  # nothing left behind


@pytest.mark.asyncio
async def test_upload_slots_are_bounded(tmp_path: Path) -> None:
    store = BlobStore(
        tmp_path, max_blob_bytes=10**6, max_concurrent_uploads=1, upload_wait_s=0.02
    )
    store.prepare()
    release = asyncio.Event()
    data = b"slow"

    async def slow() -> AsyncIterator[bytes]:
        await release.wait()
        yield data

    first = asyncio.create_task(store.put(sha256_bytes(data), slow()))
    await asyncio.sleep(0)
    other = b"other"
    with pytest.raises(BlobError) as info:
        await store.put(sha256_bytes(other), _body(other))
    assert info.value.failure_type is FailureType.RESOURCE_BUSY
    assert store.uploads_refused_busy == 1
    release.set()
    await first
    await store.put(sha256_bytes(other), _body(other))  # the slot is free again
