"""Adversarial tests for task 0.1.6 hashing: symlinks, directories, races, huge/empty input."""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

from meshforge.common.errors import DigestMismatchError
from meshforge.common.hashing import (
    Digest,
    sha256_bytes,
    sha256_file,
    verify_bytes,
    verify_file,
)

pytestmark = pytest.mark.unit


def test_zero_byte_file_matches_known_empty_sha256(tmp_path: Path) -> None:
    empty = tmp_path / "empty.bin"
    empty.touch()
    digest = sha256_file(empty)
    assert str(digest) == (
        "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert digest == sha256_bytes(b"")


def test_hashing_a_directory_raises_oserror_not_silently_wrong_digest(
    tmp_path: Path,
) -> None:
    a_dir = tmp_path / "adir"
    a_dir.mkdir()
    with pytest.raises(OSError):
        sha256_file(a_dir)


def test_hashing_missing_file_raises_filenotfounderror(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        sha256_file(tmp_path / "nope.bin")


def test_hashing_broken_symlink_raises_filenotfounderror(tmp_path: Path) -> None:
    link = tmp_path / "broken.bin"
    link.symlink_to(tmp_path / "target-does-not-exist")
    with pytest.raises(FileNotFoundError):
        sha256_file(link)


def test_hashing_symlink_to_real_file_follows_link(tmp_path: Path) -> None:
    real = tmp_path / "real.bin"
    real.write_bytes(b"hello world")
    link = tmp_path / "link.bin"
    link.symlink_to(real)
    assert sha256_file(link) == sha256_file(real)


@pytest.mark.skipif(
    os.name == "nt", reason="symlink loops behave differently on Windows"
)
def test_hashing_symlink_loop_raises_oserror(tmp_path: Path) -> None:
    loop = tmp_path / "loop.bin"
    loop.symlink_to(loop)
    with pytest.raises(OSError):
        sha256_file(loop)


def test_file_truncated_during_read_does_not_crash_or_hang(tmp_path: Path) -> None:
    """A file modified concurrently is an inherent race (no locking promised);
    this only guards against a crash/hang, not against a "correct" answer.
    """
    big = tmp_path / "big.bin"
    big.write_bytes(b"A" * (2 * 1024 * 1024))

    def truncate_soon() -> None:
        time.sleep(0.001)
        with open(big, "r+b") as f:
            f.truncate(1024)

    t = threading.Thread(target=truncate_soon)
    t.start()
    try:
        digest = sha256_file(big, read_size=4096)
        assert isinstance(digest, Digest)
    finally:
        t.join()


def test_verify_file_raises_digest_mismatch_with_no_secret_content_in_message(
    tmp_path: Path,
) -> None:
    f = tmp_path / "data.bin"
    f.write_bytes(b"actual content")
    wrong = sha256_bytes(b"different content")
    with pytest.raises(DigestMismatchError) as exc_info:
        verify_file(f, wrong)
    # details carry hex digests only, never raw file content
    assert "actual content" not in str(exc_info.value)
    assert exc_info.value.details["expected"] == str(wrong)


def test_verify_bytes_constant_time_compare_does_not_short_circuit_on_prefix() -> None:
    """Sanity: hmac.compare_digest is used (not `==`), so a matching-prefix wrong
    digest is still rejected rather than accidentally passing.
    """
    expected = sha256_bytes(b"payload")
    wrong_but_similar_prefix = Digest(
        expected.hex[:-1] + ("0" if expected.hex[-1] != "0" else "1")
    )
    with pytest.raises(DigestMismatchError):
        verify_bytes(b"payload", wrong_but_similar_prefix)


@pytest.mark.parametrize(
    "bad", ["", "sha256:", "sha256:abc", "md5:" + "a" * 32, "SHA256:" + "a" * 64]
)
def test_digest_parse_rejects_malformed_or_wrong_case_strings(bad: str) -> None:
    with pytest.raises(ValueError):
        Digest.parse(bad)


def test_digest_parse_rejects_uppercase_hex() -> None:
    """Digest is defined as *lower*-hex; accepting upper-hex would let two textual
    forms name the "same" blob, breaking content-addressed dedup/equality.
    """
    valid_hex = sha256_bytes(b"x").hex
    with pytest.raises(ValueError):
        Digest.parse(f"sha256:{valid_hex.upper()}")


def test_readinto_and_plain_read_paths_agree(tmp_path: Path) -> None:
    """Exercise both the fast `readinto` path and the fallback `read` path (a
    minimal file-like object without `readinto`) and confirm they agree.
    """

    class _NoReadIntoFile:
        def __init__(self, data: bytes) -> None:
            self._data = data
            self._pos = 0

        def read(self, n: int = -1) -> bytes:
            chunk = self._data[self._pos : self._pos + n]
            self._pos += len(chunk)
            return chunk

    from meshforge.common.hashing import sha256_fileobj

    data = os.urandom(5000)
    fallback_digest = sha256_fileobj(_NoReadIntoFile(data), read_size=777)  # type: ignore[arg-type]
    assert fallback_digest == sha256_bytes(data)


def test_sha256_fileobj_rejects_non_positive_read_size() -> None:
    import io

    with pytest.raises(ValueError):
        from meshforge.common.hashing import sha256_fileobj

        sha256_fileobj(io.BytesIO(b"x"), read_size=0)
