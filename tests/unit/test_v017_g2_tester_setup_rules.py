"""V0.17 G2 tester: pure-logic edge cases of the setup page's rules.

Derived from the brief (5b item 6/8): the state machine (no CA / no user -> setup;
any user -> 410; token wrong/missing -> 401), the first admin's password rule,
the one-time token file (0600, constant-time compare), the loopback-only bind,
and backup file naming that must never be steerable by a version string.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy", reason="needs the controller extra")

import itertools

from pydantic import SecretStr, ValidationError

from meshforge.controller.auth.passwords import PasswordError
from meshforge.controller.db_managed.backup import (
    list_backups,
    manual_backup_path,
    pre_upgrade_path,
)
from meshforge.controller.settings import DbSettings, SetupSettings
from meshforge.controller.setup.policy import check_admin_password
from meshforge.controller.setup.state import SetupState, setup_state
from meshforge.controller.setup.token import (
    ensure_setup_token,
    read_setup_token,
    remove_setup_token,
    setup_token_matches,
)

pytestmark = pytest.mark.unit


# ------------------------------------------------------------ state machine
@pytest.mark.parametrize(
    ("ca", "users", "token"), list(itertools.product([False, True], repeat=3))
)
def test_state_table_is_total_and_never_offers_setup_once_anything_exists(
    ca: bool, users: bool, token: bool
) -> None:
    state = setup_state(ca_present=ca, users_exist=users, token_present=token)
    if ca or users:
        assert state is SetupState.DONE  # 410 forever, whatever the token says
    elif token:
        assert state is SetupState.PENDING
    else:
        assert state is SetupState.UNAVAILABLE


# ------------------------------------------------------- admin password rule
def _check(name: str, pw: str) -> None:
    check_admin_password(name, SecretStr(pw))


def test_admin_password_boundaries() -> None:
    _check("asha", "abcdef123456")  # exactly 12 chars, 12 distinct
    with pytest.raises(PasswordError):
        _check("asha", "abcdef12345")  # 11
    _check("asha", "abcdefabcdef")  # 12 chars, exactly 6 distinct
    with pytest.raises(PasswordError):
        _check("asha", "abcdeabcdeab")  # 5 distinct
    with pytest.raises(PasswordError):
        _check("asha", " " * 40)  # long but one character
    with pytest.raises(PasswordError):
        _check("asha", "")


def test_admin_password_name_check_is_case_insensitive() -> None:
    for pw in ("xxASHAxx-Passw0rd!", "my-AsHa-is-fine-1", "ASHA" + "zq9#" * 4):
        with pytest.raises(PasswordError, match="user name"):
            _check("asha", pw)


@pytest.mark.parametrize(
    "common", ["PASSWORD1234", "Qwertyuiopas", "MeshForge123", "ADMINISTRATOR"]
)
def test_common_passwords_refused_any_case(common: str) -> None:
    with pytest.raises(PasswordError):
        _check("bob", common)


def test_unicode_passwords_count_characters_not_bytes() -> None:
    _check("bob", "пароль-пароль-123")  # cyrillic passphrase accepted
    _check("bob", "密码密码密码密码密码密码abcxyz")
    with pytest.raises(PasswordError):
        _check("bob", "ééééééééééééé")  # 13 chars but 1 distinct
    with pytest.raises(PasswordError, match="NUL"):
        _check("bob", "correct horse\x00battery")


def test_password_length_cap_and_error_never_echoes_the_secret() -> None:
    secret = "Zq9#" * 300  # 1200 chars
    with pytest.raises(PasswordError) as info:
        _check("bob", secret)
    assert secret not in str(info.value) and secret[:20] not in info.value.message
    with pytest.raises(PasswordError) as short:
        _check("bob", "hunter2")
    assert "hunter2" not in short.value.message


@pytest.mark.xfail(
    strict=True,
    reason="DEFECT G2-T1 (minor): a 1-2 character admin name makes the 'must not "
    "contain the user name' rule reject nearly every passphrase",
)
def test_single_letter_admin_name_does_not_make_every_password_unusable() -> None:
    # An admin called "a" must still be able to pick a normal passphrase.
    # (Observed behaviour is reported in the tester report if this fails.)
    _check("a", "correct horse battery staple 42")


# --------------------------------------------------------------- token file
def test_token_is_private_random_and_stable(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "setup-token"
    first, created = ensure_setup_token(path)
    assert created and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) & 0o077 == 0
    again, created_again = ensure_setup_token(path)
    assert not created_again
    assert again.get_secret_value() == first.get_secret_value()
    assert len(first.get_secret_value()) >= 43
    other, _ = ensure_setup_token(tmp_path / "x" / "setup-token")
    assert other.get_secret_value() != first.get_secret_value()


@pytest.mark.skipif(os.name != "posix", reason="POSIX modes")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666])
def test_a_token_other_users_can_read_is_refused(tmp_path: Path, mode: int) -> None:
    from meshforge.common.private_files import PrivateFileError

    path = tmp_path / "setup-token"
    path.write_text("a" * 43 + "\n")
    path.chmod(mode)
    with pytest.raises(PrivateFileError):
        read_setup_token(path)
    with pytest.raises(PrivateFileError):
        ensure_setup_token(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX symlinks")
def test_a_symlinked_token_file_is_not_followed(tmp_path: Path) -> None:
    from meshforge.common.private_files import PrivateFileError

    real = tmp_path / "real"
    real.write_text("b" * 43 + "\n")
    real.chmod(0o600)
    link = tmp_path / "setup-token"
    link.symlink_to(real)
    try:
        got = read_setup_token(link)
    except PrivateFileError:
        return  # refused: fine
    pytest.fail(f"symlinked token file was followed and read: {got!r}")


def test_empty_and_oversized_token_files_fail_closed(tmp_path: Path) -> None:
    from meshforge.common.private_files import PrivateFileError

    path = tmp_path / "setup-token"
    path.write_text("")
    path.chmod(0o600)
    with pytest.raises(PrivateFileError):
        read_setup_token(path)
    path.write_text("a" * 10_000)
    with pytest.raises(PrivateFileError):
        read_setup_token(path)


def test_missing_token_is_none_and_remove_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "setup-token"
    assert read_setup_token(path) is None
    remove_setup_token(path)
    ensure_setup_token(path)
    remove_setup_token(path)
    remove_setup_token(path)
    assert read_setup_token(path) is None


def test_token_comparison_edge_cases() -> None:
    stored = SecretStr("S" * 43)
    assert setup_token_matches(stored, "S" * 43)
    assert not setup_token_matches(stored, "S" * 42)
    assert not setup_token_matches(stored, "S" * 43 + " ")
    assert not setup_token_matches(stored, "s" * 43)
    assert not setup_token_matches(stored, "")
    assert not setup_token_matches(None, "S" * 43)
    assert not setup_token_matches(stored, "S" * 10_000)
    assert not setup_token_matches(stored, "S\x00" * 20)
    assert not setup_token_matches(stored, "\udcff" * 3 if False else "é" * 43)


# --------------------------------------------------------- loopback-only bind
@pytest.mark.parametrize(
    "bind",
    ["0.0.0.0:47110", "192.168.1.5:47110", "10.0.0.1:1", "[::]:47110", "8.8.8.8:80"],
)
def test_setup_bind_refuses_anything_not_loopback(bind: str) -> None:
    with pytest.raises(ValidationError):
        SetupSettings(bind=bind)


@pytest.mark.parametrize("bind", ["127.0.0.1:47110", "[::1]:47110", "127.0.0.1:0"])
def test_setup_bind_accepts_loopback(bind: str) -> None:
    assert SetupSettings(bind=bind).bind == bind


@pytest.mark.parametrize("bind", ["", "localhost", "127.0.0.1", ":47110", "x:y"])
def test_setup_bind_garbage_is_a_config_error_not_a_crash(bind: str) -> None:
    with pytest.raises(ValidationError):
        SetupSettings(bind=bind)


# ---------------------------------------------------- managed db settings
def test_db_settings_relative_and_empty_paths_refused() -> None:
    for field in ("root", "socket_dir", "bin_dir"):
        with pytest.raises(ValidationError):
            DbSettings(**{field: "relative/dir"})
        with pytest.raises(ValidationError):
            DbSettings(**{field: ""})
    with pytest.raises(ValidationError):
        DbSettings(port=0)
    with pytest.raises(ValidationError):
        DbSettings(port=70000)
    with pytest.raises(ValidationError):
        DbSettings(backups_keep=0)
    with pytest.raises(ValidationError):
        DbSettings(health_timeout_s=0)
    assert DbSettings().port == 47150  # never 5432


# ---------------------------------------------------------- backup naming
@pytest.mark.parametrize(
    "version",
    ["../../etc/cron.d/x", "1.0/../..", "a b;rm -rf /", "\x00", "", "...", "é" * 500],
)
def test_pre_upgrade_path_cannot_leave_the_backups_dir(
    tmp_path: Path, version: str
) -> None:
    path = pre_upgrade_path(tmp_path, version)
    assert path.parent == tmp_path
    assert "/" not in path.name and "\x00" not in path.name
    assert path.name.startswith("pre-") and path.name.endswith(".dump")
    assert len(path.name) < 200


def test_list_backups_ignores_partials_dirs_and_hidden(tmp_path: Path) -> None:
    (tmp_path / "pre-1.dump").write_bytes(b"x")
    (tmp_path / "manual-20260101T000000Z.dump").write_bytes(b"x")
    (tmp_path / ".pre-2.dump").write_bytes(b"x")
    (tmp_path / "pre-3.dump.partial").write_bytes(b"x")
    (tmp_path / "dir.dump").mkdir()
    (tmp_path / "notes.txt").write_text("hi")
    names = {b.path.name for b in list_backups(tmp_path)}
    assert names == {"pre-1.dump", "manual-20260101T000000Z.dump"}
    assert list_backups(tmp_path / "missing") == []
    assert manual_backup_path(tmp_path).parent == tmp_path
