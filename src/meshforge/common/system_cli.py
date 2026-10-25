r"""A human's admin command acts on the system service's state (V0.17 G1/G2, Q15).

On a machine with a MeshForge ``.deb`` installed, the state of the packaged
services (agent identity, controller database passwords, CA) belongs to the
``meshforge`` service account (``/var/lib/meshforge``, ``/etc/meshforge``). A
person typing ``meshforge-agent identity show`` or ``meshforge-controller db
backup`` runs as themselves, in user mode (``MESHFORGE_INSTALL_MODE`` is only
set by the units, Q11) - without this module that would read (or mint) a second
state in their home, or be refused by the owner-only files.

So before any command runs, :func:`decide` checks: Linux, the package is present
(``/etc/meshforge`` **and** the package's ``current`` pointer), the caller is not
the service user, ``MESHFORGE_INSTALL_MODE`` is unset, and the command is not one
that is fine for anyone (``--version``, ``--help``, plus per-program extras).
Then:

* the CLI is the packaged one (runs from a bundle) -> re-execute it as the
  service user: ``sudo -u meshforge -- /usr/bin/env MESHFORGE_INSTALL_MODE=system
  [extra env] <launcher> <args>``, saying so on stderr (``sudo`` asks for the
  admin's password; non-admins cannot, which is right);
* the CLI is a developer checkout (``uv run``) -> it keeps its user-mode state
  but warns on stderr that the service's state is elsewhere.

``MESHFORGE_INSTALL_MODE=user`` is the explicit opt-out (a developer who wants
their own state on a machine that also has the package).

**Windows (V0.17 G4, Q32).** The service runs as the virtual account
``NT SERVICE\MeshForgeAgent``, which no one can ``sudo`` into. With the
installer's files present (``%ProgramData%\MeshForge\config`` **and**
``%ProgramFiles%\MeshForge\current.txt``), :func:`decide_windows`:

* an **elevated** packaged CLI acts on the service's state *in this process*
  (``MESHFORGE_INSTALL_MODE=system`` is set before the command runs; files it
  writes stay readable by the service, ``common.windows_service``), saying so;
* a non-elevated one is refused with "run as administrator, or use the
  MeshForge console" (never a silent second identity in ``%LOCALAPPDATA%``);
* commands in ``SystemProgram.windows_refused`` (the agent's ``run``: a second
  agent with the service's identity would push the service off the fabric)
  are refused with what to do instead;
* a developer checkout warns, as on Linux.

Light on purpose: this module runs before every command, ``--version``
included, so it imports neither ``common.config`` (pydantic) nor anything else
heavy. :mod:`meshforge.agent.system_cli` and the controller's ``main`` use it.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

from meshforge.common.exit_codes import ExitCode

__all__ = [
    "INSTALL_MODE_ENV_VAR",
    "SERVICE_USER",
    "SYSTEM_LINUX_CONFIG_DIR",
    "SYSTEM_LINUX_DATA_DIR",
    "SYSTEM_PROGRAM_DIR",
    "Decision",
    "SystemProgram",
    "apply",
    "current_user",
    "decide",
    "first_command",
]

# Pinned equal to common.config's by tests/unit/agent/test_v017_g1_system_cli.py.
INSTALL_MODE_ENV_VAR: Final = "MESHFORGE_INSTALL_MODE"
SYSTEM_LINUX_CONFIG_DIR: Final = Path("/etc/meshforge")
SYSTEM_LINUX_DATA_DIR: Final = Path("/var/lib/meshforge")
SERVICE_USER: Final = "meshforge"
SYSTEM_PROGRAM_DIR: Final = Path("/opt/meshforge")
_ALWAYS_ANYONE: Final = frozenset({"--version", "--help", "-h"})


@dataclass(frozen=True, slots=True)
class SystemProgram:
    """One packaged command-line program."""

    prog: str
    """``meshforge-agent`` / ``meshforge-controller`` (messages)."""
    program_dir: Path
    """Where the package keeps its versions and the ``current`` pointer."""
    anyone: frozenset[str] = frozenset()
    """First arguments that run in place for anyone (besides --version/--help)."""
    run_in_place_without_args: bool = True
    """The bare command runs in place (the agent: help; the controller: a dev
    server - a human never starts a second packaged server by accident)."""
    extra_env: tuple[str, ...] = field(default=())
    """``NAME=value`` items the service unit also sets (e.g. the config file)."""
    reexec_commands: frozenset[str] | None = None
    """When set, only these sub-commands re-execute (the first positional
    argument; a value after an option in ``value_options`` is skipped). None:
    every command except the ``anyone`` ones (the agent)."""
    value_options: frozenset[str] = frozenset({"--config"})
    windows: bool = False
    """The program has a Windows install to act on (V0.17 G4: the agent; the
    controller is Linux-only, Q4, so a Windows controller command never looks
    at the agent installer's files)."""
    windows_refused: Mapping[str, str] = field(default_factory=dict)
    """Windows: sub-command -> why it is refused on the installed system, and
    what to do instead (V0.17 G4)."""

    @property
    def launcher(self) -> Path:
        return self.program_dir / "current" / "bin" / self.prog

    def installed(self, config_dir: Path = SYSTEM_LINUX_CONFIG_DIR) -> bool:
        return config_dir.is_dir() and (self.program_dir / "current").exists()


@dataclass(frozen=True, slots=True)
class Decision:
    action: Literal["none", "reexec", "warn", "inplace", "refuse"]
    argv: tuple[str, ...] = ()
    message: str = ""


def windows_system_dirs(
    environ: Mapping[str, str],
) -> tuple[Path, Path]:
    """``(%ProgramData%\\MeshForge, %ProgramFiles%\\MeshForge)`` (D385)."""
    data = environ.get("PROGRAMDATA") or "C:\\ProgramData"
    program = environ.get("PROGRAMFILES") or "C:\\Program Files"
    return Path(data) / "MeshForge", Path(program) / "MeshForge"


def windows_installed(environ: Mapping[str, str] | None = None) -> bool:
    """The Windows installer's files are present (module docstring)."""
    data, program = windows_system_dirs(os.environ if environ is None else environ)
    return (data / "config").is_dir() and (program / "current.txt").is_file()


def windows_elevated() -> bool:  # pragma: no cover - Windows only
    import ctypes

    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined,unused-ignore]
    except (AttributeError, OSError):
        return False


