"""Fabric token format and the bounded, validating reader (D44)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.fabric_token import (
    MAX_TOKEN_CHARS,
    MAX_TOKEN_FILE_BYTES,
    MIN_TOKEN_CHARS,
    FabricTokenError,
    default_token_path,
    generate_token,
    parse_token,
    read_token_file,
)


def test_generated_tokens_are_valid_and_distinct() -> None:
    a, b = generate_token(), generate_token()
    assert len(a.get_secret_value()) == 43
    assert parse_token(a.get_secret_value()) == a
    assert a != b
    assert a.get_secret_value() not in repr(a)


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("a" * MIN_TOKEN_CHARS, True),
        ("a" * (MIN_TOKEN_CHARS - 1), False),
        ("a" * MAX_TOKEN_CHARS, True),
        ("a" * (MAX_TOKEN_CHARS + 1), False),
        ("A-z_09" * 6, True),
        ("a" * 40 + " b", False),
        ("a" * 40 + "=", False),
        ("a" * 40 + "é", False),
        ("  " + "a" * 40 + "\n", True),
    ],
)
def test_token_shape_bounds(value: str, ok: bool) -> None:
    if ok:
        parse_token(value)
    else:
        with pytest.raises(FabricTokenError) as info:
            parse_token(value)
        assert info.value.failure_type is FailureType.CONFIG_ERROR
        assert value.strip() not in info.value.message


def test_file_size_bound(tmp_path: Path) -> None:
    token = "t" * 43
    path = tmp_path / "tok"
    path.write_bytes(token.encode() + b"\n" * (MAX_TOKEN_FILE_BYTES - 43))
    assert read_token_file(path).get_secret_value() == token
    path.write_bytes(token.encode() + b"\n" * (MAX_TOKEN_FILE_BYTES - 42))
    with pytest.raises(FabricTokenError, match="larger than"):
        read_token_file(path)


def test_missing_non_ascii_and_malformed_files(tmp_path: Path) -> None:
    with pytest.raises(FabricTokenError, match="not found"):
        read_token_file(tmp_path / "absent")
    (tmp_path / "bin").write_bytes(b"\xff" * 40)
    with pytest.raises(FabricTokenError, match="ASCII"):
        read_token_file(tmp_path / "bin")
    (tmp_path / "short").write_text("abc")
    with pytest.raises(FabricTokenError, match="URL-safe"):
        read_token_file(tmp_path / "short")


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX mode bits")
def test_require_private_refuses_a_world_readable_file(tmp_path: Path) -> None:
    path = tmp_path / "tok"
    path.write_text("a" * 43)
    os.chmod(path, 0o644)
    assert read_token_file(path).get_secret_value() == "a" * 43
    with pytest.raises(FabricTokenError, match="other users"):
        read_token_file(path, require_private=True)
    os.chmod(path, 0o600)
    read_token_file(path, require_private=True)


def test_default_path_is_under_the_data_dir(tmp_path: Path) -> None:
    assert default_token_path(tmp_path) == tmp_path / "controller" / "fabric.token"
