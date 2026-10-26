"""The Windows branches of the console checks (V0.17 G4, D388/D389, Q31).

Measured **by the service itself** (``NT SERVICE\\MeshForgeAgent`` in session
0), like every check: Docker Desktop's pipe as the service sees it, the NVIDIA
driver from session 0, the firewall rule and the network profile.

The parsers and decisions are pure (unit-tested on any OS); the commands are
fixed argument lists run through the agent's bounded
:func:`~meshforge.agent.hardware.probe.run_command`:

* ``net localgroup docker-users`` - is the service account a member (it only
  gets the group when the *service* restarts: a console "restart" is in-process
  and does not refresh the token);
* one PowerShell call for the firewall: the ``MeshForge-Agent-Peer`` rule
  (enabled, profiles, port) and each connected network's category. PowerShell
  prints the enum *names* (``Public``, ``Private``, ``DomainAuthenticated``),
  which are not localised.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Final

from meshforge.agent.hardware.probe import CmdResult, run_command
from meshforge.common.windows_service import SERVICE_ACCOUNT

__all__ = [
    "DOCKER_ACCESS_MENU",
    "FIREWALL_PS",
    "WindowsFirewall",
    "firewall_sync_command",
    "firewall_verdict",
    "in_docker_users",
    "parse_firewall",
    "service_in_docker_users",
    "windows_firewall_facts",
]

PEER_RULE: Final = "MeshForge-Agent-Peer"
DOCKER_ACCESS_MENU: Final = "Start menu -> MeshForge -> MeshForge - give Docker access"
FIREWALL_PS: Final = (
    "$ErrorActionPreference = 'SilentlyContinue'; "
    f"Get-NetFirewallRule -DisplayName '{PEER_RULE}' | ForEach-Object {{ "
    "$port = ($_ | Get-NetFirewallPortFilter).LocalPort -join ','; "
    '"rule`t$($_.Enabled)`t$($_.Profile)`t$($_.Direction)`t$($_.Action)`t$port" }; '
    "Get-NetConnectionProfile | ForEach-Object { "
    '"network`t$($_.InterfaceAlias)`t$($_.NetworkCategory)" }; '
    '"done"'
)
"""The firewall facts in one call; ``done`` proves the script ran to the end."""


def firewall_sync_command(python_exe: str) -> str:
    """The exact elevated command that puts the rule right (this bundle's python)."""
    return f'"{python_exe}" -I -m meshforge.agent.winservice firewall-sync'


def in_docker_users(net_output: str) -> bool:
    """``net localgroup docker-users`` lists the service account (or its SID)."""
    from meshforge.common.windows_service import service_sid

    wanted = (SERVICE_ACCOUNT.lower(), service_sid().lower())
    return any(line.strip().lower() in wanted for line in net_output.splitlines())


def service_in_docker_users(
    run: Callable[[str, list[str]], CmdResult] | None = None,
) -> bool | None:
    """Is the service account in ``docker-users``? ``None``: no such group (no
    Docker Desktop) or ``net`` failed."""
    runner = run or (lambda name, args: run_command(name, args, timeout_s=5.0))
    result = runner("net", ["localgroup", "docker-users"])
    if not result.ok:
        return None
    return in_docker_users(result.stdout)


@dataclass(frozen=True, slots=True)
class FirewallRuleFacts:
    enabled: bool
    profiles: frozenset[str]
    """Lower-case: ``private``, ``domain``, ``public``, or ``any``."""
    inbound: bool
    allow: bool
    ports: frozenset[str]


@dataclass(slots=True)
class WindowsFirewall:
    complete: bool = False
    rules: list[FirewallRuleFacts] = field(default_factory=list)
    networks: list[tuple[str, str]] = field(default_factory=list)
    """``(interface alias, category)`` per connected network."""


def parse_firewall(text: str) -> WindowsFirewall:
    """:data:`FIREWALL_PS` output -> facts (unknown lines ignored)."""
    facts = WindowsFirewall()
    for raw in text.splitlines():
        parts = raw.rstrip("\r").split("\t")
        kind = parts[0].strip()
        if kind == "done":
            facts.complete = True
        elif kind == "rule" and len(parts) >= 6:
            profiles = frozenset(
                p.strip().lower() for p in parts[2].split(",") if p.strip()
            )
            facts.rules.append(
                FirewallRuleFacts(
                    enabled=parts[1].strip().lower() == "true",
                    profiles=profiles,
                    inbound=parts[3].strip().lower() == "inbound",
                    allow=parts[4].strip().lower() == "allow",
                    ports=frozenset(
                        p.strip() for p in parts[5].split(",") if p.strip()
                    ),
                )
            )
        elif kind == "network" and len(parts) >= 3:
            facts.networks.append((parts[1].strip(), parts[2].strip()))
    return facts


def windows_firewall_facts(
    run: Callable[[str, list[str]], CmdResult] | None = None,
) -> WindowsFirewall | None:
    """Run :data:`FIREWALL_PS`; ``None`` when PowerShell did not finish."""
    runner = run or (lambda name, args: run_command(name, args, timeout_s=6.0))
    result = runner(
        "powershell",
        [
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            FIREWALL_PS,
        ],
    )
    if not result.ok:
        return None
    facts = parse_firewall(result.stdout)
    return facts if facts.complete else None


@dataclass(frozen=True, slots=True)
class Verdict:
    status: str
    """``ok`` | ``warn``."""
    why: str
    fix: str = ""


def _public(networks: Sequence[tuple[str, str]]) -> list[str]:
    return [alias for alias, category in networks if category.lower() == "public"]


def firewall_verdict(
    facts: WindowsFirewall | None, *, peer_on: bool, port: int, python_exe: str
) -> Verdict:
    """D389 on Windows: the rule exists exactly when peer serving is on, only for
    Private + Domain, and a Public network is named with the way to change it
    (never by widening the rule)."""
    if facts is None:
        return Verdict(
            "warn",
            "The Windows firewall could not be checked (PowerShell did not answer).",
        )
    public = _public(facts.networks)
    if not peer_on:
        why = (
            "MeshForge only makes outgoing connections (peer file sharing is off), "
            "which the Windows firewall allows; no rule is needed."
        )
        return Verdict("ok", why)
    matching = [r for r in facts.rules if r.enabled and r.inbound and r.allow]
    if not matching:
        return Verdict(
            "warn",
            f"Peer file sharing is on, but the firewall rule {PEER_RULE} is missing "
            f"or disabled, so other laptops cannot fetch files from this one (port "
            f"{port}).",
            "In an administrator prompt run "
            f"{firewall_sync_command(python_exe)} (it adds the rule for Private and "
            "Domain networks only).",
        )
    if any(r.profiles & {"public", "any"} for r in matching):
        return Verdict(
            "warn",
            f"The firewall rule {PEER_RULE} also allows Public networks; MeshForge "
            "never opens a port on Public networks.",
            f"In an administrator prompt run {firewall_sync_command(python_exe)} "
            "(Private and Domain only).",
        )
    if public:
        return Verdict(
            "warn",
            f"The network {', '.join(public)} is set to Public, so Windows blocks "
            "other laptops from reaching this one there (MeshForge's rule is for "
            "Private and Domain networks only).",
            "If this is your home or team network: Windows Settings > Network & "
            "internet > (your Wi-Fi or Ethernet) > Network profile type > Private.",
        )
    if not any(str(port) in r.ports for r in matching):
        return Verdict(
            "warn",
            f"The firewall rule {PEER_RULE} is for another port than the peer "
            f"server's ({port}).",
            f"In an administrator prompt run {firewall_sync_command(python_exe)}.",
        )
    return Verdict(
        "ok",
        f"The rule {PEER_RULE} lets other laptops reach port {port} on Private and "
        "Domain networks.",
    )
