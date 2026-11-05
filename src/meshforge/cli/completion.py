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

V1.0 G9-F (D439): the shell is found by :mod:`meshforge.cli.shell_detect`, which
on Windows looks past the ``cmd.exe /c`` that runs the installed ``compute.cmd``
launcher (Typer's own lookup answered ``cmd`` in PowerShell). A shell can also be
named: ``compute --install-completion powershell`` (:func:`take_explicit_shell`,
called by ``main``). A shell with no completion (cmd.exe, an unknown one) gets one
what / why / what-to-do line and exit 4, like every other install failure.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Collection, Sequence
from contextvars import ContextVar
from pathlib import Path
from typing import Final, NoReturn

from typer import _completion_shared as typer_completion
from typer import completion as typer_completion_options

from meshforge.cli import render
from meshforge.cli.exits import CliExit
from meshforge.cli.shell_detect import SHELL_NAMES, detect_shell

__all__ = [
    "COMPLETION_OPTIONS",
    "MARKER",
    "SUPPORTED_SHELLS",
    "clear_explicit_shell",
    "current_shell",
    "fail_unsupported",
    "install",
    "install_powershell",
    "misnamed_shell",
    "take_explicit_shell",
    "use_safe_installers",
]

MARKER: Final = "# compute shell completion (MeshForge)"
"""First line of the block written to ``$PROFILE``: its presence = installed."""

_PROFILE_TIMEOUT_S: Final = 30.0

SUPPORTED_SHELLS: Final = ("bash", "zsh", "fish", "powershell", "pwsh")
"""Shells Typer has a completion script for (``typer._completion_shared.Shells``)."""

COMPLETION_OPTIONS: Final = ("--install-completion", "--show-completion")

_TEST_SWITCH: Final = "_TYPER_COMPLETE_TEST_DISABLE_SHELL_DETECTION"
"""Typer's test switch: the two options then take the shell as their value."""

# Typer's own functions, kept before use_safe_installers() replaces them.
_typer_get_shell_name = typer_completion._get_shell_name
_typer_install = typer_completion.install
_typer_get_completion_script = typer_completion.get_completion_script

_explicit_shell: ContextVar[str | None] = ContextVar(
    "compute_completion_shell", default=None
)


def take_explicit_shell(argv: Sequence[str]) -> list[str]:
    """``compute --install-completion powershell``: Typer's options are flags
    (the shell is detected), so the name after one would be read as a command
    and ignored (lab L2 C8.10-g). A shell name right after either option is
    taken out of ``argv`` and used instead of detection (one without completion,
    e.g. ``cmd``, then gets :func:`fail_unsupported`'s line). Nothing after ``--``
    is looked at (arguments for the user's own command, review r1 T2). With
    Typer's test switch set the options take a value themselves: ``argv`` is
    unchanged."""
    args = list(argv)
    if os.environ.get(_TEST_SWITCH):
        return args
    for index, arg in enumerate(args[:-1]):
        if arg == "--":
            break
        if arg in COMPLETION_OPTIONS and args[index + 1].lower() in SHELL_NAMES:
            _explicit_shell.set(args[index + 1].lower())
            del args[index + 1]
            break
    return args


def misnamed_shell(argv: Sequence[str], commands: Collection[str]) -> str | None:
    """A bare word right after either option that is neither a shell name nor
    one of ``commands`` (``--show-completion powerhsell``): the caller fails
    with :func:`fail_unsupported` instead of silently using the detected shell
    (review r1 T3). Run on ``argv`` before :func:`take_explicit_shell`; nothing
    after ``--`` counts; ``None`` with Typer's test switch set."""
    if os.environ.get(_TEST_SWITCH):
        return None
    for index, arg in enumerate(argv[:-1]):
        if arg == "--":
            return None
        if arg not in COMPLETION_OPTIONS:
            continue
        word = argv[index + 1]
        if word.startswith("-") or word.lower() in SHELL_NAMES or word in commands:
            continue
        return word
    return None


def clear_explicit_shell() -> None:
    """Forget a shell named by :func:`take_explicit_shell` (``main`` calls it
    when the command ends, so in-process calls do not inherit it)."""
    _explicit_shell.set(None)


def current_shell() -> str | None:
    """The shell named on the command line, else the detected one
    (:mod:`meshforge.cli.shell_detect`)."""
    named = _explicit_shell.get()
    if named is not None:
        return named
    return detect_shell(fallback=_typer_get_shell_name)


def fail_unsupported(shell: str | None) -> NoReturn:
    """One line for a shell without completion, then exit 4 (D191 d)."""
    if shell == "cmd":
        render.fail(
            "tab completion is not available in cmd.exe (Command Prompt) - open "
            "PowerShell and run `compute --install-completion` there"
        )
    elif not shell:
        render.fail(
            "cannot tell which shell this is - name it: compute "
            "--install-completion powershell (or pwsh, bash, zsh, fish)"
        )
    else:
        render.fail(
            f"{render.flat(shell)} has no tab completion support - "
            "supported shells: " + ", ".join(SUPPORTED_SHELLS)
        )
    raise SystemExit(CliExit.CONFIG_ERROR)


def install(
    shell: str | None = None,
    prog_name: str | None = None,
    complete_var: str | None = None,
) -> tuple[str, Path]:
    """Typer's ``install`` with our shell lookup and our unsupported-shell line."""
    if shell is None and not os.environ.get(_TEST_SWITCH):
        shell = current_shell()
    if shell not in SUPPORTED_SHELLS:
        fail_unsupported(shell)
    return _typer_install(shell=shell, prog_name=prog_name, complete_var=complete_var)


def _completion_script(*, prog_name: str, complete_var: str, shell: str) -> str:
    """``--show-completion``'s script lookup, with our unsupported-shell line."""
    if shell not in SUPPORTED_SHELLS:
        fail_unsupported(shell)
    return _typer_get_completion_script(
        prog_name=prog_name, complete_var=complete_var, shell=shell
    )


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
        # V0.8 G3 item 0 (D191 d): one exit code for every install failure.
        raise SystemExit(CliExit.CONFIG_ERROR) from None
    for encoding in ("utf-8", "cp1252", "cp850"):
        try:
            text = result.stdout.decode(encoding).strip()
        except UnicodeDecodeError:
            continue
        if text:
            return Path(text)
    render.fail(
        f"{shell} did not print a profile path; print the script with "
        "`compute --show-completion` and add it to $PROFILE yourself"
    )
    raise SystemExit(CliExit.CONFIG_ERROR)


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


def _bash_source_line(script: Path) -> bytes:
    """``source '<script>'`` - Typer's exact form when the path has no single
    quote (so a line Typer wrote earlier is recognised), else shell-quoted.

    Bytes, in the file system's encoding: a home folder whose name is not
    UTF-8 is still one exact line (V0.8 G3 item 0, D191 e)."""
    text = str(script)
    line = f"source '{text}'" if "'" not in text else f"source {shlex.quote(text)}"
    return os.fsencode(line)


def install_bash(*, prog_name: str, complete_var: str, shell: str) -> Path:
    """Write ``~/.bash_completions/<prog>.sh`` and source it from ``~/.bashrc``
    once. Re-running leaves ``~/.bashrc`` byte-identical (V0.8 G6 T-5: Typer's
    installer appends a newline on every run); an existing file without a final
    newline is completed with one before the line is added, never rewritten.
    ``~/.bashrc`` is handled as bytes: text in any encoding (Latin-1 comments,
    a non-UTF-8 home path) neither breaks the check nor duplicates the line
    (V0.8 G3 item 0, D191 e)."""
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
        existing = rc.read_bytes() if rc.is_file() else b""
        line = _bash_source_line(script)
        if line not in existing.splitlines():
            with rc.open("ab") as fh:
                if existing and not existing.endswith(b"\n"):
                    fh.write(b"\n")
                fh.write(line + b"\n")
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
    """Route Typer's PowerShell and bash installers, its shell lookup and its
    unsupported-shell handling to ours (module docstring).

    ``typer.completion`` imported ``install``, ``get_completion_script`` and
    ``_get_shell_name`` by name, so they are replaced there (where the two
    options' callbacks look them up); the installers are looked up in
    ``typer._completion_shared``."""
    typer_completion.install_powershell = install_powershell
    typer_completion.install_bash = install_bash
    typer_completion._get_shell_name = current_shell
    setattr(typer_completion_options, "install", install)  # noqa: B010
    setattr(  # noqa: B010 - not re-exported by typer.completion (mypy)
        typer_completion_options, "get_completion_script", _completion_script
    )
    setattr(typer_completion_options, "_get_shell_name", current_shell)  # noqa: B010
