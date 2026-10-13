# ruff: noqa: RUF001
"""V0.7 G6 tester r1: master key + AES-GCM store attacks (criterion 1).

AAD binding (owner/name swap, nonce/ciphertext/key_id tamper, wrong key), the
key file (0600 on creation, refused when group/other-readable, symlink, wrong
size, directory, FIFO that must not hang, concurrent first use from several
processes) and name/value normalisation tricks.
"""

from __future__ import annotations

import multiprocessing
import os
import stat
import sys
from pathlib import Path

import pytest

from meshforge.common.errors import FailureType
from meshforge.common.secret_names import (
    SecretNameError,
    SecretValueError,
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

POSIX = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
OWNER = "usr_01J0AAAAAAAAAAAAAAAAAAAAAA"
OTHER = "usr_01J0BBBBBBBBBBBBBBBBBBBBBB"
VALUE = b"tester-r1-plaintext-value"


# ----------------------------------------------------------------- names


@pytest.mark.parametrize(
    "raw",
    [
        "KEY",  # Kelvin sign
        "ＫＥＹ",  # full-width KEY
        "KеY",  # Cyrillic e
        "ıNDEX",  # dotless i
        "KſY",  # long s (upper() -> S)
        "ß",  # sharp s (upper() -> SS)
        "A B",
        "A-B",
        "A.B",
        "A/B",
        "A\\B",
        "../X",
        "..",
        ".",
        "A\x00B",
        "\x00",
        "",
        "   ",
        "1ABC",
        "9",
        "A" * 65,
        "A" * 10_000,
        "KEY=1",
        "KEY;rm",
        "KEY\u200b",  # zero width space
        "KEY%00",
        "%41",
    ],
)
def test_name_tricks_are_refused_without_echo(raw: str) -> None:
    with pytest.raises(SecretNameError) as exc:
        normalise_secret_name(raw)
    assert len(raw) < 5 or raw not in exc.value.message
    assert exc.value.failure_type is FailureType.INVALID_INPUT


@pytest.mark.parametrize("raw", [None, 5, b"KEY", ["KEY"], 1.5])
def test_non_string_names_are_refused(raw: object) -> None:
    with pytest.raises(SecretNameError):
        normalise_secret_name(raw)


def test_a_64_char_name_is_the_longest() -> None:
    assert normalise_secret_name("a" * 64) == "A" * 64
    with pytest.raises(SecretNameError):
        normalise_secret_name("a" * 65)


def test_name_normalisation_is_idempotent_and_collapses_only_case_and_edges() -> None:
    for raw in ("db_pass", "Db_Pass", " DB_PASS ", "DB_PASS\n"):
        assert normalise_secret_name(raw) == "DB_PASS"
        assert normalise_secret_name(normalise_secret_name(raw)) == "DB_PASS"


@pytest.mark.parametrize(
    ("size", "ok"),
    [(0, False), (1, False), (3, False), (4, True), (65536, True), (65537, False)],
)
def test_value_bounds(size: int, ok: bool) -> None:
    value = b"v" * size
    if ok:
        assert check_secret_value(value) == value
    else:
        with pytest.raises(SecretValueError):
            check_secret_value(value)


def test_value_keeps_binary_and_trailing_newline_bytes() -> None:
    raw = b"\x00\xff\n\r\n abc \n"
    assert check_secret_value(raw) == raw


@pytest.mark.parametrize("bad", ["text-value", None, 12345, ["a"]])
def test_a_non_bytes_value_is_refused(bad: object) -> None:
    with pytest.raises(SecretValueError):
        check_secret_value(bad)  # type: ignore[arg-type]


# ------------------------------------------------------------------ AAD


def _box(seed: int = 1) -> SecretBox:
    return SecretBox(bytes([seed]) * 32)


def test_a_row_cannot_be_opened_as_another_owner_or_name_or_key() -> None:
    box = _box()
    sealed = box.seal(VALUE, owner_user_id=OWNER, name="DB_PASS")
    assert box.open(sealed, owner_user_id=OWNER, name="DB_PASS") == VALUE
    for owner, name in ((OTHER, "DB_PASS"), (OWNER, "DB_PAS"), (OWNER, "db_pass")):
        with pytest.raises(SecretIntegrityError):
            box.open(sealed, owner_user_id=owner, name=name)
    with pytest.raises(SecretKeyMismatchError):
        _box(2).open(sealed, owner_user_id=OWNER, name="DB_PASS")


def test_owner_and_name_cannot_be_shifted_across_the_aad_separator() -> None:
    """('ab','c') vs ('a','bc') must never share an AAD."""
    box = _box()
    sealed = box.seal(VALUE, owner_user_id="usr_ab", name="C")
    for owner, name in (("usr_a", "BC"), ("usr_abC", ""), ("usr_ab\0C", "")):
        try:
            box.open(sealed, owner_user_id=owner, name=name)
        except (SecretIntegrityError, UnicodeError, ValueError):
            continue
        raise AssertionError((owner, name))


def test_every_single_bit_flip_in_ciphertext_or_nonce_is_detected() -> None:
    box = _box()
    sealed = box.seal(VALUE, owner_user_id=OWNER, name="K1")
    for i in range(len(sealed.ciphertext)):
        flipped = bytearray(sealed.ciphertext)
        flipped[i] ^= 0x01
        with pytest.raises(SecretIntegrityError):
            box.open(
                SealedSecret(bytes(flipped), sealed.nonce, sealed.key_id),
                owner_user_id=OWNER,
                name="K1",
            )
    for i in range(len(sealed.nonce)):
        nonce = bytearray(sealed.nonce)
        nonce[i] ^= 0x80
        with pytest.raises(SecretIntegrityError):
            box.open(
                SealedSecret(sealed.ciphertext, bytes(nonce), sealed.key_id),
                owner_user_id=OWNER,
                name="K1",
            )


@pytest.mark.parametrize("cut", [0, 1, 15, 16])
def test_truncated_ciphertext_and_odd_nonce_lengths_fail_typed(cut: int) -> None:
    box = _box()
    sealed = box.seal(VALUE, owner_user_id=OWNER, name="K1")
    with pytest.raises(SecretIntegrityError):
        box.open(
            SealedSecret(sealed.ciphertext[:cut], sealed.nonce, sealed.key_id),
            owner_user_id=OWNER,
            name="K1",
        )
    for nonce in (b"", sealed.nonce[:11], sealed.nonce + b"\0"):
        with pytest.raises(SecretIntegrityError):
            box.open(
                SealedSecret(sealed.ciphertext, nonce, sealed.key_id),
                owner_user_id=OWNER,
                name="K1",
            )


def test_the_ciphertext_and_error_texts_never_carry_the_plaintext() -> None:
    box = _box()
    value = b"very-recognisable-plaintext-0xDEADBEEF"
    sealed = box.seal(value, owner_user_id=OWNER, name="K1")
    assert value not in sealed.ciphertext and value[:8] not in sealed.ciphertext
    with pytest.raises(SecretIntegrityError) as exc:
        box.open(sealed, owner_user_id=OTHER, name="K1")
    assert value.decode() not in exc.value.message
    assert OTHER not in exc.value.message and OWNER not in exc.value.message


def test_sealing_the_same_value_twice_never_repeats_a_nonce() -> None:
    box = _box()
    seen = {box.seal(VALUE, owner_user_id=OWNER, name="K").nonce for _ in range(2000)}
    assert len(seen) == 2000


# ------------------------------------------------------------- key file


@POSIX
def test_first_use_creates_0600_in_a_0700_folder_and_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "secrets.key"
    key, created = load_or_create_master_key(path)
    assert created and len(key) == 32
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    again, created2 = load_or_create_master_key(path)
    assert again == key and not created2


@POSIX
@pytest.mark.parametrize("mode", [0o640, 0o604, 0o644, 0o660, 0o606, 0o666, 0o777])
def test_any_group_or_other_access_is_refused_and_the_key_is_not_replaced(
    tmp_path: Path, mode: int
) -> None:
    path = tmp_path / "controller" / "secrets.key"
    key, _ = load_or_create_master_key(path)
    os.chmod(path, mode)
    with pytest.raises(SecretKeyError) as exc:
        load_or_create_master_key(path)
    assert exc.value.failure_type is FailureType.CONFIG_ERROR
    assert path.read_bytes() == key  # never silently regenerated


@POSIX
def test_a_stricter_0400_key_still_loads(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "secrets.key"
    key, _ = load_or_create_master_key(path)
    os.chmod(path, 0o400)
    assert load_or_create_master_key(path)[0] == key


@POSIX
@pytest.mark.parametrize("size", [0, 1, 16, 31, 33, 64, 4096])
def test_wrong_size_key_is_refused_never_zero_padded(tmp_path: Path, size: int) -> None:
    path = tmp_path / "controller" / "secrets.key"
    load_or_create_master_key(path)
    path.write_bytes(b"k" * size)
    os.chmod(path, 0o600)
    with pytest.raises(SecretKeyError):
        load_or_create_master_key(path)
    assert path.read_bytes() == b"k" * size  # not overwritten


@POSIX
def test_symlink_directory_and_fifo_keys_are_refused(tmp_path: Path) -> None:
    folder = tmp_path / "controller"
    key, _ = load_or_create_master_key(folder / "secrets.key")
    real = tmp_path / "real.key"
    real.write_bytes(key)
    os.chmod(real, 0o600)
    (folder / "secrets.key").unlink()
    (folder / "secrets.key").symlink_to(real)
    with pytest.raises(SecretKeyError):
        load_or_create_master_key(folder / "secrets.key")
    (folder / "secrets.key").unlink()
    (folder / "secrets.key").mkdir()
    with pytest.raises(SecretKeyError):
        load_or_create_master_key(folder / "secrets.key")
    (folder / "secrets.key").rmdir()
    os.mkfifo(folder / "secrets.key", 0o600)
    with pytest.raises(SecretKeyError):  # must not block forever on open()
        load_or_create_master_key(folder / "secrets.key")


def _create_in_child(path: str, out: multiprocessing.Queue[bytes]) -> None:
    try:
        out.put(load_or_create_master_key(Path(path))[0])
    except BaseException as exc:  # noqa: BLE001
        out.put(f"ERR {type(exc).__name__}: {exc}".encode())


@POSIX
def test_concurrent_first_use_from_many_processes_yields_one_key(
    tmp_path: Path,
) -> None:
    path = tmp_path / "controller" / "secrets.key"
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    procs = [
        ctx.Process(target=_create_in_child, args=(str(path), out)) for _ in range(8)
    ]
    for p in procs:
        p.start()
    results = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(30)
    assert not [r for r in results if r.startswith(b"ERR")], results
    assert len(set(results)) == 1, "processes disagree about the master key"
    assert path.read_bytes() == results[0]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@POSIX
def test_a_pre_existing_open_folder_is_not_trusted_blindly_for_the_file(
    tmp_path: Path,
) -> None:
    """A world-readable key planted before first use is refused, not adopted."""
    folder = tmp_path / "controller"
    folder.mkdir(mode=0o700)
    (folder / "secrets.key").write_bytes(b"p" * 32)
    os.chmod(folder / "secrets.key", 0o644)
    with pytest.raises(SecretKeyError):
        load_or_create_master_key(folder / "secrets.key")


def test_as_env_refuses_every_ld_variable_as_the_docs_say() -> None:
    from meshforge.common.secret_names import check_env_name

    for name in ("LD_AUDIT", "LD_DEBUG", "LD_BIND_NOW"):
        with pytest.raises(SecretNameError):
            check_env_name(name)
