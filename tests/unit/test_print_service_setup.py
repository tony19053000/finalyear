"""V0.3 G5 unit tests for ``scripts/print_service_setup.py`` (roadmap 0.3.13).

The whole point of the script is decision **D17**: it *prints* the commands and
installs nothing. So the tests check exactly that - the output is there, it names
the right files, and the script runs no subprocess, touches no file and needs
neither a controller nor an agent.
"""

from __future__ import annotations

import builtins
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "print_service_setup.py"


def load() -> ModuleType:
    """Import the script by path: ``scripts/`` is not an importable package."""
    spec = importlib.util.spec_from_file_location("mf_print_service_setup", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_shipped_assets_the_script_points_at_all_exist() -> None:
    for relative in (
        "deploy/systemd/meshforge-agent.service",
        "deploy/systemd/meshforge-controller.service",
        "deploy/windows/meshforge-agent-task.xml",
        "docs/OPERATIONS.md",
    ):
        assert (REPO / relative).is_file(), relative


@pytest.mark.parametrize("choice", ["linux", "windows", "uninstall", "all"])
def test_each_section_prints_and_exits_zero(
    choice: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert load().main([choice]) == 0
    out = capsys.readouterr().out
    assert "NOTHING WAS INSTALLED" in out
    assert "docs/OPERATIONS.md" in out


def test_the_linux_section_names_linger_and_the_journal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    load().main(["linux"])
    out = capsys.readouterr().out
    assert "loginctl enable-linger" in out
    assert "journalctl --user -u meshforge-agent" in out
    assert "systemctl --user enable --now meshforge-agent" in out


def test_the_windows_section_names_schtasks_the_log_and_the_open_question(
    capsys: pytest.CaptureFixture[str],
) -> None:
    load().main(["windows"])
    out = capsys.readouterr().out
    assert "schtasks /create /tn MeshForgeAgent /sc onlogon" in out
    assert "MeshForge\\agent.log" in out
    assert "UNVERIFIED" in out
    assert "NSSM" in out


def test_the_uninstall_section_removes_both_platforms(
    capsys: pytest.CaptureFixture[str],
) -> None:
    load().main(["uninstall"])
    out = capsys.readouterr().out
    assert "systemctl --user disable --now meshforge-agent" in out
    assert "schtasks /delete /tn MeshForgeAgent /f" in out
    assert "identity" in out  # says what is *not* removed


def test_the_windows_commands_are_not_printed_with_a_posix_path(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cmd.exe command line containing ``/home/...`` would be quietly wrong."""
    monkeypatch.setattr(sys, "platform", "linux")
    load().main(["windows"])
    out = capsys.readouterr().out
    assert "C:\\path\\to\\meshforge" in out
    assert str(REPO) not in out


def test_it_runs_no_subprocess_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """D17: printing only. Any attempt to execute or to open a file for writing
    fails the test rather than changing the machine."""

    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail(f"the script must not execute anything: {args!r}")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("os.system", forbidden)

    real_open = builtins.open

    def guarded_open(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
        assert not set(mode) & set("wax+"), f"the script must not write: {file!r}"
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    assert load().main(["all"]) == 0
    assert capsys.readouterr().out


def test_running_it_as_a_process_needs_no_controller_and_no_agent() -> None:
    """The real invocation from the docs, on a machine with nothing running."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "all"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "NOTHING WAS INSTALLED" in result.stdout
    assert result.stderr == ""
