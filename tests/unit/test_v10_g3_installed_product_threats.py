"""V1.0 G3 (D413, reviewer M4): the installed product's THREAT_MODEL rows (§5.10).

Pure checks of the decisions the rows rely on - each one a property of the
shipped code or packaging files, so a change that weakens it fails the unit
tier (and ``scripts/threat_model_check.py`` names these tests):

* **agent console** (V0.17 G3/G4, D391): binds ``127.0.0.1`` only, a foreign
  ``Host`` / ``Origin`` is refused, the token is no browser credential;
* **setup page** (V0.17 G2, D392): once a CA or any user exists the state is
  ``DONE`` and every ``/v1/setup/*`` route of the main app answers 410 to
  everyone;
* **services / installers** (D385-D389): the Windows service runs as its
  virtual account with an exact, non-inherited ACL (no Users / Everyone / Authenticated Users
  grant), the peer firewall rule is Private + Domain only and deleted by name,
  the Linux ``ufw`` profile opens exactly the D389 ports and is removed by its
  name even while ufw is inactive.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

pytest.importorskip("fastapi", reason="needs the controller extra")

import httpx
from fastapi import FastAPI
from pydantic import ValidationError

from meshforge.agent import winservice as ws
from meshforge.agent.console.auth import host_ok, origin_ok
from meshforge.agent.settings import ConsoleSettings
from meshforge.common import windows_service as wsvc
from meshforge.controller.api.errors import install_error_handlers
from meshforge.controller.setup.routes import SETUP_PATHS, gone_router
from meshforge.controller.setup.state import SetupState, setup_state

pytestmark = [pytest.mark.unit, pytest.mark.security]

REPO: Final = Path(__file__).resolve().parents[2]
D389_CONTROLLER_PORTS: Final = (47100, 47101, 47110)
WIDE_SIDS: Final = {
    "S-1-1-0",  # Everyone
    "S-1-5-11",  # Authenticated Users
    "S-1-5-32-545",  # BUILTIN\Users
    "S-1-5-4",  # INTERACTIVE
}


# ---------------------------------------------------------------- the console
@pytest.mark.parametrize(
    "bind",
    ["0.0.0.0", "::", "192.168.1.5", "10.0.0.2", "localhost", "127.0.0.2"],  # noqa: S104
)
def test_console_never_binds_beyond_ipv4_loopback(bind: str) -> None:
    with pytest.raises(ValidationError):
        ConsoleSettings(bind=bind)
    assert ConsoleSettings().bind == "127.0.0.1"


@pytest.mark.parametrize(
    "host",
    ["evil.example:47130", "192.168.1.5:47130", "localhost.evil:47130", None, ""],
)
def test_console_refuses_a_foreign_host_dns_rebinding(host: str | None) -> None:
    assert host_ok("127.0.0.1:47130", 47130)
    assert not host_ok(host, 47130)


@pytest.mark.parametrize(
    "origin", ["http://evil.example", "null", None, "https://127.0.0.1:47130"]
)
def test_console_writes_need_exactly_its_own_origin(origin: str | None) -> None:
    assert origin_ok("http://127.0.0.1:47130", 47130)
    assert not origin_ok(origin, 47130)


# -------------------------------------------------------------- the setup page
@pytest.mark.parametrize(
    ("ca", "users", "token"),
    [(True, False, True), (False, True, True), (True, True, False)],
)
def test_setup_is_done_once_a_ca_or_any_user_exists(
    ca: bool, users: bool, token: bool
) -> None:
    """Even with a leftover token file: no second admin can be created."""
    assert setup_state(ca_present=ca, users_exist=users, token_present=token) is (
        SetupState.DONE
    )


@pytest.mark.asyncio
async def test_every_setup_route_of_the_main_app_is_410_for_everyone() -> None:
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(gone_router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        assert SETUP_PATHS
        for method, path in SETUP_PATHS:
            for headers in ({}, {"authorization": "Bearer mfat_" + "A" * 43}):
                response = await client.request(method, path, headers=headers)
                assert response.status_code == 410, (method, path)


# --------------------------------------------------------- Windows service/ACL
def test_windows_service_runs_as_its_own_virtual_account() -> None:
    assert wsvc.SERVICE_ACCOUNT == r"NT SERVICE\MeshForgeAgent"
    assert wsvc.service_sid().startswith("S-1-5-80-")


def test_windows_machine_state_acl_is_exact_and_never_wide() -> None:
    layout = ws.machine_layout(r"C:\ProgramData")
    svc = wsvc.service_sid()
    user = "S-1-5-21-1-2-3-1001"
    plan = ws.acl_plan(layout, svc, [user, *WIDE_SIDS])
    assert {acl.path for acl in plan} == set(layout.folders)
    for acl in plan:
        sids = {g.sid for g in acl.grants}
        assert {wsvc.SID_SYSTEM, wsvc.SID_ADMINISTRATORS, svc} <= sids, acl.path
        if acl.path != layout.console_dir:
            assert sids == {wsvc.SID_SYSTEM, wsvc.SID_ADMINISTRATORS, svc}, acl.path
        # the installing user (and only for the console token folder) reads
        for grant in acl.grants:
            if grant.sid not in (wsvc.SID_SYSTEM, wsvc.SID_ADMINISTRATORS, svc):
                assert acl.path == layout.console_dir
                assert grant.access is ws.Access.READ
    identity = next(a for a in plan if a.path == layout.identity_dir)
    assert identity.owner_sid == svc
    # every icacls command removes inheritance and replaces grants by SID
    for acl in plan:
        argvs = ws.icacls_argv(acl)
        flat = [arg for argv in argvs for arg in argv]
        assert "/inheritance:r" in flat
        # /grant:r replaces; a plain /grant would add to what is there
        assert "/grant" not in flat and "/grant:r" in flat


def test_windows_console_readers_never_include_wide_groups_by_default() -> None:
    """A wide SID only gets in if an administrator names it; the installer
    passes the installing user alone (Q30)."""
    layout = ws.machine_layout(r"C:\ProgramData")
    plan = ws.acl_plan(layout, wsvc.service_sid(), ["S-1-5-21-1-2-3-1001"])
    console = next(a for a in plan if a.path == layout.console_dir)
    assert not ({g.sid for g in console.grants} & WIDE_SIDS)


def test_windows_firewall_rule_is_private_and_domain_only_and_removed_by_name() -> None:
    assert ws.FIREWALL_PROFILES == "private,domain"
    argv = ws.firewall_add_argv(ws.peer_firewall_rule())
    assert "profile=private,domain" in argv
    assert not any("public" in a.lower() or a.lower() == "profile=any" for a in argv)
    assert "dir=in" in argv and "localport=47120" in argv
    assert ws.firewall_delete_argv(ws.PEER_FIREWALL_RULE) == [
        "netsh", "advfirewall", "firewall", "delete", "rule",
        "name=MeshForge-Agent-Peer",
    ]  # fmt: skip


def test_the_installer_script_opens_no_firewall_port_itself() -> None:
    """All rules go through ``winservice`` (above); the .iss has no netsh rule
    and nothing that names the Public profile."""
    iss = (REPO / "packaging" / "windows" / "meshforge-agent.iss").read_text("utf-8")
    assert "advfirewall" not in iss.lower()
    assert not re.search(r"profile\s*=\s*(public|any)", iss, re.IGNORECASE)


# ----------------------------------------------------------- Linux packaging
def test_ufw_profile_opens_exactly_the_d389_controller_ports() -> None:
    text = (REPO / "packaging/linux/controller/meshforge-controller.ufw").read_text()
    (ports,) = re.findall(r"^ports=(.+)$", text, re.MULTILINE)
    assert ports == ",".join(str(p) for p in D389_CONTROLLER_PORTS) + "/tcp"
    assert "[MeshForge Controller]" in text


def test_postinst_enables_the_rule_only_when_ufw_is_active_and_never_enables_ufw() -> (
    None
):
    postinst = (REPO / "packaging/linux/controller/scripts/postinst").read_text()
    assert "ufw allow 'MeshForge Controller'" in postinst
    assert "Status: active" in postinst
    assert "ufw enable" not in postinst and "ufw --force enable" not in postinst


def test_prerm_deletes_the_rule_by_name_even_when_ufw_is_inactive() -> None:
    """``ufw status`` lists no rule while ufw is inactive, so a rule added
    while it was active would survive a remove; ``ufw show added`` lists it
    either way (V1.0 G3 fix)."""
    prerm = (REPO / "packaging/linux/controller/scripts/prerm").read_text()
    assert "ufw show added" in prerm
    assert "ufw delete allow 'MeshForge Controller'" in prerm
    assert "ufw status 2>/dev/null | grep -q 'MeshForge Controller'" not in prerm
