"""V0.17 G4: the console's Windows checks say what is true and how to fix it.

The Windows branches of ``console.checks`` (D388/D389, Q31) are decided by pure
parsers and verdicts in ``console.checks_windows``; here they are fed the
output PowerShell / ``net`` print, on any OS. Standing rule: a check never says
"ok" for something it did not measure, and the firewall fix is never "widen the
rule to Public".
"""

from __future__ import annotations

import pytest

from meshforge.agent.console import checks, checks_windows
from meshforge.agent.console.auth import windows_folder_acl
from meshforge.agent.console.checks import CheckStatus
from meshforge.agent.hardware.probe import CmdResult
from meshforge.common.windows_service import service_sid

pytestmark = pytest.mark.unit

PY = r"C:\Program Files\MeshForge\0.17.0-1\python\python.exe"


def _result(stdout: str, code: int | None = 0, error: str | None = None) -> CmdResult:
    return CmdResult(("x",), code, stdout, "", error)


def _ps(*lines: str, done: bool = True) -> str:
    return "\r\n".join([*lines, *(["done"] if done else [])]) + "\r\n"


RULE_OK = "rule\tTrue\tDomain, Private\tInbound\tAllow\t47120"
HOME = "network\tWi-Fi\tPrivate"


# ------------------------------------------------------------------ parsing
def test_parse_firewall_reads_rules_networks_and_completion() -> None:
    facts = checks_windows.parse_firewall(
        _ps(RULE_OK, HOME, "network\tEthernet 2\tDomainAuthenticated", "junk\tline")
    )
    assert facts.complete
    (rule,) = facts.rules
    assert rule.enabled and rule.inbound and rule.allow
    assert rule.profiles == frozenset({"domain", "private"})
    assert rule.ports == frozenset({"47120"})
    assert facts.networks == [
        ("Wi-Fi", "Private"),
        ("Ethernet 2", "DomainAuthenticated"),
    ]


def test_a_truncated_powershell_output_is_not_complete() -> None:
    assert not checks_windows.parse_firewall(_ps(RULE_OK, done=False)).complete


def test_firewall_facts_none_when_powershell_fails_or_stops_early() -> None:
    calls: list[tuple[str, list[str]]] = []

    def failing(name: str, args: list[str]) -> CmdResult:
        calls.append((name, args))
        return _result("", None, "timed out")

    assert checks_windows.windows_firewall_facts(failing) is None
    name, args = calls[0]
    assert name == "powershell"
    assert args[-1] == checks_windows.FIREWALL_PS
    assert "-NonInteractive" in args
    assert (
        checks_windows.windows_firewall_facts(lambda n, a: _result(_ps(done=False)))
        is None
    )
    full = checks_windows.windows_firewall_facts(lambda n, a: _result(_ps(HOME)))
    assert full is not None and full.networks == [("Wi-Fi", "Private")]


# ------------------------------------------------------------------ verdicts
def _verdict(
    text: str | None, *, peer_on: bool = True, port: int = 47120
) -> checks_windows.Verdict:
    facts = None if text is None else checks_windows.parse_firewall(text)
    return checks_windows.firewall_verdict(
        facts, peer_on=peer_on, port=port, python_exe=PY
    )


def test_unchecked_firewall_is_a_warning_never_ok() -> None:
    verdict = _verdict(None)
    assert verdict.status == "warn"
    assert "could not be checked" in verdict.why


def test_peer_off_needs_no_rule() -> None:
    verdict = _verdict(_ps("network\tWi-Fi\tPublic"), peer_on=False)
    assert verdict.status == "ok"
    assert "no rule is needed" in verdict.why


def test_peer_on_with_the_rule_on_private_networks_is_ok() -> None:
    verdict = _verdict(_ps(RULE_OK, HOME))
    assert verdict.status == "ok"
    assert "47120" in verdict.why


def test_missing_or_disabled_rule_names_the_exact_elevated_fix() -> None:
    for text in (
        _ps(HOME),
        _ps("rule\tFalse\tDomain, Private\tInbound\tAllow\t47120", HOME),
    ):
        verdict = _verdict(text)
        assert verdict.status == "warn"
        assert "missing or disabled" in verdict.why
        assert f'"{PY}" -I -m meshforge.agent.winservice firewall-sync' in verdict.fix


