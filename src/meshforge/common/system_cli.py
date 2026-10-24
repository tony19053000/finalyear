"""A human's admin command acts on the system service's state (V0.17 G1/G2, Q15).

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

    @property
    def launcher(self) -> Path:
        return self.program_dir / "current" / "bin" / self.prog

    def installed(self, config_dir: Path = SYSTEM_LINUX_CONFIG_DIR) -> bool:
        return config_dir.is_dir() and (self.program_dir / "current").exists()


@dataclass(frozen=True, slots=True)
class Decision:
    action: Literal["none", "reexec", "warn"]
    argv: tuple[str, ...] = ()
    message: str = ""


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
