"""Unit tests for ``lab/spikes/_s3/payload.py``: the deterministic payload, its
constant-memory chunking and the CRC-32 verification both sides use."""

from __future__ import annotations

import random
import zlib

import pytest

from spikes._s3 import payload


def test_block_is_deterministic_and_incompressible() -> None:
    block = bytes(payload.chunk_view(0, payload.BLOCK_BYTES))
    assert block == random.Random(payload.SEED).randbytes(payload.BLOCK_BYTES)  # noqa: S311
    # a compressing transport could not cheat: < 1 % gain
    assert len(zlib.compress(block[: 256 * 1024], 1)) > 0.99 * 256 * 1024


def test_chunk_view_wraps_at_block_boundary_without_copy() -> None:
    a = payload.chunk_view(payload.BLOCK_BYTES - 10, 20)
    assert len(a) == 20
    expected = bytes(payload.chunk_view(payload.BLOCK_BYTES - 10, 10)) + bytes(
        payload.chunk_view(0, 10)
    )
    assert bytes(a) == expected
    # any absolute offset maps to offset % BLOCK_BYTES
    assert bytes(payload.chunk_view(3 * payload.BLOCK_BYTES + 7, 5)) == bytes(
        payload.chunk_view(7, 5)
    )
    assert isinstance(a, memoryview)


@pytest.mark.parametrize("length", [-1, payload.BLOCK_BYTES + 1])
def test_chunk_view_rejects_bad_length(length: int) -> None:
    with pytest.raises(ValueError):
        payload.chunk_view(0, length)


def test_iter_chunks_covers_exactly_the_range() -> None:
    views = list(payload.iter_chunks(1000, 10_000, 4096))
    assert [len(v) for v in views] == [4096, 4096, 1808]
    joined = b"".join(bytes(v) for v in views)
    assert joined == bytes(payload.chunk_view(1000, 4096)) + bytes(
        payload.chunk_view(1000 + 4096, 4096)
    ) + bytes(payload.chunk_view(1000 + 8192, 1808))
    assert list(payload.iter_chunks(0, 0, 4096)) == []
    with pytest.raises(ValueError):
        list(payload.iter_chunks(0, 10, 0))


def test_iter_chunks_uses_constant_memory() -> None:
    # every view is a slice of the same 8 MiB buffer: no per-chunk allocation
    base = payload._double_block().obj
    for view in payload.iter_chunks(0, 16 * payload.BLOCK_BYTES, payload.BLOCK_BYTES):
        assert view.obj is base


def test_crc_of_matches_streamed_crc_and_is_offset_aware() -> None:
    crc = 0
    for view in payload.iter_chunks(5000, 3_000_000, 65_536):
        crc = zlib.crc32(view, crc)
    assert payload.crc_of(5000, 3_000_000) == crc
    assert payload.crc_of(0, 3_000_000) != payload.crc_of(1, 3_000_000)
    # a range larger than the block (spans several repetitions of it)
    assert payload.crc_of(0, 3 * payload.BLOCK_BYTES + 17) == zlib.crc32(
        b"".join(
            bytes(v)
            for v in payload.iter_chunks(0, 3 * payload.BLOCK_BYTES + 17, 1 << 20)
        )
    )


def test_verifier_accepts_exact_stream_in_any_pieces() -> None:
    v = payload.Verifier(offset=123)
    for view in payload.iter_chunks(123, 100_000, 777):
        assert v.update(view) == len(view)
    assert v.received == 100_000
    assert v.matches(100_000)
    assert not v.matches(100_001)


def test_verifier_detects_corruption_reorder_and_short() -> None:
    good = bytes(payload.chunk_view(0, 50_000))
    corrupted = bytearray(good)
    corrupted[25_000] ^= 0x01
    v = payload.Verifier()
    v.update(corrupted)
    assert v.received == 50_000 and not v.matches(50_000)

    reordered = good[25_000:] + good[:25_000]
    v = payload.Verifier()
    v.update(reordered)
    assert not v.matches(50_000)

    short = payload.Verifier()
    short.update(good[:-1])
    assert not short.matches(50_000)


def test_crc_rate_is_positive_and_bounded_in_time() -> None:
    rate = payload.crc_rate_mib_s(seconds=0.05)
    assert rate > 0
