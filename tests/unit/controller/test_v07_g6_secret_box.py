"""V0.7 G6 deliverable 1 (D144): secret names/values and the at-rest crypto.

* names: ASCII before case folding (a Kelvin sign or full-width letter never
  aliases), upper-cased, ``[A-Z_][A-Z0-9_]{0,63}``; 64 accepted, 65 refused;
* values: 4 bytes .. 64 KiB (3 and 64 KiB + 1 refused);
* AES-256-GCM: round trip, fresh nonce per seal, the ciphertext never holds
  the value, AAD binds owner + name (another owner, another name, a flipped
  bit, another master key -> typed integrity error);
* the master key file: created 32 bytes ``0600`` in a ``0700`` folder, reused,
  refused when readable by others / a symlink / the wrong size (``config_error``).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.secret_names import (
    MAX_SECRET_BYTES,
    SecretNameError,
    SecretValueError,
    check_env_name,
    check_secret_value,
    normalise_secret_name,
)
from meshforge.controller.secret_box import (
    SealedSecret,
    SecretBox,
    SecretIntegrityError,
    SecretKeyError,
    SecretKeyMismatchError,
    load_or_create_master_key,
)

pytestmark = pytest.mark.unit

OWNER = "usr_01J0AAAAAAAAAAAAAAAAAAAAAA"
OTHER = "usr_01J0BBBBBBBBBBBBBBBBBBBBBB"
VALUE = b"hunter2-correct-horse"
POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("db_pass", "DB_PASS"),
        ("  Api_Key ", "API_KEY"),
        ("_X", "_X"),
        ("A" * 64, "A" * 64),
    ],
)
def test_names_normalise(raw: str, name: str) -> None:
    assert normalise_secret_name(raw) == name


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "A" * 65,
        "1ABC",
        "A-B",
        "A B",
        "A.B",
        chr(0x212A) + "EY",  # Kelvin sign: would fold to K
        chr(0xFF21) + "PI",  # full-width A
        "ü",
        "A\x00",
        7,
        None,
    ],
)
def test_bad_names_are_refused_without_echo(raw: object) -> None:
    with pytest.raises(SecretNameError) as exc:
        normalise_secret_name(raw)
    assert exc.value.failure_type is FailureType.INVALID_INPUT
    if isinstance(raw, str) and len(raw) > 2:
        assert raw not in str(exc.value)


def test_reserved_env_names() -> None:
    assert check_env_name("API_KEY") == "API_KEY"
    for bad in ("PATH", "MESHFORGE_SECRETS_DIR", "LD_PRELOAD", "PYTHONPATH"):
        with pytest.raises(SecretNameError):
            check_env_name(bad)


def test_value_bounds() -> None:
    assert check_secret_value(b"abcd") == b"abcd"
    assert len(check_secret_value(b"x" * MAX_SECRET_BYTES)) == MAX_SECRET_BYTES
    for bad in (b"", b"abc", b"x" * (MAX_SECRET_BYTES + 1)):
        with pytest.raises(SecretValueError):
            check_secret_value(bad)


def _box() -> SecretBox:
    return SecretBox(os.urandom(32))


def test_round_trip_and_fresh_nonces() -> None:
    box = _box()
    a = box.seal(VALUE, owner_user_id=OWNER, name="API_KEY")
    b = box.seal(VALUE, owner_user_id=OWNER, name="API_KEY")
    assert a.nonce != b.nonce and a.ciphertext != b.ciphertext
    assert VALUE not in a.ciphertext
    assert len(a.ciphertext) == len(VALUE) + 16 and len(a.nonce) == 12
    assert box.open(a, owner_user_id=OWNER, name="API_KEY") == VALUE


@pytest.mark.parametrize(
    ("owner", "name"), [(OTHER, "API_KEY"), (OWNER, "API_KEY2"), (OTHER, "OTHER")]
)
def test_aad_binds_owner_and_name(owner: str, name: str) -> None:
    box = _box()
    sealed = box.seal(VALUE, owner_user_id=OWNER, name="API_KEY")
    with pytest.raises(SecretIntegrityError) as exc:
        box.open(sealed, owner_user_id=owner, name=name)
    assert exc.value.failure_type is FailureType.INTEGRITY_ERROR
    assert VALUE.decode() not in str(exc.value)


def test_tampering_and_another_key_fail_typed() -> None:
    box = _box()
    sealed = box.seal(VALUE, owner_user_id=OWNER, name="K")
    flipped = bytearray(sealed.ciphertext)
    flipped[0] ^= 1
    with pytest.raises(SecretIntegrityError):
        box.open(
            SealedSecret(bytes(flipped), sealed.nonce, sealed.key_id),
            owner_user_id=OWNER,
            name="K",
        )
    with pytest.raises(SecretIntegrityError):
        box.open(
            SealedSecret(sealed.ciphertext, sealed.nonce[:8], sealed.key_id),
            owner_user_id=OWNER,
            name="K",
        )
    other = _box()
    with pytest.raises(SecretKeyMismatchError):
        other.open(sealed, owner_user_id=OWNER, name="K")


@POSIX
def test_the_key_file_is_created_private_and_reused(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "secrets.key"
    key, created = load_or_create_master_key(path)
    assert created and len(key) == 32
    assert (path.stat().st_mode & 0o777) == 0o600
    assert (path.parent.stat().st_mode & 0o777) == 0o700
    again, created2 = load_or_create_master_key(path)
    assert again == key and not created2


@POSIX
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o660])
def test_a_key_readable_by_others_is_refused(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "controller" / "secrets.key"
    load_or_create_master_key(path)
    path.chmod(mode)
    with pytest.raises(SecretKeyError) as exc:
        load_or_create_master_key(path)
    assert exc.value.failure_type is FailureType.CONFIG_ERROR
    assert path.read_bytes()  # never replaced by a new key


@POSIX
def test_a_symlink_or_wrong_size_key_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "controller"
    folder.mkdir(mode=0o700)
    real = tmp_path / "real.key"
    real.write_bytes(os.urandom(32))
    real.chmod(0o600)
    (folder / "secrets.key").symlink_to(real)
    with pytest.raises(SecretKeyError):
        load_or_create_master_key(folder / "secrets.key")
    (folder / "secrets.key").unlink()
    for size in (31, 33, 0):
        (folder / "secrets.key").write_bytes(b"k" * size)
        (folder / "secrets.key").chmod(0o600)
        with pytest.raises(SecretKeyError):
            load_or_create_master_key(folder / "secrets.key")
        (folder / "secrets.key").unlink()
