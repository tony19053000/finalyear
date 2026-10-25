"""V0.17 G4 tester: console loopback and token rules (brief 5d item 3 and 4).

Real sockets / real objects; no Windows needed for the rules themselves. The
Windows token-ACL application is covered by fakes elsewhere and by the CI job.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest
from pydantic import ValidationError

from meshforge.agent.console import auth
from meshforge.agent.console.auth import (
    ConsoleAuth,
    host_ok,
    origin_ok,
    windows_folder_acl,
)
from meshforge.agent.console.server import bind_console_socket
from meshforge.agent.settings import ConsoleSettings
from meshforge.common.errors import ConfigError

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def _auth(clock: Clock | None = None, **kw: object) -> ConsoleAuth:
    return ConsoleAuth(
        port=47130, session_ttl_s=100.0, launch_code_ttl_s=10.0,
        clock=clock or Clock(), **kw,  # type: ignore[arg-type]
    )  # fmt: skip


# ------------------------------------------------------------------- loopback
@pytest.mark.parametrize(
    "bind",
    [
        "0.0.0.0",  # noqa: S104 - the value under test
        "::",
        "::1",
        "localhost",
        "192.168.1.5",
        "127.0.0.2",
        "",
        "127.0.0.1:1",
        "*",
    ],
)
def test_settings_refuse_any_non_127_0_0_1_bind(bind: str) -> None:
    with pytest.raises(ValidationError):
        ConsoleSettings(bind=bind)


def test_settings_accept_the_loopback_bind_and_default_to_it() -> None:
    assert ConsoleSettings().bind == "127.0.0.1"
    assert ConsoleSettings(bind=" 127.0.0.1 ").bind == "127.0.0.1"


@pytest.mark.parametrize("port", [0, 1, 1023, 65536, -1])
def test_settings_refuse_bad_ports(port: int) -> None:
    with pytest.raises(ValidationError):
        ConsoleSettings(port=port)


def test_bound_socket_is_loopback_only_and_not_reachable_via_other_addresses() -> None:
    sock = bind_console_socket(0)
    try:
        host, port = sock.getsockname()
        assert host == "127.0.0.1"
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            pass
        # any non-loopback local address of this machine must be refused
        lan = _lan_ip()
        if lan is not None:
            with pytest.raises(OSError):
                socket.create_connection((lan, port), timeout=2).close()
    finally:
        sock.close()


def _lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = str(s.getsockname()[0])
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") else ip


def test_port_in_use_is_a_config_error_naming_the_port() -> None:
    first = bind_console_socket(0)
    try:
        port = first.getsockname()[1]
        # SO_REUSEADDR on Linux does not allow a second *listening* bind
        with pytest.raises(ConfigError) as exc:
            bind_console_socket(port)
        assert str(port) in str(exc.value)
    finally:
        first.close()


# --------------------------------------------------------------- host / origin
@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("127.0.0.1:47130", True),
        (" 127.0.0.1:47130 ", True),
        (None, False),
        ("", False),
        ("localhost:47130", False),
        ("127.0.0.1", False),
        ("127.0.0.1:47131", False),
        ("[::1]:47130", False),
        ("127.0.0.1:47130.evil.example", False),
        ("evil.example:47130@127.0.0.1:47130", False),
        ("127.0.0.1:47130, evil.example", False),
        ("0x7f.0.0.1:47130", False),
        ("2130706433:47130", False),
        ("127.0.0.1:047130", False),
    ],
)
def test_host_header_rule(host: str | None, ok: bool) -> None:
    assert host_ok(host, 47130) is ok


@pytest.mark.parametrize(
    ("origin", "ok"),
    [
        ("http://127.0.0.1:47130", True),
        ("http://127.0.0.1:47130/", False),
        ("https://127.0.0.1:47130", False),
        ("http://localhost:47130", False),
        ("null", False),
        (None, False),
        ("", False),
        ("http://127.0.0.1:47130.evil.example", False),
    ],
)
def test_origin_header_rule(origin: str | None, ok: bool) -> None:
    assert origin_ok(origin, 47130) is ok


# ------------------------------------------------------------------- tokens
def test_token_is_long_random_and_unique() -> None:
    tokens = {_auth().token for _ in range(50)}
    assert len(tokens) == 50
    assert all(len(t) >= 40 for t in tokens)


@pytest.mark.parametrize(
    "candidate", [None, "", " ", "x", "ünïcödé", "\x00", "a" * 100_000]
)
def test_wrong_tokens_never_match_and_never_raise(candidate: str | None) -> None:
    a = _auth(token="t" * 43)
    assert a.token_matches(candidate) is False


def test_right_token_matches_even_when_throttled() -> None:
    a = _auth(token="t" * 43)
    for _ in range(auth.MAX_FAILURES_PER_MINUTE + 5):
        a.throttled()  # the app checks the throttle before every credential check
        a.token_matches("nope")
    assert a.throttled()
    assert a.token_matches("t" * 43)  # the owner is never locked out by a stranger


def test_throttle_window_resets_after_a_minute() -> None:
    clock = Clock()
    a = _auth(clock)
    for _ in range(auth.MAX_FAILURES_PER_MINUTE):
        a.throttled()
        a.token_matches("nope")
    assert a.throttled()
    clock.t += 61
    assert not a.throttled()


def test_launch_code_is_single_use_and_expires() -> None:
    clock = Clock()
    a = _auth(clock)
    code = a.new_launch_code()
    assert a.redeem_launch_code(code) is True
    assert a.redeem_launch_code(code) is False
    code2 = a.new_launch_code()
    clock.t += 10.0
    assert a.redeem_launch_code(code2) is False  # expired exactly at the ttl


def test_launch_code_is_not_the_token_and_token_is_not_a_launch_code() -> None:
    a = _auth()
    code = a.new_launch_code()
    assert code != a.token
    assert a.redeem_launch_code(a.token) is False
    assert a.token_matches(code) is False


def test_launch_codes_and_sessions_are_bounded() -> None:
    a = _auth()
    codes = [a.new_launch_code() for _ in range(auth.MAX_LAUNCH_CODES + 10)]
    assert a.redeem_launch_code(codes[0]) is False  # evicted, not unbounded growth
    assert a.redeem_launch_code(codes[-1]) is True
    sessions = [a.new_session() for _ in range(auth.MAX_SESSIONS + 5)]
    assert a.session_ok(sessions[0]) is False
    assert a.session_ok(sessions[-1]) is True


def test_session_expiry_and_sign_out() -> None:
    clock = Clock()
    a = _auth(clock)
    s = a.new_session()
    assert a.session_ok(s)
    a.end_session(s)
    assert not a.session_ok(s)
    s2 = a.new_session()
    clock.t += 100
    assert not a.session_ok(s2)
    a.end_session(None)
    a.end_session("unknown")


# ------------------------------------------------------------- token file
def test_token_file_content_and_unpublish(tmp_path: Path) -> None:
    a = _auth()
    path = a.publish(tmp_path / "console")
    assert path.read_text(encoding="ascii").strip() == a.token
    a.unpublish()
    assert not path.exists()
    a.unpublish()  # idempotent


@pytest.mark.skipif(not hasattr(__import__("os"), "geteuid"), reason="POSIX modes")
def test_token_file_is_not_world_readable_in_a_fresh_folder(tmp_path: Path) -> None:
    path = _auth().publish(tmp_path / "console")
    assert path.stat().st_mode & 0o077 == 0
    assert (tmp_path / "console").stat().st_mode & 0o077 == 0


def test_token_is_rewritten_atomically_on_every_start(tmp_path: Path) -> None:
    first = _auth().publish(tmp_path)
    old = first.read_text()
    second_auth = _auth()
    second_auth.publish(tmp_path)
    assert first.read_text() != old
    assert first.read_text().strip() == second_auth.token
    assert [p.name for p in tmp_path.iterdir()] == ["token"]  # no stray temp files


@pytest.mark.parametrize(
    ("env", "inherits"),
    [
        ({"MESHFORGE_INSTALL_MODE": "system"}, True),
        ({"MESHFORGE_INSTALL_MODE": "user"}, False),
        ({}, False),
    ],
)
def test_token_acl_is_inherited_only_in_system_mode(
    env: dict[str, str], inherits: bool
) -> None:
    """System mode: the folder ACL (installing user may read) is kept; a developer's
    token gets a private ACL (current user only)."""
    assert windows_folder_acl(env) is inherits


def test_write_token_file_unwritable_folder_raises_oserror(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(OSError):
        _auth().publish(blocker / "sub")
