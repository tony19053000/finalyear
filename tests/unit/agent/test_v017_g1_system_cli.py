"""V0.17 G1 (Q15): a human's ``meshforge-agent`` acts on the service's state.

Never a second identity in ``~`` on a laptop with the package: the packaged
CLI re-executes itself as ``meshforge`` via sudo; a dev checkout only warns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from meshforge.agent import system_cli
from meshforge.agent.system_cli import Decision, decide, system_install_present
from meshforge.common import config

pytestmark = pytest.mark.unit


def _decide(args: list[str], **over: object) -> Decision:
    kw: dict[str, object] = {
        "environ": {},
        "platform": "linux",
        "user": "asha",
        "installed": True,
        "bundled": True,
    }
    kw.update(over)
    return decide(args, **kw)  # type: ignore[arg-type]


def test_constants_match_common_config() -> None:
    assert system_cli.INSTALL_MODE_ENV_VAR == config.INSTALL_MODE_ENV_VAR
    assert Path(config.SYSTEM_LINUX_CONFIG_DIR) == system_cli.SYSTEM_LINUX_CONFIG_DIR
    assert Path(config.SYSTEM_LINUX_DATA_DIR) == system_cli.SYSTEM_LINUX_DATA_DIR


@pytest.mark.parametrize(
    "args",
    [
        ["identity", "show"],
        ["join", "-"],
        ["policy", "set", "accept_jobs=true"],
        ["hardware", "score"],
        ["run"],
    ],
)
def test_packaged_cli_reexecutes_as_the_service_user(args: list[str]) -> None:
    decision = _decide(args)
    assert decision.action == "reexec"
    assert decision.argv == (
        "sudo",
        "-u",
        "meshforge",
        "--",
        "/usr/bin/env",
        "MESHFORGE_INSTALL_MODE=system",
        "/opt/meshforge/current/bin/meshforge-agent",
        *args,
    )
    assert "via sudo" in decision.message


@pytest.mark.parametrize(
    "args",
    [[], ["--version"], ["--help"], ["identity", "--help"], ["migrate-identity"]],
)
def test_harmless_commands_run_in_place(args: list[str]) -> None:
    assert _decide(args).action == "none"


@pytest.mark.parametrize(
    "over",
    [
        {"platform": "win32"},
        {"installed": False},
        {"user": "meshforge"},
        {"environ": {"MESHFORGE_INSTALL_MODE": "system"}},  # the unit itself
        {"environ": {"MESHFORGE_INSTALL_MODE": "user"}},  # explicit opt-out
    ],
)
def test_no_redirect_when_it_does_not_apply(over: dict[str, object]) -> None:
    assert _decide(["identity", "show"], **over).action == "none"


def test_dev_checkout_only_warns() -> None:
    decision = _decide(["identity", "show"], bundled=False)
    assert decision.action == "warn"
    assert "/var/lib/meshforge" in decision.message
    assert "MESHFORGE_INSTALL_MODE=user" in decision.message


def test_install_detection_needs_both_dirs(tmp_path: Path) -> None:
    etc, opt = tmp_path / "etc", tmp_path / "opt"
    assert not system_install_present(etc, opt)
    etc.mkdir()
    assert not system_install_present(etc, opt)  # e.g. after `apt remove`
    (opt / "current").mkdir(parents=True)
    assert system_install_present(etc, opt)


def _force(monkeypatch: pytest.MonkeyPatch, *, bundled: bool) -> None:
    monkeypatch.setattr("meshforge.agent.system_cli.sys.platform", "linux")
    monkeypatch.setattr(system_cli, "system_install_present", lambda: True)
    monkeypatch.setattr(system_cli, "_current_user", lambda: "asha")
    monkeypatch.setattr(
        system_cli, "bundle_root", lambda: Path("/opt/x") if bundled else None
    )
    monkeypatch.delenv("MESHFORGE_INSTALL_MODE", raising=False)


def test_apply_execs_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    _force(monkeypatch, bundled=True)
    calls: list[tuple[str, list[str]]] = []
    said: list[str] = []
    system_cli.apply(
        ["identity", "show"],
        execvp=lambda f, a: calls.append((f, a)),
        which=lambda _n: "/usr/bin/sudo",
        stderr=said.append,
    )
    assert calls and calls[0][0] == "sudo"
    assert calls[0][1][-2:] == ["identity", "show"]
    assert said and "via sudo" in said[0]


def test_apply_without_sudo_explains_and_exits_4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force(monkeypatch, bundled=True)
    said: list[str] = []
    with pytest.raises(SystemExit) as info:
        system_cli.apply(
            ["identity", "show"],
            execvp=lambda f, a: pytest.fail("must not exec"),
            which=lambda _n: None,
            stderr=said.append,
        )
    assert info.value.code == 4
    assert "meshforge" in said[0] and "su -s /bin/sh" in said[0]


def test_apply_returns_on_the_common_case(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(system_cli, "system_install_present", lambda: False)
    system_cli.apply(
        ["identity", "show"],
        execvp=lambda f, a: pytest.fail("must not exec"),
        stderr=lambda _t: pytest.fail("must not print"),
    )
