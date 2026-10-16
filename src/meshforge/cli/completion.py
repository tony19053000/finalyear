"""Shell completion for ``compute`` (V0.8 G6, D178).

``compute --install-completion`` / ``--show-completion`` are Typer's own options
(bash, zsh, fish, PowerShell; the shell is detected from the parent process).
One part of Typer's installer is replaced here: for PowerShell, Typer 0.27 runs
``Set-ExecutionPolicy Unrestricted -Scope CurrentUser`` before writing the
profile - silently weakening a team member's Windows script policy as a side
effect of asking for tab completion - and appends the script again on every run.

:func:`install_powershell` does neither: it appends the completion block to
``$PROFILE`` once (an idempotent marker check) and prints, without running it,
the one command to allow the profile if PowerShell blocks scripts. It is
installed into Typer's installer table by :func:`use_safe_installers`, which
:mod:`meshforge.cli.app` calls once at import; a unit test pins that Typer still
looks the function up there, so a Typer upgrade that moves it fails loudly
instead of quietly bringing the policy change back.

The bash installer is replaced too (:func:`install_bash`, V0.8 G6 T-5): Typer's
appends a newline to ``~/.bashrc`` on every run; ours leaves it byte-identical
on a re-run.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from typing import Final, NoReturn

from typer import _completion_shared as typer_completion

from meshforge.cli import render
from meshforge.cli.exits import CliExit

__all__ = ["MARKER", "install_powershell", "use_safe_installers"]

MARKER: Final = "# compute shell completion (MeshForge)"
"""First line of the block written to ``$PROFILE``: its presence = installed."""

_PROFILE_TIMEOUT_S: Final = 30.0


def _profile_path(shell: str) -> Path:
    """``$PROFILE`` of the current user, asked from PowerShell itself."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, the user's own shell
            [shell, "-NoProfile", "-Command", "echo", "$profile"],
            check=True,
            capture_output=True,
            timeout=_PROFILE_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        render.fail(
            f"cannot ask {shell} for your profile path ({type(exc).__name__}); "
            f"print the script with `compute --show-completion` and add it to "
            "$PROFILE yourself"
        )
        raise SystemExit(1) from None
    for encoding in ("utf-8", "cp1252", "cp850"):
        try:
            text = result.stdout.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
        if text:
            return Path(text)
    render.fail(f"{shell} did not print a profile path")
    raise SystemExit(1)


def install_powershell(*, prog_name: str, complete_var: str, shell: str) -> Path:
    """Append the completion block to ``$PROFILE`` once; never change the
    execution policy (module docstring)."""
    profile = _profile_path(shell)
    script = typer_completion.get_completion_script(
        prog_name=prog_name, complete_var=complete_var, shell=shell
    )
    try:
        existing = (
            profile.read_text(encoding="utf-8", errors="replace")
            if profile.is_file()
            else ""
        )
        if MARKER not in existing:
            profile.parent.mkdir(parents=True, exist_ok=True)
            with profile.open("a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(f"{MARKER}\n{script}\n")
    except OSError as exc:
        _install_failed(shell, exc)
    render.note(
        "if PowerShell says running scripts is disabled, allow your own profile "
        "once with: Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
    )
    return profile


def _bash_source_line(script: Path) -> str:
    """``source '<script>'`` - Typer's exact form when the path has no single
    quote (so a line Typer wrote earlier is recognised), else shell-quoted."""
    text = str(script)
    return f"source '{text}'" if "'" not in text else f"source {shlex.quote(text)}"


def install_bash(*, prog_name: str, complete_var: str, shell: str) -> Path:
    """Write ``~/.bash_completions/<prog>.sh`` and source it from ``~/.bashrc``
    once. Re-running leaves ``~/.bashrc`` byte-identical (V0.8 G6 T-5: Typer's
    installer appends a newline on every run); an existing file without a final
    newline is completed with one before the line is added, never rewritten."""
    home = Path.home()
    script = home / ".bash_completions" / f"{prog_name}.sh"
    rc = home / ".bashrc"
    content = (
        typer_completion.get_completion_script(
            prog_name=prog_name, complete_var=complete_var, shell=shell
        )
        + "\n"
    )
    try:
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(content, encoding="utf-8")
        existing = (
            rc.read_text(encoding="utf-8", errors="replace") if rc.is_file() else ""
        )
        line = _bash_source_line(script)
        if line not in existing.splitlines():
            with rc.open("a", encoding="utf-8") as fh:
                if existing and not existing.endswith("\n"):
                    fh.write("\n")
                fh.write(f"{line}\n")
    except OSError as exc:
        _install_failed("bash", exc)
    return script


def _install_failed(shell: str, exc: OSError) -> NoReturn:
    """T-10: a file the installer must write is not writable (read-only home,
    ``~/.bashrc`` is a directory, ``~/.bash_completions`` is a file...): one
    what / why / what-to-do line and exit 4 (local configuration), never the
    "internal error - this is a bug" line."""
    where = exc.filename if exc.filename else "your home folder"
    why = exc.strerror or type(exc).__name__
    render.fail(
        f"cannot install {shell} completion: {where}: {why} - fix that file or "
        "folder (permissions, or a file where a folder belongs), or print the "
        "script with `compute --show-completion` and install it yourself"
    )
    raise SystemExit(CliExit.CONFIG_ERROR)


def use_safe_installers() -> None:
    """Route Typer's PowerShell and bash installers to ours (module docstring)."""
    typer_completion.install_powershell = install_powershell
    typer_completion.install_bash = install_bash
