"""Blob store (verify-then-rename, caps, no partial files) and fetch tokens (D48).

Real files in ``tmp_path``; the upload stream is an async generator so failures
(wrong digest, oversize, a stream that raises, cancellation) happen for real.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Iterable
from pathlib import Path

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.hashing import Digest, sha256_bytes
from meshforge.controller.blobs import (
    MAX_FETCH_TOKEN_DIGESTS,
    MAX_FETCH_TOKEN_TTL_S,
    BlobError,
    BlobIntegrityError,
    BlobStore,
    BlobTooLargeError,
    FetchTokens,
)

MIB = 1024 * 1024


async def _chunks(parts: Iterable[bytes]) -> AsyncIterator[bytes]:
    for part in parts:
        yield part


def _store(tmp_path: Path, cap: int = MIB) -> BlobStore:
    store = BlobStore(tmp_path / "blobs", max_blob_bytes=cap)
    store.prepare()
    return store


def _leftovers(store: BlobStore) -> list[Path]:
    return sorted((store.root / "tmp").iterdir())


def _stored(store: BlobStore) -> list[Path]:
    return sorted(p for p in (store.root / "sha256").rglob("*") if p.is_file())


# ------------------------------------------------------------------- store


@pytest.mark.asyncio
async def test_put_stores_under_the_adr_0003_layout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = b"hello blob"
    digest = sha256_bytes(data)
    result = await store.put(digest, _chunks([data[:3], data[3:]]), declared_size=10)
    assert result.created and result.size == 10
    path = store.root / "sha256" / digest.hex[:2] / digest.hex
    assert path.read_bytes() == data
    assert store.size(digest) == 10
    assert _leftovers(store) == []


@pytest.mark.asyncio
async def test_second_put_is_idempotent_and_writes_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = sha256_bytes(b"x")
    await store.put(digest, _chunks([b"x"]))
    mtime = store.path_for(digest).stat().st_mtime_ns
    again = await store.put(digest, _chunks([b"x"]))
    assert not again.created
    assert store.path_for(digest).stat().st_mtime_ns == mtime
    assert _leftovers(store) == []


@pytest.mark.asyncio
async def test_wrong_digest_keeps_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    claimed = sha256_bytes(b"what I said")
    with pytest.raises(BlobIntegrityError) as info:
        await store.put(claimed, _chunks([b"what I sent"]))
    assert info.value.failure_type is FailureType.INTEGRITY_ERROR
    assert _stored(store) == [] and _leftovers(store) == []


@pytest.mark.asyncio
async def test_other_bytes_under_an_existing_name_are_refused(tmp_path: Path) -> None:
    store = _store(tmp_path)
    digest = sha256_bytes(b"real")
    await store.put(digest, _chunks([b"real"]))
    with pytest.raises(BlobIntegrityError):
        await store.put(digest, _chunks([b"fake"]))
    assert store.path_for(digest).read_bytes() == b"real"


@pytest.mark.asyncio
async def test_size_cap_max_ok_and_max_plus_one_refused(tmp_path: Path) -> None:
    store = _store(tmp_path, cap=MIB)
    exact = b"a" * MIB
    await store.put(
        sha256_bytes(exact), _chunks([exact[: MIB // 2], exact[MIB // 2 :]])
    )
    over = b"b" * (MIB + 1)
    with pytest.raises(BlobTooLargeError):
        await store.put(sha256_bytes(over), _chunks([over]))  # streamed, no length
    with pytest.raises(BlobTooLargeError):
        await store.put(sha256_bytes(over), _chunks([]), declared_size=MIB + 1)
    assert _leftovers(store) == []
    assert len(_stored(store)) == 1


@pytest.mark.asyncio
async def test_body_shorter_than_declared_is_a_transfer_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    data = b"short"
    with pytest.raises(BlobError) as info:
        await store.put(sha256_bytes(data), _chunks([data]), declared_size=99)
    assert info.value.failure_type is FailureType.TRANSFER_ERROR
    assert _stored(store) == [] and _leftovers(store) == []


@pytest.mark.asyncio
async def test_a_stream_that_breaks_leaves_no_part_file(tmp_path: Path) -> None:
    store = _store(tmp_path)

    async def broken() -> AsyncIterator[bytes]:
        yield b"a" * 1000
        raise ConnectionResetError("peer went away")

    with pytest.raises(ConnectionResetError):
        await store.put(sha256_bytes(b"whatever"), broken())
    assert _leftovers(store) == [] and _stored(store) == []


@pytest.mark.asyncio
async def test_cancellation_mid_upload_leaves_no_part_file(tmp_path: Path) -> None:
    store = _store(tmp_path)
    started = asyncio.Event()

    async def slow() -> AsyncIterator[bytes]:
        yield b"a" * 1000
        started.set()
        await asyncio.Event().wait()  # never finishes
        yield b""

    task = asyncio.create_task(store.put(sha256_bytes(b"x"), slow()))
    await started.wait()
    assert len(_leftovers(store)) == 1  # it really was writing a part file
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _leftovers(store) == [] and _stored(store) == []


def test_prepare_sweeps_part_files_from_a_crash(tmp_path: Path) -> None:
    store = _store(tmp_path)
    (store.root / "tmp" / "abc.part").write_bytes(b"half")
    (store.root / "tmp" / "keep.txt").write_bytes(b"not ours")
    assert store.prepare() == 1
    assert [p.name for p in _leftovers(store)] == ["keep.txt"]


def test_size_of_a_missing_blob_is_none(tmp_path: Path) -> None:
    assert _store(tmp_path).size(Digest(hashlib.sha256(b"nope").hexdigest())) is None


# ------------------------------------------------------------ fetch tokens


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


D1, D2, D3 = (sha256_bytes(bytes([i])) for i in range(3))


def test_token_reads_only_its_digests() -> None:
    tokens = FetchTokens(clock=Clock())
    token = tokens.mint("att_1", [D1, D2], ttl_s=60).get_secret_value()
    assert tokens.check(token, D1)
    assert tokens.check(token, D2)
    assert not tokens.check(token, D3)
    assert not tokens.check(token + "x", D1)
    assert not tokens.check(None, D1)
    assert not tokens.check("", D1)


def test_fetch_tokens_are_download_only() -> None:
    """D73: there is no upload flag any more - uploads need an upload grant."""
    tokens = FetchTokens(clock=Clock())
    with pytest.raises(TypeError):
        tokens.mint("att_1", [D1], ttl_s=60, allow_upload=True)  # type: ignore[call-arg]
    token = tokens.mint("att_1", [D1], ttl_s=60)
    assert not tokens.check(token.get_secret_value(), D3)


def test_expiry_and_expire_after_and_revoke() -> None:
    clock = Clock()
    tokens = FetchTokens(clock=clock)
    a = tokens.mint("att_a", [D1], ttl_s=60).get_secret_value()
    b = tokens.mint("att_b", [D1], ttl_s=60).get_secret_value()
    clock.now += 59.9
    assert tokens.check(a, D1)
    clock.now += 0.1
    assert not tokens.check(a, D1)  # expired exactly at the edge
    c = tokens.mint("att_c", [D1], ttl_s=600).get_secret_value()
    assert tokens.expire_after("att_c", 5) == 1
    clock.now += 4.9
    assert tokens.check(c, D1)
    clock.now += 0.2
    assert not tokens.check(c, D1)
    d = tokens.mint("att_d", [D1], ttl_s=600).get_secret_value()
    assert tokens.revoke("att_d") == 1
    assert not tokens.check(d, D1)
    assert not tokens.check(b, D1)


def test_expire_after_never_extends() -> None:
    clock = Clock()
    tokens = FetchTokens(clock=clock)
    t = tokens.mint("att", [D1], ttl_s=10).get_secret_value()
    assert tokens.expire_after("att", 1000) == 0
    clock.now += 10
    assert not tokens.check(t, D1)


def test_capacity_evicts_the_oldest() -> None:
    tokens = FetchTokens(clock=Clock(), capacity=2)
    first = tokens.mint("a1", [D1], ttl_s=60).get_secret_value()
    second = tokens.mint("a2", [D1], ttl_s=60).get_secret_value()
    third = tokens.mint("a3", [D1], ttl_s=60).get_secret_value()
    assert len(tokens) == 2 and tokens.evicted == 1
    assert not tokens.check(first, D1)
    assert tokens.check(second, D1)
    assert tokens.check(third, D1)


def test_ttl_and_digest_bounds() -> None:
    tokens = FetchTokens(clock=Clock())
    tokens.mint("a", [D1], ttl_s=MAX_FETCH_TOKEN_TTL_S)
    for bad in (0, -1, MAX_FETCH_TOKEN_TTL_S + 1):
        with pytest.raises(ValueError):
            tokens.mint("a", [D1], ttl_s=bad)
    many = [sha256_bytes(i.to_bytes(4, "big")) for i in range(MAX_FETCH_TOKEN_DIGESTS)]
    tokens.mint("a", many, ttl_s=1)
    with pytest.raises(ValueError):
        tokens.mint("a", [*many, D1 if D1 not in many else D2], ttl_s=1)


def test_the_table_never_holds_a_usable_token() -> None:
    tokens = FetchTokens(clock=Clock())
    token = tokens.mint("a", [D1], ttl_s=60).get_secret_value()
    assert token not in repr(tokens._grants)  # keyed by SHA-256 of the token
