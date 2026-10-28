"""V0.17 G2 (D392, Q20): the setup state machine, the token file, the admin
password rule and the setup settings. Pure (files in tmp_path)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pydantic import SecretStr

from meshforge.common.private_files import PrivateFileError
from meshforge.controller.auth.passwords import PasswordError
from meshforge.controller.settings import ControllerSettings
from meshforge.controller.setup.policy import check_admin_password
from meshforge.controller.setup.routes import SETUP_PATHS
from meshforge.controller.setup.state import SetupState, setup_state
from meshforge.controller.setup.token import (
    ensure_setup_token,
    read_setup_token,
    remove_setup_token,
    setup_token_matches,
)
from meshforge.controller.store.settings import DatabaseSettings

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("ca", "users", "token", "expected"),
    [
        (False, False, True, SetupState.PENDING),  # a new controller
        (False, False, False, SetupState.UNAVAILABLE),  # mtls refuses to start
        (True, False, True, SetupState.DONE),  # replay after success -> 410
        (True, True, False, SetupState.DONE),
        (False, True, True, SetupState.DONE),  # any user exists -> 410
        (True, False, False, SetupState.DONE),
    ],
)
def test_state_machine(
    ca: bool, users: bool, token: bool, expected: SetupState
) -> None:
    assert (
        setup_state(ca_present=ca, users_exist=users, token_present=token) is expected
    )


def test_token_file_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "controller" / "setup-token"
    assert read_setup_token(path) is None
    token, created = ensure_setup_token(path)
    assert created and len(token.get_secret_value()) >= 43
    if os.name != "nt":  # POSIX file modes
        assert path.stat().st_mode & 0o777 == 0o600
    again, created_again = ensure_setup_token(path)
    assert not created_again
    assert again.get_secret_value() == token.get_secret_value()
    assert setup_token_matches(token, token.get_secret_value())
    assert not setup_token_matches(token, token.get_secret_value()[:-1] + "x")
    assert not setup_token_matches(token, "")
    assert not setup_token_matches(token, "a" * 1000)
    assert not setup_token_matches(None, token.get_secret_value())
    if os.name != "nt":
        # POSIX: readable by others is refused, never used. Windows has no mode
        # bits; its owner-only ACL is applied at write time and not re-read
        # (private_files module doc, THREAT_MODEL LF-S2).
        path.chmod(0o644)
        with pytest.raises(PrivateFileError):
            read_setup_token(path)
    remove_setup_token(path)
    remove_setup_token(path)
    assert read_setup_token(path) is None


@pytest.mark.parametrize(
    ("password", "why"),
    [
        ("short", "characters"),
        ("abcabcabcabcabc", "different"),
        ("asha-the-admin-2026!", "user name"),
        ("Password1234", "too common"),
        ("x" * 2000, "characters"),
    ],
)
def test_admin_password_rule_refuses(password: str, why: str) -> None:
    with pytest.raises(PasswordError, match=why) as info:
        check_admin_password("asha", SecretStr(password))
    assert password[:8] not in str(info.value) or len(password) < 8


def test_admin_password_rule_accepts_a_passphrase() -> None:
    check_admin_password("asha", SecretStr("correct horse battery staple 42"))


@pytest.mark.parametrize(
    ("name", "password", "refused"),
    [
        ("a", "correct horse battery 42", False),  # 1-2 letters: rule off
        ("ab", "abacus-drawing-2026", False),
        ("abc", "abcdef-drawing-2026", True),  # 3+: rule on
        ("ASHA", "my-asha-pass-2026", True),  # case-insensitive
        ("  asha  ", "my-AsHa-pass-2026", True),  # name trimmed
        ("   ", "correct horse battery 42", False),
    ],
)
def test_admin_name_rule_needs_three_characters(
    name: str, password: str, refused: bool
) -> None:
    if refused:
        with pytest.raises(PasswordError, match="user name"):
            check_admin_password(name, SecretStr(password))
    else:
        check_admin_password(name, SecretStr(password))


def test_setup_settings(tmp_path: Path) -> None:
    default = ControllerSettings(data_dir=tmp_path, database=DatabaseSettings())
    assert default.setup_bind == "127.0.0.1:47110"
    assert default.setup_token_path == tmp_path / "controller" / "setup-token"
    custom = ControllerSettings(
        data_dir=tmp_path,
        controller={"http_bind": "192.168.1.5:48000", "allow_any_bind": False},
        database=DatabaseSettings(),
    )
    assert custom.setup_bind == "127.0.0.1:48000"
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        ControllerSettings(
            data_dir=tmp_path,
            controller={"setup": {"bind": "192.168.1.5:47110"}},
            database=DatabaseSettings(),
        )


def test_setup_paths_are_open_routes_of_the_main_app() -> None:
    from meshforge.controller.api.auth import OPEN_ROUTES

    assert SETUP_PATHS <= OPEN_ROUTES
