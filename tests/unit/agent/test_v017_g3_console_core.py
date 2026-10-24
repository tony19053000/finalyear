"""V0.17 G3: the console's building blocks, alone.

* settings: ``[agent.console] bind`` must be 127.0.0.1 (the console is never on
  the LAN); on by default only for the system service; the token folder;
* token file modes (``0640`` in a set-group-id folder, else ``0600``) and the
  credentials table (single-use codes, expiry, session cap, throttling);
* Host/Origin predicates; join errors in plain words for every reason;
* the node-name file; the setup marker; the log tail's redaction;
* ``policy.apply_changes``: validates first, merges or replaces, says which
  keys need a restart; ``policy_view`` = ``policy show --json``;
* ``Hello.display_name``; the controller's admission limit for it.
"""

from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

import pytest

from meshforge.agent.console.auth import (
    MAX_SESSIONS,
    ConsoleAuth,
    host_ok,
    origin_ok,
    write_token_file,
)
from meshforge.agent.console.joining import explain
from meshforge.agent.console.logbuffer import LogBuffer
from meshforge.agent.console.node_name import (
    clear_node_name,
    effective_node_name,
    node_name_path,
    read_node_name,
    write_node_name,
)
from meshforge.agent.console.setup_state import (
    clear_setup,
    mark_setup_complete,
    setup_complete,
)
from meshforge.agent.enrollment import JoinError, JoinReason
from meshforge.agent.policy import apply_changes, policy_path, policy_view
from meshforge.agent.session.hello import build_hello
from meshforge.agent.settings import (
    SYSTEM_CONSOLE_TOKEN_DIR,
    AgentSection,
    AgentSettings,
    ConsoleSettings,
    check_node_name,
)
from meshforge.common.config import INSTALL_MODE_ENV_VAR
from meshforge.common.errors import ConfigError

pytestmark = [pytest.mark.unit]
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")


def _settings(tmp_path: Path, **agent: object) -> AgentSettings:
    return AgentSettings(
        data_dir=tmp_path / "data",
        agent=AgentSection(controller_address="127.0.0.1:47100", **agent),  # type: ignore[arg-type]
    )


# ------------------------------------------------------------------ settings


@pytest.mark.parametrize(
    "bind",
    [
        "0.0.0.0",  # noqa: S104 - the point of the test
        "192.168.1.5",
        "localhost",
        "::1",
        "127.0.0.2",
        "",
    ],
)
def test_console_binds_loopback_v4_only(bind: str) -> None:
    with pytest.raises(ValueError, match=r"127\.0\.0\.1"):
        ConsoleSettings(bind=bind)


def test_console_bind_default_and_port_range() -> None:
    assert ConsoleSettings().bind == "127.0.0.1"
    assert ConsoleSettings(bind=" 127.0.0.1 ").bind == "127.0.0.1"
    with pytest.raises(ValueError):
        ConsoleSettings(port=80)


def test_console_on_for_the_system_service_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(INSTALL_MODE_ENV_VAR, raising=False)
    settings = _settings(tmp_path)
    assert settings.console_enabled is False
    assert settings.console_token_dir == settings.data_dir / "console"
    monkeypatch.setenv(INSTALL_MODE_ENV_VAR, "system")
    assert settings.console_enabled is True
    if sys.platform == "linux":
        assert settings.console_token_dir == SYSTEM_CONSOLE_TOKEN_DIR
    off = _settings(tmp_path, console={"enabled": False, "token_dir": tmp_path / "t"})
    assert off.console_enabled is False and off.console_token_dir == tmp_path / "t"


def test_node_name_rule() -> None:
    assert check_node_name("  Lab A ") == "Lab A"
    assert check_node_name("") == ""
    assert check_node_name("Ünïcödé 名前") == "Ünïcödé 名前"
    for bad in ("a\tb", "a\nb", "\x1b[0m", "x" * 65):
        with pytest.raises(ValueError):
            check_node_name(bad)
    with pytest.raises(ValueError):
        AgentSection(node_name="bad\x00name")


# --------------------------------------------------------------------- auth