def decide_windows(
    args: Sequence[str],
    *,
    program: SystemProgram,
    environ: Mapping[str, str],
    installed: bool,
    bundled: bool,
    elevated: bool,
) -> Decision:
    """The Windows twin of :func:`decide` (module docstring). Pure."""
    if not installed or environ.get(INSTALL_MODE_ENV_VAR, "").strip():
        return Decision("none")
    if not args:
        if program.run_in_place_without_args:
            return Decision("none")
    elif (
        args[0] in _ALWAYS_ANYONE
        or args[0] in program.anyone
        or any(a in ("--help", "-h") for a in args)
    ):
        return Decision("none")
    command = first_command(args, program.value_options)
    if program.reexec_commands is not None and (
        command is None or command not in program.reexec_commands
    ):
        return Decision("none")
    data, _program_dir = windows_system_dirs(environ)
    if not bundled:
        return Decision(
            "warn",
            message=(
                f"{program.prog}: note: MeshForge is installed on this computer; this "
                "development command uses YOUR user-mode state, not the service's "
                f"({data}). Use {program.prog} from the installed version for the "
                f"service, or set {INSTALL_MODE_ENV_VAR}=user to silence this."
            ),
        )
    if command is not None and command in program.windows_refused:
        return Decision(
            "refuse",
            message=f"{program.prog} {command}: {program.windows_refused[command]}",
        )
    if not elevated:
        return Decision(
            "refuse",
            message=(
                f"{program.prog}: this command acts on the MeshForge service's state "
                f"({data}), which only administrators may change: run it in an "
                "elevated prompt (Run as administrator), or use the MeshForge "
                "console (Start menu -> MeshForge)"
            ),
        )
    return Decision(
        "inplace",
        message=(
            f"{program.prog}: acting on the MeshForge service's state ({data}) as "
            "administrator"
        ),
    )


def current_user() -> str | None:
    """The effective user's name (None on Windows or an unknown uid)."""
    if sys.platform == "win32":  # pragma: no cover - Linux package only
        return None
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return None


def first_command(args: Sequence[str], value_options: frozenset[str]) -> str | None:
    """The first positional argument (the sub-command), skipping options and
    the value of an option in ``value_options`` (``--config FILE``)."""
    skip = False
    for arg in args:
        if skip:
            skip = False
            continue
        if arg == "--":
            return None
        if arg.startswith("-"):
            skip = arg in value_options
            continue
        return arg
    return None


