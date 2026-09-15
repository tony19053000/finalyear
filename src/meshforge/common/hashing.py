"""SHA-256 helpers for the content-addressed data plane (ADR-0003, ARCHITECTURE §14).

* :class:`Digest` is the canonical, printable name of a blob: ``sha256:<64 lower-hex>``.
  The algorithm prefix leaves room for another algorithm later without ambiguity.
  It is immutable and hashable, and usable directly as a Pydantic field type
  (validated from a string, serialised back to the string form).
* File / file-object hashing is streaming with one reusable buffer
  (``readinto``), so memory use is constant regardless of file size.
  :data:`DEFAULT_READ_SIZE` is the *read buffer* size, unrelated to the 8 MiB
  *transfer chunk* size of the blob protocol.
* ``verify_*`` raise :class:`~meshforge.common.errors.DigestMismatchError` (typed,
  retryable: the usual cause is a corrupted transfer, fixed by re-fetching).

Async variants are not needed yet; callers can run these in a thread
(``asyncio.to_thread``) since hashlib releases the GIL on large updates.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass
from typing import IO, Any, Self

from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from meshforge.common.errors import DigestMismatchError

__all__ = [
    "DEFAULT_READ_SIZE",
    "Digest",
    "sha256_bytes",
    "sha256_file",
    "sha256_fileobj",
    "verify_bytes",
    "verify_file",
]

DEFAULT_READ_SIZE = 1024 * 1024
"""Read buffer size used when streaming files (1 MiB)."""

ALGORITHM = "sha256"
_HEX_RE = re.compile(r"[0-9a-f]{64}")

type Buffer = bytes | bytearray | memoryview
type StrPath = str | os.PathLike[str]


@dataclass(frozen=True, slots=True)
class Digest:
    """A SHA-256 content digest. ``str(d)`` → ``sha256:<hex>``."""

    hex: str

    def __post_init__(self) -> None:
        if not isinstance(self.hex, str) or not _HEX_RE.fullmatch(self.hex):
            raise ValueError(f"invalid sha256 hex digest: {self.hex!r}")

    @classmethod
    def parse(cls, value: str) -> Self:
        """Parse ``sha256:<64 lower-hex>`` (the only accepted textual form)."""
        if not isinstance(value, str):
            # ValueError (not TypeError) so Pydantic reports it as a ValidationError.
            raise ValueError(f"digest must be a string, got {type(value).__name__}")  # noqa: TRY004
        algorithm, sep, hex_part = value.partition(":")
        if not sep or algorithm != ALGORITHM:
            raise ValueError(f"digest must look like 'sha256:<hex>', got {value!r}")
        return cls(hex_part)

    @classmethod
    def from_bytes(cls, raw: bytes) -> Self:
        """Build from a raw 32-byte digest (e.g. ``hashlib.sha256().digest()``)."""
        if len(raw) != 32:
            raise ValueError(f"raw sha256 digest must be 32 bytes, got {len(raw)}")
        return cls(raw.hex())

    @property
    def algorithm(self) -> str:
        return ALGORITHM

    def __str__(self) -> str:
        return f"{ALGORITHM}:{self.hex}"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        def validate(value: object) -> Digest:
            if isinstance(value, Digest):
                return value
            if isinstance(value, str):
                return Digest.parse(value)
            raise ValueError(f"digest must be a string, got {type(value).__name__}")

        return core_schema.no_info_plain_validator_function(
            validate,
            serialization=core_schema.plain_serializer_function_ser_schema(str),
        )


def sha256_bytes(data: Buffer) -> Digest:
    """Digest of an in-memory buffer."""
    return Digest(hashlib.sha256(data).hexdigest())


def sha256_fileobj(fileobj: IO[bytes], *, read_size: int = DEFAULT_READ_SIZE) -> Digest:
    """Digest of a binary file object, read from its current position to EOF.

    Uses a single reusable buffer, so memory is O(read_size). The caller owns the
    file object (it is not closed or rewound).
    """
    if read_size <= 0:
        raise ValueError("read_size must be positive")
    hasher = hashlib.sha256()
    buffer = bytearray(read_size)
    view = memoryview(buffer)
    readinto = getattr(fileobj, "readinto", None)
    if readinto is not None:
        while True:
            n = readinto(buffer)
            if not n:
                break
            hasher.update(view[:n])
    else:  # pragma: no cover - exotic file objects without readinto
        while chunk := fileobj.read(read_size):
            hasher.update(chunk)
    return Digest(hasher.hexdigest())


def sha256_file(path: StrPath, *, read_size: int = DEFAULT_READ_SIZE) -> Digest:
    """Digest of a file on disk, streamed in ``read_size`` pieces."""
    with open(path, "rb", buffering=0) as f:
        return sha256_fileobj(f, read_size=read_size)


def _coerce(expected: Digest | str) -> Digest:
    return expected if isinstance(expected, Digest) else Digest.parse(expected)


def _check(actual: Digest, expected: Digest, what: str) -> None:
    if not hmac.compare_digest(actual.hex, expected.hex):
        raise DigestMismatchError(
            f"sha256 mismatch for {what}",
            details={"expected": str(expected), "actual": str(actual)},
        )


def verify_bytes(data: Buffer, expected: Digest | str) -> None:
    """Raise :class:`DigestMismatchError` unless ``data`` hashes to ``expected``."""
    _check(sha256_bytes(data), _coerce(expected), "in-memory data")


def verify_file(
    path: StrPath, expected: Digest | str, *, read_size: int = DEFAULT_READ_SIZE
) -> None:
    """Raise :class:`DigestMismatchError` unless the file hashes to ``expected``."""
    _check(sha256_file(path, read_size=read_size), _coerce(expected), os.fspath(path))
