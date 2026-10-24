"""V0.17 G1: bundle facts, ``--version`` text, SP3 F5 (compute -> own agent), F2 root."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from meshforge import __version__
from meshforge.cli import fabric_commands
from meshforge.common import buildinfo
from meshforge.common.buildinfo import (
    BUILDINFO_NAME,
    bundle_command,
    bundle_root,
    read_build_info,
    stable_root,
    version_line,
)

pytestmark = pytest.mark.unit

_EXE = (
    ("python", "python.exe")
    if sys.platform == "win32"
    else ("python", "bin", "python3.12")
)
_SUFFIX = ".cmd" if sys.platform == "win32" else ""


def _fake_bundle(root: Path, *, info: object | None = None) -> Path:
    exe = root.joinpath(*_EXE)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    if info is not None:
        text = info if isinstance(info, str) else json.dumps(info)
        (root / BUILDINFO_NAME).write_text(text, encoding="utf-8")
    return exe


_INFO = {
    "version": "0.17.0",
    "git_sha": "0123456789abcdef0123",
    "role": "agent",
    "python": {"version": "3.12.14"},
}


def test_bundle_root_found_from_the_interpreter(tmp_path: Path) -> None:
    exe = _fake_bundle(tmp_path / "b", info=_INFO)
    assert bundle_root(exe) == tmp_path / "b"


def test_no_bundle_outside_one(tmp_path: Path) -> None:
    exe = _fake_bundle(tmp_path / "b")  # no BUILDINFO.json: a dev venv
    assert bundle_root(exe) is None
    assert bundle_root("relative/python") is None


def test_build_info_is_parsed(tmp_path: Path) -> None:
    _fake_bundle(tmp_path / "b", info=_INFO)
    info = read_build_info(tmp_path / "b")
    assert info is not None
    assert (info.version, info.role, info.python_version) == (
        "0.17.0",
        "agent",
        "3.12.14",
    )
    assert info.short_sha == "0123456789ab"


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_damaged_build_info_is_ignored(tmp_path: Path, text: str) -> None:
    _fake_bundle(tmp_path / "b", info=text)
    assert read_build_info(tmp_path / "b") is None


def test_version_line_dev_and_bundled(tmp_path: Path) -> None:
    _fake_bundle(tmp_path / "b", info=_INFO)
    info = read_build_info(tmp_path / "b")
    assert version_line("meshforge-agent", info) == (
        f"meshforge-agent {__version__} (git 0123456789ab, python 3.12.14, bundle)"
    )


def test_version_line_without_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(buildinfo, "current_build_info", lambda: None)
    assert version_line("compute") == f"compute {__version__}"


def test_bundle_command_needs_the_launcher(tmp_path: Path) -> None:
    root = tmp_path / "b"
    _fake_bundle(root, info=_INFO)
    assert bundle_command("meshforge-agent", root) is None
    (root / "bin").mkdir()
    launcher = root / "bin" / f"meshforge-agent{_SUFFIX}"
    launcher.write_text("", encoding="utf-8")
    assert bundle_command("meshforge-agent", root) == launcher


def test_compute_prefers_its_own_bundle_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SP3 F5: an agent on PATH (e.g. a dev venv) must not win over the agent
    shipped in the same bundle as this ``compute``."""
    root = tmp_path / "b"
    exe = _fake_bundle(root, info=_INFO)
    (root / "bin").mkdir()
    own = root / "bin" / f"meshforge-agent{_SUFFIX}"
    own.write_text("", encoding="utf-8")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(shutil, "which", lambda _n: "/elsewhere/agent")
    assert fabric_commands.agent_command() == [str(own)]


def test_compute_falls_back_to_path_outside_a_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = _fake_bundle(tmp_path / "venv")
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(shutil, "which", lambda _n: "/usr/bin/agent")
    assert fabric_commands.agent_command() == ["/usr/bin/agent"]


@pytest.mark.skipif(
    os.name != "posix", reason="the current symlink is the Linux layout"
)
def test_stable_root_prefers_the_current_pointer(tmp_path: Path) -> None:
    versioned = tmp_path / "opt" / "0.17.0"
    versioned.mkdir(parents=True)
    other = tmp_path / "opt" / "0.16.0"
    other.mkdir()
    assert stable_root(versioned) == versioned  # no pointer yet
    (tmp_path / "opt" / "current").symlink_to("0.16.0")
    assert stable_root(versioned) == versioned  # the pointer names another version
    (tmp_path / "opt" / "current").unlink()
    (tmp_path / "opt" / "current").symlink_to("0.17.0")
    assert stable_root(versioned) == tmp_path / "opt" / "current"
