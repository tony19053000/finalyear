"""The product is shown to people as "Hive"; installed names stay "MeshForge".

The Linux app-menu entry and the Windows installer show "Hive" (menu names,
Start-menu shortcuts, Apps list, wizard text). Everything a machine relies on
keeps its old name so upgrades in place keep working: the .desktop file name and
its Exec= line, the installer AppId, the service name, the install folders and
the setup output file name.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
DESKTOP = REPO / "packaging" / "linux" / "desktop" / "meshforge.desktop"
ISS = (REPO / "packaging" / "windows" / "meshforge-agent.iss").read_text(
    encoding="utf-8"
)
LICENSE = (REPO / "packaging" / "windows" / "license.txt").read_text(encoding="utf-8")


def _desktop_entry() -> dict[str, str]:
    entry: dict[str, str] = {}
    for line in DESKTOP.read_text(encoding="utf-8").splitlines():
        if line.startswith("[") and entry:
            break  # only the main [Desktop Entry] group
        key, sep, value = line.partition("=")
        if sep:
            entry[key] = value
    return entry


def _iss(key: str) -> str:
    match = re.search(rf"^{re.escape(key)}=(.*)$", ISS, re.M)
    assert match, key
    return match.group(1)


def test_linux_menu_entry_is_named_hive() -> None:
    entry = _desktop_entry()
    assert entry["Name"] == "Hive"
    for key in ("Name", "GenericName", "Comment"):
        assert "MeshForge" not in entry.get(key, ""), key


def test_linux_menu_entry_keeps_its_installed_names() -> None:
    entry = _desktop_entry()
    assert DESKTOP.name == "meshforge.desktop"
    assert entry["Exec"] == (
        "env MESHFORGE_INSTALL_MODE=system /usr/bin/meshforge-console-open"
    )
    assert entry["Icon"] == "meshforge"


def test_windows_installer_shows_hive() -> None:
    assert _iss("AppName") == "Hive Agent"
    assert _iss("AppVerName") == "Hive Agent {#AppVersion}"
    assert _iss("UninstallDisplayName") == "Hive Agent"
    assert _iss("VersionInfoProductName") == "Hive Agent"
    assert _iss("FinishedHeadingLabel") == "Hive setup finished"
    icons = [
        line for line in ISS.splitlines() if line.startswith('Name: "{autoprograms}')
    ]
    assert [line.split('"')[1] for line in icons] == [
        r"{autoprograms}\Hive",
        r"{autoprograms}\Hive - give Docker access",
    ]
    assert all("MeshForge" not in line for line in icons)
    assert 'Description: "Open Hive"' in ISS
    assert LICENSE.splitlines()[0] == "Hive Agent"


def test_windows_upgrade_removes_the_old_start_menu_entries() -> None:
    section = ISS.split("[InstallDelete]", 1)[1].split("\n[", 1)[0]
    assert r'Name: "{autoprograms}\MeshForge.lnk"' in section
    assert r'Name: "{autoprograms}\MeshForge - give Docker access.lnk"' in section


def test_windows_installer_keeps_its_installed_names() -> None:
    # Upgrades in place and the uninstall entry are found by the AppId.
    assert _iss("AppId") == "{#AppId}"
    assert '#define AppId "{{A8774C05-C367-491D-833B-4979498B0BEE}"' in ISS
    assert '#define ServiceName "MeshForgeAgent"' in ISS
    assert _iss("DefaultDirName") == r"{autopf}\MeshForge"
    assert '"MeshForge-Agent-Setup-" + VersionDir' in ISS
    assert r"{commonappdata}\MeshForge\logs\setup-result.json" in ISS
    assert "(MeshForgeAgent)" in LICENSE
