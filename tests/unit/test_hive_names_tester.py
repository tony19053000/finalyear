"""Tester: no visible "MeshForge" in CLI help of any command; upgrade-in-place invariants."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import typer
from typer.main import get_command

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
ISS = (REPO / "packaging" / "windows" / "meshforge-agent.iss").read_text(
    encoding="utf-8"
)


def _walk(cmd, path):  # type: ignore[no-untyped-def]
    yield path, cmd
    for name, sub in getattr(cmd, "commands", {}).items():
        yield from _walk(sub, f"{path} {name}")


def _apps():  # type: ignore[no-untyped-def]
    from meshforge.agent.main import app as agent
    from meshforge.cli.app import app as cli
    from meshforge.controller.app import app as controller

    return {"agent": agent, "compute": cli, "controller": controller}


@pytest.mark.parametrize("which", ["agent", "compute", "controller"])
def test_no_command_help_text_says_meshforge(which: str) -> None:
    app: typer.Typer = _apps()[which]
    offenders = []
    for path, cmd in _walk(get_command(app), which):
        texts = [cmd.help or "", getattr(cmd, "short_help", "") or ""]
        texts += [getattr(p, "help", "") or "" for p in cmd.params]
        offenders.extend((path, t[:80]) for t in texts if "MeshForge" in t)
    assert not offenders, offenders


def test_installer_upgrade_in_place_invariants() -> None:
    assert 'AppId "{{A8774C05-C367-491D-833B-4979498B0BEE}"' in ISS
    assert r"DefaultDirName={autopf}\MeshForge" in ISS
    assert 'ServiceName "MeshForgeAgent"' in ISS
    assert 'OutputName "MeshForge-Agent-Setup-"' in ISS


def test_install_delete_only_removes_the_two_old_shortcuts() -> None:
    section = re.search(r"^\[InstallDelete\]\n(.*?)(?=^\[)", ISS, re.M | re.S)
    assert section
    entries = [
        ln
        for ln in section.group(1).splitlines()
        if ln.strip() and not ln.startswith(";")
    ]
    assert sorted(entries) == sorted(
        [
            r'Type: files; Name: "{autoprograms}\MeshForge.lnk"',
            r'Type: files; Name: "{autoprograms}\MeshForge - give Docker access.lnk"',
        ]
    )


def test_old_shortcut_names_match_what_the_previous_release_created() -> None:
    # The [Icons] now create Hive*.lnk; the deleted names are exactly the old ones.
    assert r'{autoprograms}\Hive"' in ISS
    assert r'{autoprograms}\Hive - give Docker access"' in ISS
    assert r'{autoprograms}\MeshForge"' not in ISS


def test_desktop_file_exec_and_icon_unchanged() -> None:
    d = (REPO / "packaging/linux/desktop/meshforge.desktop").read_text(encoding="utf-8")
    assert (
        "Exec=env MESHFORGE_INSTALL_MODE=system /usr/bin/meshforge-console-open\n" in d
    )
    assert "Icon=meshforge\n" in d