@posix_only
def test_token_file_modes(tmp_path: Path) -> None:
    private = write_token_file(tmp_path / "dev", "tok")
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert stat.S_IMODE(private.parent.stat().st_mode) == 0o700
    shared_dir = tmp_path / "shared"
    shared_dir.mkdir()
    shared_dir.chmod(0o2750)
    old = os.umask(0o077)
    try:
        shared = write_token_file(shared_dir, "tok2")
    finally:
        os.umask(old)
    assert stat.S_IMODE(shared.stat().st_mode) == 0o640
    assert shared.read_text() == "tok2\n"


def test_host_and_origin_predicates() -> None:
    assert host_ok("127.0.0.1:47130", 47130)
    assert host_ok(" 127.0.0.1:47130 ", 47130)
    for bad in (
        None,
        "",
        "localhost:47130",
        "127.0.0.1",
        "127.0.0.1:47131",
        "evil:47130",
    ):
        assert not host_ok(bad, 47130), bad
    assert origin_ok("http://127.0.0.1:47130", 47130)
    for bad in (None, "null", "https://127.0.0.1:47130", "http://localhost:47130"):
        assert not origin_ok(bad, 47130), bad


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def test_credentials_table() -> None:
    clock = Clock()
    auth = ConsoleAuth(port=1, session_ttl_s=100, launch_code_ttl_s=10, clock=clock)
    other = ConsoleAuth(port=1, session_ttl_s=100, launch_code_ttl_s=10)
    assert auth.token != other.token and len(auth.token) >= 40  # rotates per start
    code = auth.new_launch_code()
    assert auth.redeem_launch_code(code) and not auth.redeem_launch_code(code)
    late = auth.new_launch_code()
    clock.now = 11
    assert not auth.redeem_launch_code(late)
    sessions = [auth.new_session() for _ in range(MAX_SESSIONS + 1)]
    assert not auth.session_ok(sessions[0])  # the oldest browser signed out
    assert auth.session_ok(sessions[-1])
    clock.now = 200
    assert not auth.session_ok(sessions[-1])
    assert not auth.token_matches("") and not auth.token_matches(None)
    assert auth.token_matches(auth.token)


# ------------------------------------------------------------------ joining


@pytest.mark.parametrize(
    ("reason", "controller", "code"),
    [
        (JoinReason.BAD_CODE, None, "bad_code"),
        (JoinReason.ALREADY_ENROLLED, None, "already_joined"),
        (JoinReason.UNREACHABLE, None, "unreachable"),
        (JoinReason.TLS_ERROR, None, "not_a_controller"),
        (JoinReason.PIN_MISMATCH, None, "ca_mismatch"),
        (JoinReason.STORAGE, None, "storage"),
        (JoinReason.REFUSED, "token_expired", "expired"),
        (JoinReason.REFUSED, "token_used", "used"),
        (JoinReason.REFUSED, "token_revoked", "revoked_code"),
        (JoinReason.REFUSED, "token_unknown", "bad_code"),
        (JoinReason.REFUSED, "node_revoked", "node_revoked"),
        (JoinReason.REFUSED, "rate_limited", "busy"),
        (JoinReason.REFUSED, "csr_bad_signature", "refused"),
        (JoinReason.BAD_RESPONSE, None, "refused"),
    ],
)
def test_join_errors_in_plain_words(
    reason: JoinReason, controller: str | None, code: str
) -> None:
    problem = explain(
        JoinError(reason, "technical text", controller_reason=controller),
        address="192.168.1.5",
    )
    assert problem.code == code
    assert problem.message and problem.fix
    assert "technical text" not in problem.message
    assert problem.message[0].isupper() and problem.message.endswith(".")


# ------------------------------------------------------------- name, setup


def test_node_name_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path, node_name="From config")
    assert effective_node_name(settings) == "From config"
    assert write_node_name(settings, " Lab A ") == "Lab A"
    assert read_node_name(node_name_path(settings)) == "Lab A"
    assert effective_node_name(settings) == "Lab A"
    write_node_name(settings, "")  # cleared on purpose: no name, not the config's
    assert effective_node_name(settings) == ""
    assert clear_node_name(settings) and effective_node_name(settings) == "From config"
    with pytest.raises(ValueError):
        write_node_name(settings, "a\x07b")
    if sys.platform != "win32":
        path = node_name_path(settings)
        path.write_text("Lab B")
        path.chmod(0o644)  # not private -> refused, the Hello falls back
        with pytest.raises(ConfigError):
            read_node_name(path)
        assert effective_node_name(settings) == "From config"


