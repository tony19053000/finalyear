"""Task 0.1.6: SHA-256 helpers."""

import hashlib
import io
import tracemalloc
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

from meshforge.common.errors import DigestMismatchError, FailureType
from meshforge.common.hashing import (
    Digest,
    sha256_bytes,
    sha256_file,
    sha256_fileobj,
    verify_bytes,
    verify_file,
)

pytestmark = pytest.mark.unit

ABC_HEX = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
EMPTY_HEX = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_known_vectors() -> None:
    assert str(sha256_bytes(b"abc")) == f"sha256:{ABC_HEX}"
    assert sha256_bytes(b"").hex == EMPTY_HEX
    assert sha256_bytes(memoryview(b"abc")) == sha256_bytes(bytearray(b"abc"))


def test_digest_parse_round_trip_and_hashable() -> None:
    d = Digest.parse(f"sha256:{ABC_HEX}")
    assert d == Digest(ABC_HEX) == Digest.from_bytes(hashlib.sha256(b"abc").digest())
    assert Digest.parse(str(d)) == d
    assert d.algorithm == "sha256"
    assert len({d, Digest(ABC_HEX)}) == 1


@pytest.mark.parametrize(
    "bad",
    [
        ABC_HEX,  # no algorithm
        f"md5:{ABC_HEX}",
        f"sha256:{ABC_HEX.upper()}",  # canonical form is lower-case
        f"sha256:{ABC_HEX[:-1]}",
        f"sha256:{ABC_HEX}0",
        "sha256:",
        "",
    ],
)
def test_digest_parse_rejects_malformed(bad: str) -> None:
    with pytest.raises(ValueError):
        Digest.parse(bad)


def test_from_bytes_requires_32_bytes() -> None:
    with pytest.raises(ValueError):
        Digest.from_bytes(b"\x00" * 31)


@pytest.mark.parametrize("read_size", [1, 7, 4096, 1024 * 1024])
def test_file_and_fileobj_match_bytes(tmp_path: Path, read_size: int) -> None:
    data = bytes(range(256)) * 1000 + b"tail"
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    assert sha256_file(path, read_size=read_size) == sha256_bytes(data)
    assert sha256_fileobj(io.BytesIO(data), read_size=read_size) == sha256_bytes(data)


def test_fileobj_hashes_from_current_position_and_leaves_it_open() -> None:
    buf = io.BytesIO(b"xxabc")
    buf.seek(2)
    assert sha256_fileobj(buf).hex == ABC_HEX
    assert not buf.closed


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert sha256_file(path).hex == EMPTY_HEX


def test_invalid_read_size() -> None:
    with pytest.raises(ValueError):
        sha256_fileobj(io.BytesIO(b"x"), read_size=0)


def test_missing_file_raises_oserror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "nope")


def test_large_file_streams_in_constant_memory(tmp_path: Path) -> None:
    size_mib = 50
    path = tmp_path / "big.bin"
    block = hashlib.sha256(b"seed").digest() * (1024 * 1024 // 32)  # 1 MiB
    expected = hashlib.sha256()
    with path.open("wb") as f:
        for i in range(size_mib):
            piece = i.to_bytes(4, "big") + block[4:]
            f.write(piece)
            expected.update(piece)
    del block, piece

    tracemalloc.start()
    try:
        digest = sha256_file(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert digest.hex == expected.hexdigest()
    # One 1 MiB buffer plus small overhead — far below the 50 MiB file size.
    assert peak < 4 * 1024 * 1024, f"peak traced memory {peak} bytes"


def test_verify_success_accepts_digest_or_string(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abc")
    verify_file(path, f"sha256:{ABC_HEX}")
    verify_file(path, Digest(ABC_HEX))
    verify_bytes(b"abc", f"sha256:{ABC_HEX}")


def test_verify_mismatch_raises_typed_retryable_error(tmp_path: Path) -> None:
    path = tmp_path / "f"
    path.write_bytes(b"abd")
    with pytest.raises(DigestMismatchError) as info:
        verify_file(path, f"sha256:{ABC_HEX}")
    err = info.value
    assert err.failure_type is FailureType.INTEGRITY_ERROR
    assert err.retryable is True
    assert err.details["expected"] == f"sha256:{ABC_HEX}"
    assert err.details["actual"] == str(sha256_bytes(b"abd"))
    with pytest.raises(DigestMismatchError):
        verify_bytes(b"", Digest(ABC_HEX))


def test_verify_rejects_malformed_expected() -> None:
    with pytest.raises(ValueError):
        verify_bytes(b"abc", "not-a-digest")


class _Blob(BaseModel):
    digest: Digest


def test_digest_as_pydantic_field() -> None:
    blob = _Blob.model_validate({"digest": f"sha256:{ABC_HEX}"})
    assert blob.digest == Digest(ABC_HEX)
    assert blob.model_dump(mode="json") == {"digest": f"sha256:{ABC_HEX}"}
    assert _Blob.model_validate_json(blob.model_dump_json()) == blob
    with pytest.raises(ValidationError):
        _Blob.model_validate({"digest": "sha256:zz"})
    with pytest.raises(ValidationError):
        _Blob.model_validate({"digest": 123})
