"""The deterministic payload every S3 transport moves, and its verification.

The payload is one fixed 4 MiB pseudo-random block repeated for as long as
needed, so the byte at absolute offset ``i`` is ``BLOCK[i % BLOCK_BYTES]``. Both
laptops can therefore generate and check *any* byte range without holding the
payload in RAM (2 GiB must never be materialised) and without sending a
checksum over the wire: the receiver computes a rolling CRC-32 and compares it
with the locally computed CRC of the same range. A transport that drops,
duplicates or reorders bytes fails loudly instead of looking fast.

The block comes from ``random.Random(SEED)``, which is reproducible across
CPython versions and operating systems - unlike ``os.urandom`` - and is *not*
compressible enough for any transport to cheat (gRPC and HTTP do not compress
here, but a future compressing transport would be visible in the report).
"""

from __future__ import annotations

import functools
import random
import time
import zlib
from collections.abc import Iterator
from typing import Final

#: Repeating block size; also the largest usable chunk (see protocol.MAX_CHUNK_BYTES).
BLOCK_BYTES: Final = 4 * 1024 * 1024
SEED: Final = 20260919


@functools.cache
def _double_block() -> memoryview:
    """``BLOCK + BLOCK`` (8 MiB), so any chunk <= BLOCK_BYTES is one slice."""
    block = random.Random(SEED).randbytes(BLOCK_BYTES)  # noqa: S311 - test payload, not crypto
    return memoryview(block + block)


def chunk_view(offset: int, length: int) -> memoryview:
    """``length`` payload bytes starting at absolute ``offset`` (no copy)."""
    if not 0 <= length <= BLOCK_BYTES:
        raise ValueError(f"length must be 0..{BLOCK_BYTES}, got {length}")
    phase = offset % BLOCK_BYTES
    return _double_block()[phase : phase + length]


def iter_chunks(offset: int, length: int, chunk: int) -> Iterator[memoryview]:
    """The payload range ``[offset, offset+length)`` in ``chunk``-sized views.

    Constant memory: every view points into the same 8 MiB buffer.
    """
    if chunk <= 0:
        raise ValueError("chunk must be > 0")
    pos = offset
    end = offset + length
    while pos < end:
        step = min(chunk, end - pos)
        yield chunk_view(pos, step)
        pos += step


@functools.lru_cache(maxsize=256)
def crc_of(offset: int, length: int) -> int:
    """CRC-32 of the payload range; cached, so reps pay for it only once."""
    crc = 0
    for view in iter_chunks(offset, length, BLOCK_BYTES):
        crc = zlib.crc32(view, crc)
    return crc


class Verifier:
    """Rolling CRC-32 + byte count of a received range.

    ``update`` accepts any buffer (``bytes``, ``memoryview`` of a recv buffer).
    """

    __slots__ = ("crc", "offset", "received")

    def __init__(self, offset: int = 0) -> None:
        self.offset = offset
        self.received = 0
        self.crc = 0

    def update(self, data: bytes | bytearray | memoryview) -> int:
        self.crc = zlib.crc32(data, self.crc)
        n = len(data)
        self.received += n
        return n

    def matches(self, expected_length: int) -> bool:
        """True when exactly the expected bytes arrived, unchanged and in order."""
        return self.received == expected_length and self.crc == crc_of(
            self.offset, expected_length
        )


def crc_rate_mib_s(seconds: float = 0.2) -> float:
    """How fast this laptop can CRC-32 the payload (MiB/s).

    Verification runs on the receiving side of every transport, so this is part
    of the tool's own ceiling and is recorded with the results.
    """
    block = chunk_view(0, BLOCK_BYTES)
    crc = 0
    total = 0
    start = time.perf_counter()
    while True:
        crc = zlib.crc32(block, crc)
        total += BLOCK_BYTES
        elapsed = time.perf_counter() - start
        if elapsed >= seconds:
            return total / (1024 * 1024) / elapsed
