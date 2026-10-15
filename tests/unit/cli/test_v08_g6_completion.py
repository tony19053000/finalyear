"""V0.8 G6 (D178) deliverable 6: shell completion.

* ``compute --show-completion`` prints a script for bash, zsh, fish and
  PowerShell (Typer's test switch picks the shell instead of detecting it);
* the bash script **loads** in a real bash and **completes** through the
  installed ``compute`` console script (commands, subcommands, options);
* ``--install-completion`` for bash writes the script and sources it from
  ``~/.bashrc`` once, and an interactive bash then has the completion;
* PowerShell install never runs ``Set-ExecutionPolicy`` and never adds the
  block twice (``meshforge.cli.completion``); zsh/pwsh scripts are loaded by
  the real shell when it is installed (skipped otherwise).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer import _completion_shared as typer_completion

from meshforge.cli import app as cli_app  # noqa: F401 - installs the safe installer
from meshforge.cli import completion

pytestmark = pytest.mark.unit

COMPUTE = Path(sys.executable).with_name("compute")
_TIMEOUT_S = 60


def _env(home: Path | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("MESHFORGE_")}
    env["_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION"] = "1"
    env["PATH"] = f"{COMPUTE.parent}{os.pathsep}{env.get('PATH', '')}"
    if home is not None:
        env["HOME"] = str(home)
    return env


def _show(shell: str) -> str:
    done = subprocess.run(
        [sys.executable, "-m", "meshforge.cli", "--show-completion", shell],
        capture_output=True,
        text=True,
        env=_env(),
        timeout=_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "powershell", "pwsh"])
def test_show_completion_prints_a_script_for_every_shell(shell: str) -> None:
    script = _show(shell)
    assert "_COMPUTE_COMPLETE" in script
    assert "compute" in script
    if shell in {"powershell", "pwsh"}:
        assert "Register-ArgumentCompleter" in script


needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="no bash")
needs_compute = pytest.mark.skipif(
    not COMPUTE.is_file(), reason="the compute console script is not installed"
)


def _bash(script: str, *, home: Path | None = None, interactive: bool = False) -> Any:
    argv = ["bash", "--norc", "--noprofile", "-c", script]
    if interactive:
        assert home is not None
        argv = ["bash", "--rcfile", str(home / ".bashrc"), "-i", "-c", script]
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        env=_env(home),
        timeout=_TIMEOUT_S,
        check=False,
    )


@needs_bash
def test_the_bash_script_loads(tmp_path: Path) -> None:
    script = tmp_path / "compute.sh"
    script.write_text(_show("bash"), encoding="utf-8")
    done = _bash(f"source '{script}' && complete -p compute")
    assert done.returncode == 0, done.stderr
    assert "-F _compute_completion compute" in done.stdout


def _complete(script: Path, *words: str) -> list[str]:
    quoted = " ".join(f"'{w}'" for w in words)
    done = _bash(
        f"source '{script}'; COMP_WORDS=({quoted}); "
        f"COMP_CWORD={len(words) - 1}; _compute_completion compute; "
        'printf "%s\\n" "${COMPREPLY[@]}"'
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.split()


@needs_bash
@needs_compute
def test_bash_completes_commands_subcommands_and_options(tmp_path: Path) -> None:
    script = tmp_path / "compute.sh"
    script.write_text(_show("bash"), encoding="utf-8")
    assert set(_complete(script, "compute", "no")) == {"nodes", "node"}
    assert set(_complete(script, "compute", "tokens", "")) >= {"list", "revoke"}
    assert "--json" in _complete(script, "compute", "jobs", "--j")
    assert "--state" in _complete(script, "compute", "jobs", "--st")


@needs_bash
@needs_compute
def test_install_completion_for_bash_is_idempotent_and_works(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    for _ in range(2):
        done = subprocess.run(
            [str(COMPUTE), "--install-completion", "bash"],
            capture_output=True,
            text=True,
            env=_env(home),
            timeout=_TIMEOUT_S,
            check=False,
        )
        assert done.returncode == 0, done.stdout + done.stderr
    installed = home / ".bash_completions" / "compute.sh"
    assert installed.is_file()
    bashrc = (home / ".bashrc").read_text(encoding="utf-8")
    assert bashrc.count("compute.sh") == 1
    done = _bash("complete -p compute", home=home, interactive=True)
    assert "_compute_completion compute" in done.stdout, done.stderr


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh not installed")
def test_the_zsh_script_loads(
    tmp_path: Path,
) -> None:  # pragma: no cover - CI has no zsh
    script = tmp_path / "_compute"
    script.write_text(_show("zsh"), encoding="utf-8")
    done = subprocess.run(
        [
            "zsh",
            "-f",
            "-c",
            f"autoload -U compinit && compinit -u && source '{script}'",
        ],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        timeout=_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell not installed")
def test_the_powershell_script_loads(tmp_path: Path) -> None:  # pragma: no cover
    script = tmp_path / "compute.ps1"
    script.write_text(_show("pwsh"), encoding="utf-8")
    done = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", f". '{script}'"],
        capture_output=True,
        text=True,
        env=_env(tmp_path),
        timeout=_TIMEOUT_S,
        check=False,
    )
    assert done.returncode == 0, done.stderr


# ---------------------------------------------------------- PowerShell install


def test_typer_still_routes_powershell_through_our_installer() -> None:
    """A Typer upgrade that stops looking the installer up in its module would
    silently bring back ``Set-ExecutionPolicy Unrestricted``."""
    assert typer_completion.install_powershell is completion.install_powershell
    source = Path(typer_completion.__file__).read_text(encoding="utf-8")
    assert "installed_path = install_powershell(" in source
    # fix round 1 (T-5): bash goes through ours too
    assert typer_completion.install_bash is completion.install_bash
    assert "installed_path = install_bash(" in source


@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_powershell_install_never_changes_the_execution_policy(
    shell: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    profile = tmp_path / "Documents" / "PowerShell" / "profile.ps1"
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        calls.append(list(argv))
        return subprocess.CompletedProcess(argv, 0, stdout=f"{profile}\r\n".encode())

    monkeypatch.setattr(completion.subprocess, "run", fake_run)
    for _ in range(2):
        _name, path = typer_completion.install(
            shell=shell, prog_name="compute", complete_var="_COMPUTE_COMPLETE"
        )
    assert path == profile
    text = profile.read_text(encoding="utf-8")
    assert text.count(completion.MARKER) == 1
    assert text.count("Register-ArgumentCompleter") == 1
    assert all("Set-ExecutionPolicy" not in " ".join(c) for c in calls)
    assert calls == [[shell, "-NoProfile", "-Command", "echo", "$profile"]] * 2
    assert "RemoteSigned" in capsys.readouterr().err


def test_powershell_install_keeps_an_existing_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = tmp_path / "profile.ps1"
    profile.write_text("Set-Alias ll Get-ChildItem", encoding="utf-8")

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(argv, 0, stdout=str(profile).encode())

    monkeypatch.setattr(completion.subprocess, "run", fake_run)
    completion.install_powershell(  # noqa: S604 - a shell name, not shell=True
        prog_name="compute", complete_var="_COMPUTE_COMPLETE", shell="pwsh"
    )
    text = profile.read_text(encoding="utf-8")
    assert text.startswith("Set-Alias ll Get-ChildItem\n" + completion.MARKER)


def test_powershell_missing_is_one_line_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def missing(_argv: list[str], **_kwargs: Any) -> Any:
        raise FileNotFoundError("pwsh")

    monkeypatch.setattr(completion.subprocess, "run", missing)
    with pytest.raises(SystemExit) as exited:
        completion.install_powershell(  # noqa: S604 - a shell name, not shell=True
            prog_name="compute", complete_var="_COMPUTE_COMPLETE", shell="pwsh"
        )
    assert exited.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("compute: cannot ask pwsh") and err.count("\n") == 1