@pytest.mark.parametrize("profile", ["Any", "Domain, Private, Public"])
def test_a_rule_open_on_public_networks_is_flagged(profile: str) -> None:
    verdict = _verdict(_ps(f"rule\tTrue\t{profile}\tInbound\tAllow\t47120", HOME))
    assert verdict.status == "warn"
    assert "also allows Public" in verdict.why
    assert "Private and Domain only" in verdict.fix


def test_a_public_network_is_named_and_fixed_in_settings_never_by_the_rule() -> None:
    verdict = _verdict(_ps(RULE_OK, "network\tCafe Wi-Fi\tPublic"))
    assert verdict.status == "warn"
    assert "Cafe Wi-Fi" in verdict.why
    assert "Network profile type > Private" in verdict.fix
    assert "firewall-sync" not in verdict.fix
    assert "public" not in verdict.fix.lower().replace("network profile", "")


def test_a_rule_for_another_port_is_flagged() -> None:
    verdict = _verdict(_ps(RULE_OK, HOME), port=47999)
    assert verdict.status == "warn"
    assert "another port" in verdict.why


# --------------------------------------------------------------- docker-users
def test_in_docker_users_matches_the_account_or_its_sid() -> None:
    listing = (
        "Alias name     docker-users\r\nMembers\r\n\r\n------------\r\n"
        "asha\r\nNT SERVICE\\MeshForgeAgent\r\nThe command completed successfully.\r\n"
    )
    assert checks_windows.in_docker_users(listing)
    assert checks_windows.in_docker_users(f"{service_sid()}\r\n")
    assert not checks_windows.in_docker_users("asha\r\nNT SERVICE\\Other\r\n")


def test_service_in_docker_users_is_none_without_the_group() -> None:
    def no_group(name: str, args: list[str]) -> CmdResult:
        assert (name, args) == ("net", ["localgroup", "docker-users"])
        return _result("System error 1376", 2)

    assert checks_windows.service_in_docker_users(no_group) is None
    assert (
        checks_windows.service_in_docker_users(
            lambda n, a: _result("NT SERVICE\\MeshForgeAgent\r\n")
        )
        is True
    )


# ------------------------------------------------------------ docker check
def test_windows_docker_working_is_ok_with_the_version() -> None:
    check = checks._check_docker_windows(None, "27.3.1")
    assert check.status is CheckStatus.OK
    assert "27.3.1" in check.why


def test_windows_docker_without_access_offers_the_uac_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks_windows, "service_in_docker_users", lambda: False)
    check = checks._check_docker_windows("docker_permission_denied", None)
    assert check.status is CheckStatus.WARN
    assert check.action == checks.DOCKER_ACCESS_ACTION
    assert "give Docker access" in check.fix
    assert checks.WINDOWS_DOCKER_ACCESS_COMMAND in check.fix
    assert "full admin rights" in check.why


def test_windows_docker_access_given_but_not_restarted_says_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checks_windows, "service_in_docker_users", lambda: True)
    check = checks._check_docker_windows("docker_permission_denied", None)
    assert check.status is CheckStatus.WARN
    assert "has not restarted" in check.why
    assert check.action is None


def test_windows_docker_desktop_not_running_says_so() -> None:
    check = checks._check_docker_windows("docker_daemon_unreachable", None)
    assert check.status is CheckStatus.WARN
    assert "not running" in check.why
    assert "Start Docker Desktop" in check.fix


def test_windows_docker_command_matches_the_admin_command() -> None:
    from meshforge.agent.wininstall import docker_users_argv

    argv = docker_users_argv(add=True)
    assert f'"{argv[3]}"' in checks.WINDOWS_DOCKER_ACCESS_COMMAND
    assert "Restart-Service MeshForgeAgent" in checks.WINDOWS_DOCKER_ACCESS_COMMAND


def test_docker_npipe_access_denied_is_a_permission_problem() -> None:
    from meshforge.agent.execution.docker_support import _unreachable_reason

    exc = OSError("(5, 'CreateFile', 'Access is denied.')")
    assert _unreachable_reason(exc) == "docker_permission_denied"
    assert _unreachable_reason(OSError("pipe not found")) == (
        "docker_daemon_unreachable"
    )


# ------------------------------------------------------------- console token
def test_the_token_keeps_the_folder_acl_only_on_the_installed_service() -> None:
    assert windows_folder_acl({"MESHFORGE_INSTALL_MODE": "system"}) is True
    assert windows_folder_acl({"MESHFORGE_INSTALL_MODE": "user"}) is False
    assert windows_folder_acl({}) is False