def decide(
    args: Sequence[str],
    *,
    program: SystemProgram,
    environ: Mapping[str, str],
    platform: str,
    user: str | None,
    installed: bool,
    bundled: bool,
) -> Decision:
    """What to do with ``<prog> <args>`` (module docstring). Pure."""
    if platform != "linux" or not installed or user == SERVICE_USER:
        return Decision("none")
    if environ.get(INSTALL_MODE_ENV_VAR, "").strip():
        return Decision("none")  # system (the unit) or an explicit user opt-out
    if not args:
        if program.run_in_place_without_args:
            return Decision("none")
    elif (
        args[0] in _ALWAYS_ANYONE
        or args[0] in program.anyone
        or any(a in ("--help", "-h") for a in args)
    ):
        return Decision("none")
    if program.reexec_commands is not None:
        command = first_command(args, program.value_options)
        if command is None or command not in program.reexec_commands:
            return Decision("none")
    if not bundled:
        return Decision(
            "warn",
            message=(
                f"{program.prog}: note: a system install exists; this development "
                "command uses YOUR user-mode state, not the service's "
                f"({SYSTEM_LINUX_DATA_DIR}). Use {program.prog} from the "
                f"package for the service, or set {INSTALL_MODE_ENV_VAR}=user to "
                "silence this."
            ),
        )
    argv = (
        "sudo",
        "-u",
        SERVICE_USER,
        "--",
        "/usr/bin/env",
        f"{INSTALL_MODE_ENV_VAR}=system",
        *program.extra_env,
        str(program.launcher),
        *args,
    )
    return Decision(
        "reexec",
        argv=argv,
        message=(
            f"{program.prog}: acting on the system service's state "
            f"({SYSTEM_LINUX_DATA_DIR}) as user {SERVICE_USER!r} via sudo"
        ),
    )


def apply(
    args: Sequence[str],
    *,
    program: SystemProgram,
    installed: Callable[[], bool],
    user: Callable[[], str | None],
    bundled: Callable[[], bool],
    execvp: Callable[[str, list[str]], object] = os.execvp,
    which: Callable[[str], str | None] = shutil.which,
    stderr: Callable[[str], object] | None = None,
) -> None:
    """Act on :func:`decide` for this process; returns only when the command
    should run here (``none`` / ``warn``). Exits ``CONFIG_ERROR`` when the
    re-execution is needed but ``sudo`` is missing."""
    say = stderr or (lambda text: sys.stderr.write(text + "\n"))
    if sys.platform == "win32":
        if program.windows:
            _apply_windows(args, program=program, bundled=bundled, say=say)
        return
    if sys.platform != "linux" or not installed():
        return  # the common case, decided with two stat calls
    decision = decide(
        args,
        program=program,
        environ=os.environ,
        platform=sys.platform,
        user=user(),
        installed=True,
        bundled=bundled(),
    )
    if decision.action == "none":
        return
    if decision.action == "warn":
        say(decision.message)
        return
    if which("sudo") is None:
        env = " ".join((f"{INSTALL_MODE_ENV_VAR}=system", *program.extra_env))
        say(
            f"{program.prog}: this machine's {program.prog} runs as the system "
            f"user {SERVICE_USER!r}; run this command as that user (no sudo found): "
            f"su -s /bin/sh {SERVICE_USER} -c '{env} {program.launcher} ...'"
        )
        raise SystemExit(int(ExitCode.CONFIG_ERROR))
    say(decision.message)
    sys.stdout.flush()
    sys.stderr.flush()
    execvp("sudo", list(decision.argv))


def _apply_windows(
    args: Sequence[str],
    *,
    program: SystemProgram,
    bundled: Callable[[], bool],
    say: Callable[[str], object],
) -> None:  # pragma: no cover - Windows only (decide_windows is the tested part)
    if not windows_installed():
        return
    decision = decide_windows(
        args,
        program=program,
        environ=os.environ,
        installed=True,
        bundled=bundled(),
        elevated=windows_elevated(),
    )
    if decision.action in ("warn", "inplace"):
        say(decision.message)
    if decision.action == "inplace":
        os.environ[INSTALL_MODE_ENV_VAR] = "system"
    elif decision.action == "refuse":
        say(decision.message)
        raise SystemExit(int(ExitCode.CONFIG_ERROR))