def test_setup_marker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    assert not setup_complete(settings)
    mark_setup_complete(settings)
    assert setup_complete(settings)
    clear_setup(settings)
    clear_setup(settings)
    assert not setup_complete(settings)


def test_log_buffer_redacts_and_caps() -> None:
    buffer = LogBuffer(3)
    log = logging.getLogger("test.g3.buffer")
    log.addHandler(buffer)
    log.setLevel(logging.INFO)
    try:
        log.info("Authorization: Bearer abc.def.ghi")
        log.info("ok", extra={"api_key": "k-123", "count": 2})
        for i in range(3):
            log.info("n%d", i)
    finally:
        log.removeHandler(buffer)
    lines = buffer.tail(10)
    assert [line.event for line in lines] == ["n0", "n1", "n2"]
    assert [line.seq for line in lines] == [3, 4, 5]
    everything = LogBuffer(10)
    log.addHandler(everything)
    try:
        log.info("Authorization: Bearer abc.def.ghi")
        log.info("ok", extra={"api_key": "k-123", "count": 2})
    finally:
        log.removeHandler(everything)
    text = repr([line.as_dict() for line in everything.tail(10)])
    assert "abc.def.ghi" not in text and "k-123" not in text
    assert everything.tail(10)[1].fields["count"] == 2
    assert everything.tail(10, after=1)[0].event == "ok"


# ------------------------------------------------------------------ policy


def test_apply_changes_validates_first_and_merges(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = policy_path(settings)
    result = apply_changes(settings, {"accept_jobs": True, "max_cpu_percent": 50})
    assert result.restart_keys == ("accept_jobs",)
    assert result.effective.max_cpu_percent == 50
    again = apply_changes(settings, {"accept_jobs": True})
    assert again.restart_keys == ()  # was already on
    before = path.read_bytes()
    for bad in ({"max_cpu_percent": 0}, {"bogus": 1}, {"max_cpu_jobs": 999}):
        with pytest.raises(ConfigError):
            apply_changes(settings, bad)
    assert path.read_bytes() == before  # nothing written
    view = policy_view(settings)
    assert view["policy"]["accept_jobs"] is True
    assert view["live_keys"] == ["accept_jobs", "max_cpu_percent"]


def test_apply_changes_replaces_a_broken_file(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = policy_path(settings)
    path.parent.mkdir(parents=True)
    path.write_text("this is = = not toml")
    if sys.platform != "win32":
        path.chmod(0o600)
    assert policy_view(settings)["invalid"]
    with pytest.raises(ConfigError):
        apply_changes(settings, {"accept_jobs": False})  # merge cannot read it
    result = apply_changes(settings, {"accept_jobs": False}, replace=True)
    assert result.restart_keys == ()
    assert policy_view(settings)["invalid"] is None


# -------------------------------------------------------------- display name


def test_hello_carries_the_display_name() -> None:
    hello = build_hello(
        node_id="node_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        session_epoch=1,
        agent_version="0.17.0",
        facts=("lab-a", "Ubuntu", "linux-x86_64"),
        display_name="Lab A",
    )
    assert hello.display_name == "Lab A" and hello.hostname == "lab-a"
    plain = build_hello(
        node_id="node_01ARZ3NDEKTSV4RRFFQ69G5FAV",
        session_epoch=1,
        agent_version="0.17.0",
        facts=("lab-a", "Ubuntu", "linux-x86_64"),
    )
    assert plain.display_name == ""


def test_controller_refuses_a_bad_display_name() -> None:
    pytest.importorskip("sqlalchemy", reason="controller extra")
    from meshforge.controller.session.limits import check_hello_strings

    ok = {"hostname": "h", "os": "o", "platform": "p", "agent_version": "1"}
    assert check_hello_strings(**ok, display_name="Lab A") is None
    assert check_hello_strings(**ok) is None
    too_long = check_hello_strings(**ok, display_name="x" * 65)
    assert (
        too_long is not None and "display_name" in too_long and "x" * 10 not in too_long
    )
    control = check_hello_strings(**ok, display_name="a\x1bb")
    assert control is not None and "U+001B" in control
